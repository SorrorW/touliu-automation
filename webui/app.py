"""投流工具 Web UI 后端：薄 API 层，直接复用 巨量千川 现有异步模块。

所有浏览器操作耗时较长（秒级~分钟级），一律提交为后台 job，
前端轮询 /api/jobs/{id} 获取结果。仅监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from json import JSONDecodeError
from pathlib import Path
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from 巨量千川.auth import check_authorization, authorize_creator
from 巨量千川.chase_create import create_chase
from 巨量千川.chase_ops import delete_task, pause_task, resume_task, set_task_budget
from 巨量千川.daily_high_performer_monitor import (
    CHASE_BUDGET,
    CHASE_HOURS,
    MIN_ORDERS,
    MIN_ROI,
    scan_all_plans,
)
from 巨量千川.plan_create import PlanRequest, create_plan
from 巨量千川.plan_metrics import read_chase_by_creator, search_plan_metrics
from 巨量千川.session_prepare import (
    ACCOUNT_BY_DEPARTMENT,
    PRODUCT_MAP,
    _probe_cdp,
    lookup_product,
    prepare_session,
)

from . import db
from .agent_flow import BriefParseError, parse_auto, run_plan_flow, validate_fields
from .big_watch import RULES, add_creator, chase_one, evaluate_all, list_creators, remove_creator
from .jobs import manager
from .llm import LLMError, load_config, masked_config, save_config, test_connection
from .logging_setup import get_logger

db.init_db()

_log = get_logger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
DEPARTMENTS = tuple(ACCOUNT_BY_DEPARTMENT)

app = FastAPI(title="投流操作台", docs_url=None, redoc_url=None)


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
    roi: float = 7.3
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


@app.get("/api/status")
async def get_status() -> dict:
    """CDP 可用性、当前千川页面、部门/商品/阈值等静态配置。"""
    browser = _probe_cdp()
    qianchuan_url = None
    if browser:
        try:
            with urlopen("http://127.0.0.1:9222/json/list", timeout=1) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for t in targets:
                if t.get("type") == "page" and "qianchuan" in t.get("url", ""):
                    qianchuan_url = t["url"]
                    break
        except (OSError, JSONDecodeError):
            pass
    return {
        "cdp_ok": bool(browser),
        "browser": browser,
        "qianchuan_url": qianchuan_url,
        "departments": list(DEPARTMENTS),
        "products": list(PRODUCT_MAP),
        "thresholds": {"min_roi": MIN_ROI, "min_orders": MIN_ORDERS},
        "chase_defaults": {"budget": CHASE_BUDGET, "hours": CHASE_HOURS},
    }


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": manager.list()}


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
    job_id = manager.submit(
        f"监控扫描-{req.department}",
        scan_all_plans(
            creators=req.creators,
            department=req.department,
            progress_cb=manager.report_progress,
        ),
        on_done=lambda result: _on_scan_done(req.department, result),
    )
    return {"job_id": job_id}


def _on_scan_done(department: str, result: dict) -> None:
    db.save_scan(department, result)
    totals = {
        dep: d.get("total_plans", "err") for dep, d in result.items()
    } if isinstance(result, dict) else {}
    _log.info("扫描完成落库: %s %s", department, totals)


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
        authorize_creator(req.creator, req.code, req.department),
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
        }
        if auth.get("creator_name"):
            updates["creator_name"] = auth["creator_name"]  # 抓到昵称就回填，便于辨认
        # 授权已生效但条目还挂着「待授权」→ 推进为「待建计划」，消除状态冲突
        if auth["authorized"] and todo["status"] == "pending_auth":
            updates["status"] = "pending_plan"
            updates["message"] = "授权已生效，可执行 发布计划+追投"
        db.update_plan_todo(todo_id, **updates)
        return {"summary": summary, "authorized": auth["authorized"]}

    job_id = manager.submit(f"查询授权-{todo['creator_name'] or todo['creator_id']}", _run())
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
