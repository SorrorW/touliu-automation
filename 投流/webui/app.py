"""投流工具 Web UI 后端：薄 API 层，直接复用 巨量千川 现有异步模块。

所有浏览器操作耗时较长（秒级~分钟级），一律提交为后台 job，
前端轮询 /api/jobs/{id} 获取结果。仅监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from json import JSONDecodeError
from pathlib import Path
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from 巨量千川.auth import STATUS_OK, auth_mode_for_department, check_authorization, authorize_creator
from 巨量千川.chase_create import create_chase
from 巨量千川.chase_ops import delete_task, pause_task, resume_task, set_task_budget
from 巨量千川.daily_high_performer_monitor import (
    CHASE_BUDGET,
    CHASE_HOURS,
    MIN_ORDERS,
    MIN_ROI,
    scan_all_plans,
)
from 巨量千川.manual_whitelist import WHITELIST_FILE
from 巨量千川.plan_create import PlanRequest, create_plan
from 巨量千川.plan_metrics import read_chase_by_creator, search_plan_metrics
from 巨量千川.session_prepare import (
    ACCOUNT_BY_DEPARTMENT,
    PRODUCT_MAP,
    _cdp_http_opener,
    _probe_cdp,
    lookup_product,
    prepare_session,
)
from 视频号投流.session_prepare import prepare_session as channels_prepare_session
from 视频号投流.submit_order import submit_order as channels_submit_order

from . import db
from .agent_flow import BriefParseError, parse_auto, run_plan_flow, validate_fields
from .autoscan import INTERVAL_CHOICES, autoscan_loop, load_settings as load_autoscan, save_settings as save_autoscan
from .big_watch import RULES, add_creator, chase_one, evaluate_all, list_creators, remove_creator
from .jobs import manager
from .llm import LLMError, load_config, masked_config, save_config, test_connection
from .logging_setup import get_logger
from .poll_runner import configure as configure_poll_runner

db.init_db()

_log = get_logger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
DEPARTMENTS = tuple(ACCOUNT_BY_DEPARTMENT)

_SCHEDULED_TASK_NAMES = (
    # wecom_poll 已不设计划任务：轮询由桌面控制台进程托管（见 /api/auto/status），
    # 列出来只会显示「未找到」误导（2026-09-30 用户指认）。
    ("wecom_plan_close", "计划收口（每日 23:00，完成分类后放行夜间建计划）"),
    ("wecom_report_morning", "数据播报（每日 08:00）"),
    ("wecom_report_noon", "数据播报（每日 12:00）"),
    # wecom_daily_report(17:00) 是旧版任务，register_report_tasks_ascii2.ps1
    # 已按设计注销（17:00 由 wecom_evening_closeout 接替），勿再列出。
    ("wecom_report_night", "数据播报（每日 22:30）"),
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 监控页「自动扫描」后台协程：到点提交全部门扫描（走同一任务系统）
    task = asyncio.create_task(autoscan_loop(_autoscan_submit))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="投流操作台", docs_url=None, redoc_url=None, lifespan=_lifespan)


def _check_department(department: str, allow_all: bool = False) -> None:
    valid = set(DEPARTMENTS) | ({"all"} if allow_all else set())
    if department not in valid:
        raise HTTPException(400, f"无效部门: {department}（可选: {'/'.join(sorted(valid))}）")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class SessionReq(BaseModel):
    department: str


class ScanReq(BaseModel):
    department: str = "all"
    creators: list[str] | None = None


class PlanQueryReq(BaseModel):
    creator: str | None = None
    plan_name: str | None = None
    department: str


class ChaseQueryReq(BaseModel):
    creator: str
    department: str


class ChaseCreateReq(BaseModel):
    creator: str | None = None
    main_plan_id: str | None = None
    budget: int = 100
    hours: float = 24
    department: str


class ChaseQuickReq(BaseModel):
    """达标计划一键追投：只需 plan_id，预算/时长用监控默认参数。"""

    plan_id: str
    department: str


class ChaseOpsReq(BaseModel):
    action: str  # pause | resume | budget | delete
    task_id: str
    creator: str | None = None
    main_plan_id: str | None = None
    amount: int | None = None
    department: str


class PlanCreateReq(BaseModel):
    product: str
    creator: str
    department: str
    roi: float = 4.5
    budget: int = 300
    creator_name: str | None = None
    seller: str | None = None
    plan_name: str | None = None
    select_first_video: bool = False
    publish: bool = False
    confirm_risky: bool = False


class AuthCheckReq(BaseModel):
    creator: str
    department: str


class AuthAuthorizeReq(BaseModel):
    creator: str
    code: str | None = None
    department: str


# ---------------------------------------------------------------------------
# 系统状态
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    """Cheap liveness probe for the desktop shell; do not touch CDP/browser."""
    return {"ok": True}


@app.get("/api/status")
async def get_status() -> dict:
    """CDP 可用性、当前千川/视频号页面、部门/商品/阈值等静态配置。"""
    browser = _probe_cdp()
    qianchuan_url = None
    channels_url = None
    if browser:
        try:
            # 同 _probe_cdp：必须绕过系统代理读本机 CDP，否则带着代理变量
            # 启动的后端会把 127.0.0.1:9223 送进代理，页面列表永远为空。
            with _cdp_http_opener().open("http://127.0.0.1:9223/json/list", timeout=1) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for t in targets:
                if t.get("type") != "page":
                    continue
                url = t.get("url", "")
                if qianchuan_url is None and "qianchuan" in url:
                    qianchuan_url = url
                elif channels_url is None and "store.weixin.qq.com" in url:
                    channels_url = url
        except (OSError, JSONDecodeError):
            pass
    return {
        "cdp_ok": bool(browser),
        "browser": browser,
        "qianchuan_url": qianchuan_url,
        "channels_url": channels_url,
        "departments": list(DEPARTMENTS),
        "products": list(PRODUCT_MAP),
        "thresholds": {"min_roi": MIN_ROI, "min_orders": MIN_ORDERS},
        "chase_defaults": {"budget": CHASE_BUDGET, "hours": CHASE_HOURS},
    }


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": manager.list()}


def _scheduled_tasks() -> list[dict]:
    """读取本机 Windows 计划任务，供操作台展示自动化任务状态。

    计划任务独立于 Web UI 的 JobManager 进程运行，不能从 SQLite 的 jobs 表取得
    状态；这里读取任务计划程序的权威状态，不执行或修改任何计划任务。
    """
    names = [name for name, _label in _SCHEDULED_TASK_NAMES]
    labels = dict(_SCHEDULED_TASK_NAMES)
    quoted_names = ", ".join(f"'{name}'" for name in names)
    script = f"""
$names = @({quoted_names})
$rows = foreach ($name in $names) {{
  try {{
    $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    $info = $task | Get-ScheduledTaskInfo
    [PSCustomObject]@{{
      name = $task.TaskName
      state = [string]$task.State
      last_run_at = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('o') }} else {{ $null }}
      last_result = [int]$info.LastTaskResult
      next_run_at = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('o') }} else {{ $null }}
      error = $null
    }}
  }} catch {{
    [PSCustomObject]@{{
      name = $name
      state = 'Missing'
      last_run_at = $null
      last_result = $null
      next_run_at = $null
      error = $_.Exception.Message
    }}
  }}
}}
$rows | ConvertTo-Json -Compress
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            # PowerShell 5.1 控制台输出是 GBK：用 utf-8 解码会把中文错误
            # 信息变成替换符乱码（2026-09-30 控制台「结果」列红乱码）。
            encoding="mbcs",
            errors="replace",
            timeout=8,
            check=False,
            # uvicorn 无控制台：每次打开运行中心页都会给 powershell 弹可见黑窗。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "任务计划程序查询失败")
        payload = json.loads(proc.stdout or "[]")
        rows = payload if isinstance(payload, list) else [payload]
    except Exception as exc:  # 页面应可见故障，而不是静默空白
        return [{
            "name": name, "label": labels[name], "state": "Unavailable",
            "last_run_at": None, "last_result": None, "next_run_at": None,
            "error": str(exc),
        } for name in names]

    for row in rows:
        row["label"] = labels.get(row.get("name"), row.get("name", "未知任务"))
    return rows


@app.get("/api/system/scheduled-tasks")
async def list_scheduled_tasks() -> dict:
    """Windows 自动化定时任务状态（只读）。"""
    return {"tasks": _scheduled_tasks()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"任务不存在: {job_id}")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict:
    """终止任务：排队中的直接取消，执行中的中断浏览器操作。"""
    if not manager.cancel(job_id):
        raise HTTPException(409, f"任务不存在或已结束: {job_id}")
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# 会话 / 扫描 / 查询
# ---------------------------------------------------------------------------


@app.post("/api/session")
async def api_session(req: SessionReq) -> dict:
    _check_department(req.department)
    job_id = manager.submit(
        f"切换会话-{req.department}",
        prepare_session(req.department, time.monotonic() + 60.0),
    )
    return {"job_id": job_id}


@app.post("/api/scan")
async def api_scan(req: ScanReq) -> dict:
    """全表扫描（只收集数据，不追投）。department 支持 all。结果落库。"""
    _check_department(req.department, allow_all=True)
    job_id = _submit_scan(req.department, creators=req.creators)
    return {"job_id": job_id}


def _submit_scan(department: str, creators: list[str] | None = None) -> int:
    """提交监控扫描任务。手动按钮与自动扫描共用（自动扫描调它，部门固定 all）。"""
    # 用上次扫描的各部门条数做进度预估分母，让百分比全程真实
    est_totals: dict[str, int] = {}
    last = db.load_last_scan()
    if last:
        for dep, rec in last.get("departments", {}).items():
            n = (rec.get("data") or {}).get("total_plans")
            if isinstance(n, int) and n > 0:
                est_totals[dep] = n
    return manager.submit(
        f"监控扫描-{department}",
        scan_all_plans(
            creators=creators,
            department=department,
            progress_cb=manager.report_progress,
            est_totals=est_totals or None,
        ),
        on_done=lambda result: _on_scan_done(department, result),
    )


def _scan_job_active() -> bool:
    """已有扫描任务在排队/执行中（自动扫描据此避免重复堆积）。"""
    return any(
        j["status"] in {"queued", "running"} and "监控扫描" in j["name"]
        for j in db.list_jobs(20)
    )


def _autoscan_submit() -> None:
    """自动扫描到点提交：已有扫描在进行（手动或上一轮没跑完）则本轮跳过。"""
    if _scan_job_active():
        _log.info("自动扫描到点，但已有扫描任务在排队/执行，本轮跳过")
        return
    _submit_scan("all")


class AutoScanReq(BaseModel):
    enabled: bool
    interval_hours: int = 2


@app.get("/api/scan/autoscan")
async def api_autoscan_get() -> dict:
    """自动扫描设置（开关/周期/上次与下次运行时间）。"""
    return load_autoscan()


@app.post("/api/scan/autoscan")
async def api_autoscan_save(req: AutoScanReq) -> dict:
    if req.interval_hours not in INTERVAL_CHOICES:
        raise HTTPException(400, f"无效周期: {req.interval_hours}（可选: {INTERVAL_CHOICES}）")
    return save_autoscan(req.enabled, req.interval_hours)


def _on_scan_done(department: str, result: dict) -> None:
    db.save_scan(department, result)
    synced = db.sync_plan_catalog(result)
    totals = {
        dep: d.get("total_plans", "err") for dep, d in result.items()
    } if isinstance(result, dict) else {}
    _log.info("扫描完成落库: %s %s，全量计划库同步 %d 条", department, totals, synced)
    # 全部门扫描后的每日动作（2026-08-17 新规）：
    # 达标达人追投（近30天单量 ≥ 三店最高×0.6，每天评估一次）+
    # ROI>10 计划预算改不限（每天一次，已调过的跳过）。单部门扫描不参与
    # （「三店最高」必须全部门数据才算得准）。
    if department == "all" and isinstance(result, dict):
        has_error = any(isinstance(v, dict) and v.get("error") for v in result.values())
        if not has_error:
            manager.submit("每日达标处理（追投+预算）", _post_scan_daily_job(result))


@app.get("/api/plan-catalog")
async def api_plan_catalog() -> dict:
    """本地全量计划库，只读展示最近一次扫描到的所有实际计划。"""
    return {"items": db.list_plan_catalog()}


@app.get("/api/plan-board")
async def api_plan_board(date: str | None = None) -> dict:
    """计划每日看板：一行一个计划，8/12/17/23 四个扫描档位的分类标签。"""
    return {"items": db.list_plan_daily_board(date)}


# 每日扫描后处理的状态：按天重置（chase_done=今天已评估追投，
# budget_raised=今天已改不限的 plan_id）。
# 存 wecom-agent 目录：控制台（无论哪个包）与全自动轮询共用同一份门闩，
# 谁先触发谁执行，另一边看到 chase_done 就跳过（追投平台侧也幂等）。
POST_SCAN_STATE_FILE = (
    Path(__file__).resolve().parents[2] / "wecom-agent" / "growth_state.json"
)


def _load_post_scan_state() -> dict:
    try:
        st = json.loads(POST_SCAN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    today = datetime.date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "chase_done": False, "budget_raised": []}
    return st


def _save_post_scan_state(st: dict) -> None:
    POST_SCAN_STATE_FILE.write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


async def _post_scan_daily_job(today_report: dict) -> dict:
    """每日一次的扫描后处理：达标追投 + ROI>10 预算不限。"""
    from 巨量千川.budget_raise import raise_budgets
    from 巨量千川.daily_high_performer_monitor import scan_all_plans as _scan
    from 巨量千川.qualified_chase import MONTH_RANGE, chase_qualified

    st = _load_post_scan_state()
    out: dict = {}

    # 1. 达标追投：默认关闭，需在 automation.json 显式开启 growth_enabled。
    if not st.get("chase_done"):
        if not _growth_enabled():
            out["qualified_chase"] = {
                "success": True,
                "skipped": True,
                "message": "自动增长开关已关闭，未创建达标追投",
            }
            st["chase_done"] = True
            _save_post_scan_state(st)
        else:
            monthly = await _scan(department="all", time_range=MONTH_RANGE)
            if any(isinstance(v, dict) and v.get("error") for v in monthly.values()):
                out["qualified_chase"] = {
                    "success": False,
                    "message": "近30天扫描部分部门失败，本次不评估（明天再试）",
                }
            else:
                out["qualified_chase"] = await chase_qualified(monthly)
                st["chase_done"] = True
                _save_post_scan_state(st)
                _log.info("达标追投: %s", out["qualified_chase"].get("message"))

    # 2. ROI>10 预算不限：用刚扫完的当天报告，已调过的 plan_id 跳过
    if _growth_enabled():
        budget_res = await raise_budgets(
            today_report, already_raised=set(st.get("budget_raised", [])))
    else:
        budget_res = {
            "success": True,
            "skipped": True,
            "raised": [],
            "targets": [],
            "message": "自动增长开关已关闭，未调整计划预算",
        }
    if budget_res.get("raised"):
        st["budget_raised"] = sorted(
            set(st.get("budget_raised", [])) | set(budget_res["raised"]))
        _save_post_scan_state(st)
    if budget_res.get("targets"):
        _log.info("ROI>10 预算调整: %s", budget_res.get("message"))
    out["budget_raise"] = budget_res
    return out


@app.get("/api/scan/last")
async def api_scan_last() -> dict:
    """最近一次扫描结果（含时间戳），重开应用时恢复展示。"""
    last = db.load_last_scan()
    if not last:
        raise HTTPException(404, "还没有扫描记录")
    return last


@app.post("/api/plans/query")
async def api_plans_query(req: PlanQueryReq) -> dict:
    _check_department(req.department)
    if not req.creator and not req.plan_name:
        raise HTTPException(400, "creator 和 plan_name 至少填一个")

    async def _run() -> list[dict]:
        plans = await search_plan_metrics(
            creator_name=req.creator, plan_name=req.plan_name, department=req.department
        )
        return [asdict(p) for p in plans]

    job_id = manager.submit(f"查计划-{req.creator or req.plan_name}", _run())
    return {"job_id": job_id}


@app.post("/api/chase/query")
async def api_chase_query(req: ChaseQueryReq) -> dict:
    _check_department(req.department)

    async def _run() -> list[dict]:
        tasks = await read_chase_by_creator(req.creator, req.department)
        return [asdict(t) for t in tasks]

    job_id = manager.submit(f"查追投-{req.creator}", _run())
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 追投操作（写操作：前端需二次确认）
# ---------------------------------------------------------------------------


@app.post("/api/chase/create")
async def api_chase_create(req: ChaseCreateReq) -> dict:
    _check_department(req.department)
    if not req.creator and not req.main_plan_id:
        raise HTTPException(400, "creator 和 main_plan_id 至少填一个")
    job_id = manager.submit(
        f"建追投-{req.creator or req.main_plan_id}",
        create_chase(
            creator_name=req.creator,
            main_plan_id=req.main_plan_id,
            budget=req.budget,
            hours=req.hours,
            department=req.department,
        ),
    )
    return {"job_id": job_id}


@app.post("/api/chase/quick")
async def api_chase_quick(req: ChaseQuickReq) -> dict:
    """一键追投：按监控默认参数（预算/时长）对达标计划直接创建追投。"""
    _check_department(req.department)
    if not req.plan_id:
        raise HTTPException(400, "plan_id 必填")

    def _validate(result: dict) -> None:
        if not result.get("task_id"):
            _log.warning("一键追投未返回 task_id: plan_id=%s result=%s", req.plan_id, result)

    job_id = manager.submit(
        f"一键追投-{req.plan_id}",
        create_chase(
            main_plan_id=req.plan_id,
            budget=CHASE_BUDGET,
            hours=CHASE_HOURS,
            department=req.department,
        ),
        on_done=_validate,
    )
    return {"job_id": job_id}


@app.post("/api/chase/ops")
async def api_chase_ops(req: ChaseOpsReq) -> dict:
    _check_department(req.department)
    if not req.creator and not req.main_plan_id:
        raise HTTPException(400, "creator 和 main_plan_id 至少填一个")
    if not req.task_id:
        raise HTTPException(400, "task_id 必填")

    common = {
        "creator_name": req.creator,
        "main_plan_id": req.main_plan_id,
        "task_id": req.task_id,
        "department": req.department,
    }
    if req.action == "pause":
        coro, name = pause_task(**common), "暂停追投"
    elif req.action == "resume":
        coro, name = resume_task(**common), "恢复追投"
    elif req.action == "budget":
        if req.amount is None:
            raise HTTPException(400, "budget 操作必须提供 amount")
        coro, name = set_task_budget(budget=req.amount, **common), "改追投预算"
    elif req.action == "delete":
        coro, name = delete_task(**common), "删除追投"
    else:
        raise HTTPException(400, f"无效操作: {req.action}（pause/resume/budget/delete）")

    async def _run() -> dict:
        value = await coro
        return {"action": req.action, "task_id": req.task_id, "value": value}

    job_id = manager.submit(f"{name}-{req.task_id}", _run())
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 建投放计划 / 授权（写操作：前端需二次确认）
# ---------------------------------------------------------------------------


@app.post("/api/plan/create")
async def api_plan_create(req: PlanCreateReq) -> dict:
    _check_department(req.department)
    if lookup_product(req.product) is None:
        raise HTTPException(400, f"未知商品: {req.product}（可选: {'/'.join(PRODUCT_MAP)}）")
    if not req.plan_name and not (req.creator_name and req.seller):
        raise HTTPException(400, "不传 plan_name 时需同时提供 creator_name 和 seller")

    request = PlanRequest(
        product_name=req.product,
        creator_id=req.creator,
        department=req.department,
        roi=req.roi,
        daily_budget=req.budget,
        creator_name=req.creator_name,
        seller=req.seller,
        plan_name=req.plan_name,
        select_first_video=req.select_first_video,
        publish=req.publish,
        confirm_risky=req.confirm_risky,
    )

    async def _run() -> dict:
        result = await create_plan(request)
        return asdict(result) if hasattr(result, "__dataclass_fields__") else result

    job_id = manager.submit(f"建计划-{req.creator}", _run())
    return {"job_id": job_id}


@app.post("/api/auth/check")
async def api_auth_check(req: AuthCheckReq) -> dict:
    _check_department(req.department)
    job_id = manager.submit(
        f"授权检查-{req.creator}",
        check_authorization(req.creator, req.department),
    )
    return {"job_id": job_id}


@app.post("/api/auth/authorize")
async def api_auth_authorize(req: AuthAuthorizeReq) -> dict:
    """抖音号授权 + 投放授权申请。投放授权提交后需达人 24h 内在抖音 APP 确认。"""
    _check_department(req.department)
    job_id = manager.submit(
        f"授权申请-{req.creator}",
        authorize_creator(req.creator, req.code, req.department, auth_mode_for_department(req.department)),
    )
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 文本建计划 agent 流程（LLM 解析 + 两阶段执行）
# ---------------------------------------------------------------------------


class AgentParseReq(BaseModel):
    text: str


class AgentPlanReq(BaseModel):
    fields: dict


class AgentConfigReq(BaseModel):
    provider: str
    api_key: str | None = None  # 留空/不传 = 不修改已有 Key
    model: str | None = None


@app.get("/api/agent/config")
async def api_agent_config_get() -> dict:
    return masked_config()


@app.post("/api/agent/config")
async def api_agent_config_save(req: AgentConfigReq) -> dict:
    try:
        save_config(req.provider, req.api_key or None, req.model or None)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    _log.info("agent 配置保存: provider=%s model=%s key变更=%s",
              req.provider, req.model or "默认", bool(req.api_key))
    return masked_config()


@app.post("/api/agent/test")
async def api_agent_test() -> dict:
    try:
        model = test_connection(load_config())
    except LLMError as exc:
        _log.warning("agent 连接测试失败: %s", exc)
        raise HTTPException(400, str(exc)) from exc
    _log.info("agent 连接测试成功: model=%s", model)
    return {"ok": True, "model": model}


# ---------------------------------------------------------------------------
# 自动化（企微表格轮询 wecom-agent）：总开关 + 实时状态
# ---------------------------------------------------------------------------

# 默认只使用当前目录同级的 wecom-agent；需要外部目录时显式设置环境变量。
_PKG_AGENT_DIR = Path(__file__).resolve().parents[2] / "wecom-agent"
_EXTERNAL_AGENT_DIR = Path(os.environ["TOULIU_AGENT_DIR"]) if os.environ.get("TOULIU_AGENT_DIR") else None
WECOM_AGENT_DIR = _EXTERNAL_AGENT_DIR if _EXTERNAL_AGENT_DIR and _EXTERNAL_AGENT_DIR.is_dir() else _PKG_AGENT_DIR
AUTO_FLAG_FILE = WECOM_AGENT_DIR / "automation.json"
POLL_RUNNER = configure_poll_runner(WECOM_AGENT_DIR, AUTO_FLAG_FILE)


def _auto_enabled() -> bool:
    """缺文件/损坏都视为关闭（与 poll_and_run 判定保持一致）。"""
    try:
        return bool(json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8")).get("enabled", False))
    except Exception:
        return False


def _video_enabled() -> bool:
    """视频号任务表分开关（与 poll_and_run automation_enabled("video_enabled") 一致）。"""
    try:
        return bool(json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8")).get("video_enabled", False))
    except Exception:
        return False


def _growth_enabled() -> bool:
    """全自动追投和预算放开开关，默认关闭；手工一键操作不受影响。"""
    try:
        return bool(json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8")).get("growth_enabled", False))
    except Exception:
        return False


def _save_automation_flags(flags: dict) -> None:
    """Atomic switch writes prevent the runner from reading a partial JSON file."""
    AUTO_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = AUTO_FLAG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(flags, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(AUTO_FLAG_FILE)


class AutoSwitchReq(BaseModel):
    enabled: bool


class ManualWhitelistReq(BaseModel):
    creators: list[dict]


def _load_manual_whitelist() -> dict:
    try:
        raw = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        raw = {}
    return {"updated_at": raw.get("updated_at", ""), "policy": raw.get("policy", {}),
            "creators": raw.get("creators", [])}


def _validate_manual_whitelist(creators: list[dict]) -> list[dict]:
    validated: list[dict] = []
    names: set[tuple[str, str]] = set()
    for raw in creators:
        name = str(raw.get("name") or "").strip()
        department = str(raw.get("department") or "").strip()
        if not name:
            raise HTTPException(400, "人工白名单缺少达人名")
        _check_department(department)
        key = (department, name)
        if key in names:
            raise HTTPException(400, f"白名单存在重复达人: {department}/{name}")
        names.add(key)
        aliases = [str(item).strip() for item in raw.get("aliases", []) if str(item).strip()]
        plan_ids = [str(item).strip() for item in raw.get("plan_ids", []) if str(item).strip()]
        if any(not item.isdigit() for item in plan_ids):
            raise HTTPException(400, f"{name} 的计划 ID 只能包含数字")
        item = {"name": name, "aliases": aliases, "department": department,
                "plan_ids": plan_ids,
                "reason": str(raw.get("reason") or "人工投放").strip() or "人工投放"}
        # 保留 budget_watch 等扩展标记（2026-09-01 恩豆妈咪全天跑预算监控），
        # 否则 UI 保存一次会静默摘掉监控
        if raw.get("budget_watch"):
            item["budget_watch"] = True
        validated.append(item)
    return validated


@app.get("/api/manual-whitelist")
async def api_manual_whitelist() -> dict:
    return _load_manual_whitelist()


@app.get("/api/manual-whitelist-merged")
async def api_manual_whitelist_merged() -> dict:
    """只读合并视图（2026-09-07 表格驱动白名单）：底账 JSON + 表格缓存合并后的
    生效条目 + 表格来源条目 + 最近同步时间。UI 只读展示，不可在此编辑表格条目
    （表格条目要到企微表格「人工白名单」页改状态）。"""
    try:
        from 巨量千川 import manual_whitelist as mw
        base = _load_manual_whitelist()
        cache = mw._load_sheet_cache()
        merged = mw._load_entries()
        return {
            "base_creators": base.get("creators", []),          # 底账（可在 UI 编辑）
            "sheet_enable": cache.get("enable_entries", []),    # 表格启用条目（只读）
            "sheet_synced_at": cache.get("synced_at", ""),
            "sheet_conflicts": cache.get("conflicts", []),
            "sheet_deny_plan_ids": cache.get("deny_plan_ids", []),
            "sheet_kill_names": cache.get("kill_names", []),
            "merged_count": len(merged),
            "merged": merged,
        }
    except Exception as exc:
        return {"error": str(exc)[:200], "base_creators": _load_manual_whitelist().get("creators", []),
                "sheet_enable": [], "merged_count": 0, "merged": []}


@app.put("/api/manual-whitelist")
async def api_manual_whitelist_save(req: ManualWhitelistReq) -> dict:
    current = _load_manual_whitelist()
    payload = {"updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "policy": current["policy"],
               "creators": _validate_manual_whitelist(req.creators)}
    WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = WHITELIST_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(WHITELIST_FILE)
    _log.info("人工白名单已更新: %d 位达人", len(payload["creators"]))
    return payload


def failure_reason(detail: str) -> str:
    """返回自动化页面上显示的人话失败原因。"""
    text = str(detail or "").replace("\n", " ").replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip()
    rules = (
        ("60020", "企微 API 拒绝访问：本机公网 IP 不在可信 IP 白名单"),
        ("视频抽屉接口未返回任何视频", "达人暂无该商品挂车视频"),
        ("未搜索到抖音号", "千川页面未找到该抖音号"),
        ("无法勾选抖音号", "该抖音号对此商品没有推广权限"),
        ("scroll into view", "千川页面元素未稳定，自动化超时"),
        ("未授权/授权生效", "投放授权尚未完全生效"),
        ("只写了日期、没有姓名", "业务员字段/备注缺少业务员姓名"),
        ("识别不出业务员姓名", "备注中的业务员姓名无法识别"),
        ("cannot access local variable", "系统内部恢复日期异常（已修复）"),
    )
    for needle, reason in rules:
        if needle in text:
            return reason
    return text[:240] or "未记录具体错误"


@app.get("/api/auto/status")
async def api_auto_status() -> dict:
    """自动化总开关 + 轮询实时状态（status.json）+ 授权等待/断点列表 + 今日日志尾部。"""
    status: dict = {}
    status_file = WECOM_AGENT_DIR / "status.json"
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            status = {"error": "status.json 读取失败"}

    waiting: list[dict] = []
    records: list[dict] = []
    state_file = WECOM_AGENT_DIR / "state.json"
    MAX_ATTEMPTS = 3  # 与 poll_and_run.MAX_ATTEMPTS 一致：满 3 次回写「失败」停止重试
    if state_file.exists():
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        now = time.time()
        labels = st.get("labels") or {}
        waits = st.get("waiting_auth") or {}
        done_ids = set(st.get("processed_ids") or [])

        def _label(rid: str) -> str:
            return labels.get(rid, "")

        for rid, w in waits.items():
            steps = (st.get("steps") or {}).get(rid) or {}
            waiting.append({
                "record_id": rid,
                "creator": _label(rid),
                "step": next((k for k, v in steps.items() if v == "wait"), ""),
                "waited_hours": round(max(0.0, (now - w.get("since", now)) / 3600), 1),
                "next_check": time.strftime("%H:%M", time.localtime(w.get("next_check", 0))),
                "note": (w.get("note") or "")[:120],
            })
        for rid, steps in (st.get("steps") or {}).items():
            attempts = (st.get("attempts") or {}).get(rid, 0)
            failed = [k for k, v in steps.items() if v == "fail"]
            waiting_steps = [k for k, v in steps.items() if v == "wait"]
            finished = rid in done_ids
            # 断点：卡住的模块（等待 > 失败 > 最后一个成功的下一步）
            breakpoint_step = (waiting_steps or failed or [""])[0]
            error = (st.get("errors") or {}).get(rid, "")[:200]
            # 下一步动作与时机
            if rid in waits:
                next_action = f"{time.strftime('%H:%M', time.localtime(waits[rid].get('next_check', 0)))} 复查授权"
            elif finished:
                next_action = "已完成"
            elif attempts >= MAX_ATTEMPTS:
                next_action = "已失败停止（表格进度改回「待处理」可续跑）"
            else:
                cadence_minutes = 10 if datetime.datetime.now().hour < 8 else 15
                next_action = f"下轮重试（定时任务每{cadence_minutes}分钟一轮）"
            records.append({
                "record_id": rid,
                "creator": _label(rid),
                "attempts": attempts,
                "done": [k for k, v in steps.items() if v == "ok"],
                "waiting": waiting_steps,
                "failed": failed,
                "breakpoint": breakpoint_step,
                "error": error,
                "failure_reason": failure_reason(error) if error else "",
                "next_action": next_action,
                "finished": finished,
            })
        records.sort(key=lambda r: (r["finished"], -r["attempts"]))
        # 当前状态补上达人名
        cur_rid = status.get("record_id") or ""
        if cur_rid:
            status["creator"] = _label(cur_rid)

    log_lines: list[str] = []
    log_file = WECOM_AGENT_DIR / "logs" / f"poll_{datetime.date.today():%Y%m%d}.log"
    if log_file.exists():
        try:
            log_lines = log_file.read_text(
                encoding="utf-8", errors="replace").splitlines()[-60:]
        except OSError:
            pass

    runner_state = POLL_RUNNER.snapshot()
    if runner_state.get("mode") == "not_started":
        # 裸 uvicorn 重启时后端进程自身不跑轮询线程（轮询由桌面控制台进程托管，
        # 见 desktop.py runner.start()）。此时不要误报「尚未启动」——用 status.json/
        # 轮询日志的新鲜度推断真实轮询状态，避免误导使用者以为自动化停了。
        try:
            last_poll = status.get("updated_at") or status.get("round_started") or ""
            last_dt = datetime.datetime.fromisoformat(str(last_poll)) if last_poll else None
        except Exception:
            last_dt = None
        runner_log = WECOM_AGENT_DIR / "logs" / "poll_runner_stdout.log"
        if last_dt is None and runner_log.exists():
            try:
                last_dt = datetime.datetime.fromtimestamp(runner_log.stat().st_mtime)
            except OSError:
                last_dt = None
        if last_dt is not None:
            age_min = (datetime.datetime.now() - last_dt).total_seconds() / 60
            if age_min <= 25:
                runner_state = {
                    "mode": "external",
                    "message": "轮询由桌面控制台托管（后端为页面服务进程）",
                    "last_started_at": status.get("round_started", ""),
                    "last_finished_at": status.get("updated_at", ""),
                    "last_exit_code": None,
                    "next_run_at": "",
                    "process_running": False,
                }
            else:
                runner_state = dict(runner_state)
                runner_state["message"] = (
                    f"桌面轮询疑似停滞（上轮 {last_dt:%H:%M}，已 {int(age_min)} 分钟）；"
                    "请确认桌面控制台窗口是否还开着"
                )

    return {
        "enabled": _auto_enabled(),
        "video_enabled": _video_enabled(),
        "growth_enabled": _growth_enabled(),
        "runner": runner_state,
        "status": status,
        "waiting": waiting,
        "records": records,
        "log_tail": log_lines,
        "log_file": log_file.name,
    }


@app.post("/api/auto/enabled")
async def api_auto_enabled(req: AutoSwitchReq) -> dict:
    """切换自动化总开关，并唤醒桌面托管的轮询器。"""
    flags: dict = {}
    try:
        flags = json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    flags["enabled"] = req.enabled  # 合并写回，保留 video_enabled 等分表开关
    _save_automation_flags(flags)
    POLL_RUNNER.notify()
    _log.info("自动化总开关切换: %s", "开启" if req.enabled else "关闭")
    return {"enabled": req.enabled}


@app.post("/api/auto/video_enabled")
async def api_auto_video_enabled(req: AutoSwitchReq) -> dict:
    """切换视频号任务表轮询开关（与抖音任务表独立，2026-08-18 用户要求分开控制）。"""
    flags: dict = {}
    try:
        flags = json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    flags["video_enabled"] = req.enabled  # 合并写回，保留总开关
    _save_automation_flags(flags)
    POLL_RUNNER.notify()
    _log.info("视频号轮询开关切换: %s", "开启" if req.enabled else "关闭")
    return {"video_enabled": req.enabled}


@app.post("/api/auto/growth_enabled")
async def api_auto_growth_enabled(req: AutoSwitchReq) -> dict:
    """自动追投和预算放开已固定为人工操作，禁止从 UI 开启。"""
    if req.enabled:
        raise HTTPException(409, "自动追投和预算放开已改为人工操作，不能开启")
    flags: dict = {}
    try:
        flags = json.loads(AUTO_FLAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    flags["growth_enabled"] = req.enabled
    _save_automation_flags(flags)
    POLL_RUNNER.notify()
    _log.info("自动增长开关切换: %s", "开启" if req.enabled else "关闭")
    return {"growth_enabled": req.enabled}


class AutoRetryReq(BaseModel):
    record_id: str


@app.post("/api/auto/retry")
async def api_auto_retry(req: AutoRetryReq) -> dict:
    """手动重试单条记录：隐藏窗口拉起 poll_and_run --retry，立即从断点续跑。

    提前解除授权等待、失败记录自动捞回；已投放/已完成记录 poll 侧仍会拒绝，
    防重复扣费的最后一道闸在 poll 的进度过滤，不在此。
    """
    import os
    import subprocess

    rid = req.record_id.strip()
    if not rid:
        raise HTTPException(400, "record_id 为空")
    if not _auto_enabled():
        raise HTTPException(409, "自动化总开关已关闭，无法执行重试；请先开启开关")
    # 解释器定位：便携包用内置 runtime，开发机回落到本机 Python
    import sys as _sys
    candidates = [
        WECOM_AGENT_DIR.parent / "投流" / "runtime" / "python.exe",
        Path(r"C:\Users\fengsheng\AppData\Local\Programs\Python\Python312\python.exe"),
        Path(_sys.executable),
    ]
    python = next((str(p) for p in candidates if p.is_file()), None)
    if not python:
        raise HTTPException(500, "找不到可用的 Python 解释器")
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    subprocess.Popen(  # noqa: S603 —— 固定解释器+脚本，rid 只作记录标识
        [python, "poll_and_run.py", "--retry", rid],
        cwd=str(WECOM_AGENT_DIR),
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _log.info("手动重试触发: %s", rid)
    return {"ok": True, "record_id": rid}


@app.post("/api/agent/parse")
async def api_agent_parse(req: AgentParseReq) -> dict:
    """只解析不执行：返回字段 + 解析路径（deepseek/kimi/规则解析）。

    字段（含合作码）原样返回给前端暂存，确认执行时随 fields 回传——
    本机自用控制台，无泄露面。
    """
    if not req.text.strip():
        raise HTTPException(400, "文本为空")
    try:
        return parse_auto(req.text)
    except BriefParseError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/agent/plan")
async def api_agent_plan(req: AgentPlanReq) -> dict:
    """执行确认后的字段（服务端重新校验）。"""
    try:
        fields = validate_fields(req.fields)
    except BriefParseError as exc:
        _log.warning("建计划字段校验失败: %s", exc)
        raise HTTPException(400, str(exc)) from exc
    _log.info(
        "建计划提交: 部门=%s 业务员=%s 抖音号=%s 商品=%s 发布=%s",
        fields["department"], fields["seller"], fields["creator_id"],
        fields["product"], fields["publish"],
    )
    job_id = manager.submit(
        f"文本建计划-{fields['creator_id']}",
        run_plan_flow(fields, progress_cb=manager.report_progress),
        on_done=lambda result: _record_plan_todo(fields, result),
    )
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 待建计划（建计划页右侧面板：未上线计划的达人 + 授权状态 + 一键重建）
# ---------------------------------------------------------------------------


def _record_plan_todo(fields: dict, result: dict) -> None:
    """建计划流程结果落 plan_todos：未完成的进面板，已完成标 done。"""
    if not isinstance(result, dict):
        return
    steps = result.get("steps") or []
    plan_id = result.get("plan_id")
    if result.get("ok"):
        # 已发布 → done（不显示）；未发布 → 待发布（可用按钮补发布+追投）
        status = "done" if fields.get("publish") else "pending_publish"
        message = steps[-1].get("detail", "") if steps else ""
    else:
        failed = next((s for s in steps if s.get("status") == "failed"), None)
        if not failed:
            return  # 结果结构异常，不记录
        step_name = failed.get("step", "")
        if "授权" in step_name:
            status = "pending_auth"
        elif "追投" in step_name:
            status = "pending_chase"
        else:
            status = "pending_plan"
        message = failed.get("detail", "")
    # 授权步结果一并落库：面板直接显示真实授权状态，不再出现"授权未查询"
    auth = result.get("auth") or {}
    # 防御：流程在授权步失败但查询结果显示已生效 → 不应挂「待授权」
    if status == "pending_auth" and auth.get("authorized"):
        status = "pending_plan"
    todo_id = db.upsert_plan_todo(
        creator_id=fields["creator_id"],
        department=fields["department"],
        creator_name=fields.get("creator_name") or auth.get("creator_name"),
        fields=fields,
        status=status,
        plan_id=plan_id,
        message=(message or "")[:300],
    )
    if auth.get("douyin_auth") is not None:
        db.update_plan_todo(
            todo_id,
            auth_status=f"抖音号:{auth.get('douyin_auth')} 投放:{auth.get('toufang_auth')}",
            authorized=1 if auth.get("authorized") else 0,
            auth_checked_at=time.time(),
        )
    _log.info("待建计划落库: %s（%s）→ %s", fields["creator_id"], fields["department"], status)


@app.get("/api/agent/todos")
async def api_agent_todos() -> dict:
    """待建计划列表（不含已完成）。"""
    return {"items": db.list_plan_todos()}


@app.post("/api/agent/todos/{todo_id}/recheck")
async def api_agent_todo_recheck(todo_id: int) -> dict:
    """重新查询授权：更新面板条目的授权状态。"""
    todo = db.get_plan_todo(todo_id)
    if not todo:
        raise HTTPException(404, f"待建计划条目不存在: {todo_id}")

    async def _run() -> dict:
        auth = await check_authorization(todo["creator_id"], todo["department"])
        summary = f"抖音号:{auth['douyin_auth']} 投放:{auth['toufang_auth']}"
        updates: dict = {
            "auth_status": summary,
            "authorized": 1 if auth["authorized"] else 0,
            "auth_checked_at": time.time(),
        }
        if auth.get("creator_name"):
            updates["creator_name"] = auth["creator_name"]  # 抓到昵称就回填，便于辨认
        # 查询成功即刷新说明，避免旧的报错文本（如 Page.goto 超时）一直挂着
        if auth["authorized"]:
            updates["message"] = "授权已生效，可执行 发布计划+追投"
        else:
            updates["message"] = f"授权未完成（{summary}），达人确认后可再查询"
        # 授权已生效但条目还挂着「待授权」→ 推进为「待建计划」，消除状态冲突
        if auth["authorized"] and todo["status"] == "pending_auth":
            updates["status"] = "pending_plan"
        db.update_plan_todo(todo_id, **updates)
        return {"summary": summary, "authorized": auth["authorized"]}

    job_id = manager.submit(f"查询授权-{todo['creator_name'] or todo['creator_id']}", _run())
    return {"job_id": job_id}


@app.post("/api/agent/todos/{todo_id}/reauth")
async def api_agent_todo_reauth(todo_id: int) -> dict:
    """重新申请授权：24h 未确认导致申请失效时，用条目存的合作码重走授权流程。"""
    todo = db.get_plan_todo(todo_id)
    if not todo:
        raise HTTPException(404, f"待建计划条目不存在: {todo_id}")
    code = (todo["fields"] or {}).get("code")
    if not code:
        raise HTTPException(400, "该条目没有合作码，无法重新申请授权，请删除后重新建计划")

    async def _run() -> dict:
        auth = await authorize_creator(todo["creator_id"], code, todo["department"], auth_mode_for_department(todo["department"]))
        summary = f"抖音号:{auth['douyin_auth']} 投放:{auth['toufang_auth']}"
        authorized = auth["douyin_auth"] == STATUS_OK and auth["toufang_auth"] == STATUS_OK
        updates: dict = {
            "auth_status": summary,
            "authorized": 1 if authorized else 0,
            "auth_checked_at": time.time(),
            "message": (auth.get("message") or "")[:300],
        }
        if auth.get("creator_name"):
            updates["creator_name"] = auth["creator_name"]
        if authorized and todo["status"] == "pending_auth":
            updates["status"] = "pending_plan"
            updates["message"] = "授权已生效，可执行 发布计划+追投"
        db.update_plan_todo(todo_id, **updates)
        return {"summary": summary, "authorized": authorized}

    job_id = manager.submit(f"重新申请授权-{todo['creator_name'] or todo['creator_id']}", _run())
    return {"job_id": job_id}


@app.post("/api/agent/todos/{todo_id}/retry")
async def api_agent_todo_retry(todo_id: int) -> dict:
    """发布计划+追投：pending_chase 只补追投（计划已在），其余重跑完整链路。"""
    todo = db.get_plan_todo(todo_id)
    if not todo:
        raise HTTPException(404, f"待建计划条目不存在: {todo_id}")
    fields = dict(todo["fields"])

    async def _run() -> dict:
        if todo["status"] == "pending_chase" and todo["plan_id"]:
            try:
                chase = await create_chase(
                    main_plan_id=todo["plan_id"],
                    budget=CHASE_BUDGET,
                    hours=CHASE_HOURS,
                    department=todo["department"],
                )
            except Exception:
                # 上次可能实际已创建、只是回读 task_id 失败 ——
                # 核实任务中心：已有追投任务就视为完成，不再挂失败
                existing = []
                try:
                    existing = await read_chase_by_creator(
                        todo["creator_name"] or todo["creator_id"], todo["department"]
                    )
                except Exception:
                    pass
                if existing:
                    db.update_plan_todo(
                        todo_id,
                        status="done",
                        message=f"千川已有 {len(existing)} 个追投任务在跑，视为完成",
                    )
                    return {"mode": "chase_only", "verified_existing": len(existing)}
                raise
            db.update_plan_todo(
                todo_id, status="done", message=f"追投已补建 task_id={chase.get('task_id')}"
            )
            return {"mode": "chase_only", "chase": chase}
        if todo["status"] == "pending_publish":
            fields["publish"] = True  # 补发布：重跑时强制直接发布
        result = await run_plan_flow(fields, progress_cb=manager.report_progress)
        _record_plan_todo(fields, result)
        return {"mode": "full_flow", **result}

    job_id = manager.submit(
        f"发布计划+追投-{todo['creator_name'] or todo['creator_id']}", _run()
    )
    return {"job_id": job_id}


@app.delete("/api/agent/todos/{todo_id}")
async def api_agent_todo_delete(todo_id: int) -> dict:
    if not db.delete_plan_todo(todo_id):
        raise HTTPException(404, f"待建计划条目不存在: {todo_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 重点关注达人（列表读写统一在 big_watch，页面增删即文件增删）
# ---------------------------------------------------------------------------


class WatchAddReq(BaseModel):
    name: str
    department: str


@app.get("/api/watchlist")
async def api_watchlist_get() -> dict:
    return {"items": list_creators()}


@app.post("/api/watchlist")
async def api_watchlist_add(req: WatchAddReq) -> dict:
    try:
        items = add_creator(req.name, req.department)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"items": items}


@app.delete("/api/watchlist/{name}")
async def api_watchlist_remove(name: str) -> dict:
    try:
        items = remove_creator(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"items": items}


@app.post("/api/watchlist/scan")
async def api_watchlist_scan() -> dict:
    """逐达人查询当日计划数据（串行）。"""
    items = list_creators()
    if not items:
        raise HTTPException(400, "关注列表为空，请先添加达人")

    async def _run() -> list[dict]:
        results = []
        for idx, item in enumerate(items, 1):
            try:
                plans = await search_plan_metrics(
                    creator_name=item["name"], department=item["department"]
                )
                if plans:
                    p = plans[0]
                    results.append({
                        **item,
                        "plan_name": p.plan_name,
                        "status": p.status,
                        "spent": p.spent,
                        "roi": p.roi,
                        "orders": p.orders,
                        "plan_id": p.plan_id,
                    })
                else:
                    results.append({**item, "error": "未找到计划"})
            except Exception as exc:
                results.append({**item, "error": str(exc)})
            manager.report_progress(idx, len(items))
        return results

    job_id = manager.submit("关注达人扫描", _run())
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 重点关注大达人（big_creator_monitor 状态机规则）
# ---------------------------------------------------------------------------


@app.get("/api/bigwatch")
async def api_bigwatch_list() -> dict:
    return {"items": list_creators(), "rules": RULES}


@app.post("/api/bigwatch/scan")
async def api_bigwatch_scan() -> dict:
    """按状态机规则评估是否符合追投（不真实追投）。结果落库。"""
    job_id = manager.submit(
        "大达人评估",
        evaluate_all(progress_cb=manager.report_progress),
        on_done=lambda result: db.save_eval(result),
    )
    return {"job_id": job_id}


@app.get("/api/bigwatch/last")
async def api_bigwatch_last() -> dict:
    """最近一次评估结果（含时间戳），重开应用时恢复展示。"""
    last = db.load_last_eval()
    if not last:
        raise HTTPException(404, "还没有评估记录")
    return last


class BigChaseReq(BaseModel):
    name: str
    department: str


@app.post("/api/bigwatch/chase")
async def api_bigwatch_chase(req: BigChaseReq) -> dict:
    """一键追投：走 big_creator_monitor 状态机（内部拦截休息期/追投中/上限）。"""
    job_id = manager.submit(
        f"大达人追投-{req.name}",
        chase_one(req.name, req.department),
    )
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 视频号投流（微信小店推广：选视频 + 填表 + 勾协议，全程不点「提交」）
# ---------------------------------------------------------------------------


class ChannelsSubmitReq(BaseModel):
    keyword: str | None = None       # 作者关键词：传则先搜作者
    start_date: str | None = None    # 视频筛选日期 YYYY-MM-DD（单日）
    roi: float = 7.3
    budget: int = 300
    no_budget: bool = False          # 跳过预算（选商品ROI后页面自动带出）
    hours: int = 24
    priority_target: str = "商品ROI"
    bid_method: str = "控成本加热"
    template: str = "肥牛片"
    no_template: bool = False        # 跳过人群模版


@app.post("/api/channels/session")
async def api_channels_session() -> dict:
    """拉起 Edge 并打开视频号小店推广页（未登录时在浏览器里扫码）。"""
    job_id = manager.submit(
        "打开会话",
        channels_prepare_session(time.monotonic() + 60.0),
        platform="视频号",
    )
    return {"job_id": job_id}


@app.post("/api/channels/submit")
async def api_channels_submit(req: ChannelsSubmitReq) -> dict:
    """视频号加热订单填表：选视频 → 填表 → 勾协议，不点提交，留人工核对。"""
    if req.start_date:
        try:
            datetime.date.fromisoformat(req.start_date)
        except ValueError:
            raise HTTPException(400, f"日期格式错误: {req.start_date}（应 YYYY-MM-DD）")
    if not req.no_template and not req.template.strip():
        raise HTTPException(400, "模版名不能为空（或勾选跳过模版）")
    _log.info(
        "视频号下单提交: 作者=%s 日期=%s 目标=%s 出价=%s ROI=%s 预算=%s 时长=%sh 模版=%s",
        req.keyword or "默认视频", req.start_date or "不限",
        req.priority_target, req.bid_method, req.roi,
        "自动带出" if req.no_budget else req.budget,
        req.hours, "跳过" if req.no_template else req.template,
    )
    job_id = manager.submit(
        f"下单-{req.keyword or '默认视频'}",
        channels_submit_order(
            roi=req.roi,
            budget=None if req.no_budget else req.budget,
            hours=req.hours,
            priority_target=req.priority_target,
            bid_method=req.bid_method,
            keyword=req.keyword or None,
            start_date=req.start_date or None,
            template=None if req.no_template else req.template,
        ),
        platform="视频号",
    )
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# 运行日志
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def api_logs(lines: int = 200) -> dict:
    """日志文件尾部 N 行。"""
    from .logging_setup import LOG_FILE

    lines = max(1, min(lines, 2000))
    if not LOG_FILE.exists():
        return {"lines": [], "file": str(LOG_FILE)}
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": content[-lines:], "file": str(LOG_FILE), "total": len(content)}


@app.get("/api/logs/download")
async def api_logs_download() -> FileResponse:
    from .logging_setup import LOG_FILE

    if not LOG_FILE.exists():
        raise HTTPException(404, "日志文件还不存在")
    return FileResponse(LOG_FILE, filename="webui.log", media_type="text/plain")


# 静态页面（放在最后，/api 路由优先匹配）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
