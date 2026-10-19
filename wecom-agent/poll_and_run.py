#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""poll_and_run.py —— 轮询执行主程序（多表 + 步骤日志 + 断点重试 + 浏览器互斥锁版）

当前抖音任务：主表和旧素材表统一由 main_account_flow 处理。
  商品ID匹配四主品 → 完整核实主号对应计划 → 未绑定则合作达人授权 → 生效后加入现有计划。
  待主号授权 / 等待达人接受 / 待加入主品 / 主号已加入（自动拉取为主号已拉取）。
  不再分部授权、不新建计划，不受 21:00 / 23:00 窗口或人工允许授权闸门限制。
  历史分部已投放记录不重开；人工停投、商品、佣金、平台限制仍生效。
  主号等待不消耗旧重试次数，也不因等待超过48小时误标失败。
  共享单实例、浏览器互斥、记录间播报让行；每表每轮默认最多10条，等待下轮复查。

视频号任务仍沿用原本 local_tools 流程；下方旧分部代码仅保留兼容，抖音表不再进入。

CLI：poll_and_run.py [--dry-run] [--retry RECORD_ID] [--main-limit N]
真实绑定预览：main_account_flow.py --preview --output preview.json
手动主号补跑：main_account_flow.py --apply [--record RECORD_ID] [--all]
"""
import argparse
import datetime
import json
import logging
import msvcrt
import os
import re
import sys
import time
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 企微 API 必须直连：清掉本机代理变量（定时任务环境会继承系统代理）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

# 便携包内置 runtime 的 python312._pth 完全接管 sys.path，脚本目录不在其中，
# 必须显式补上，否则 import wecom_client 报 ModuleNotFoundError
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "投流")))

if __name__ == "__main__":
    sys.modules.setdefault("poll_and_run", sys.modules[__name__])

import wecom_client
import local_tools
from webui import db as plan_db

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
STATUS_FILE = os.path.join(HERE, "status.json")
INSTANCE_LOCK_FILE = os.path.join(HERE, ".poll_instance.lock")
POLL_LOCK_FILE = os.path.join(HERE, "browser_poll.lock")
AUTOMATION_FLAG_FILE = os.path.join(HERE, "automation.json")  # 操作台 UI 的总开关
WEBUI_LOCK_FILE = os.path.normpath(
    os.path.join(HERE, "..", "投流", "webui", "assets", "browser.lock"))
LOG_DIR = os.path.join(HERE, "logs")

MAX_ATTEMPTS = 3         # 单条记录最大尝试次数，达到后回写「失败」并停止重试
MAX_TASKS_PER_RUN = 10    # 每张表每轮最多处理条数，其余留下轮（2026-10-09 用户令：5→10）
# 基础设施瞬态关键词：命中则不消耗重试次数（见 fail 分支注释）
_INFRA_TRANSIENT = ("SessionPrepareError", "标签上限", "标签页已达",
                    "CDP 未就绪", "connect_over_cdp")
LOCK_STALE_SECONDS = 30 * 60   # 锁文件超 30 分钟视为残留（持有进程已死）
AUTH_GIVEUP_SECONDS = 48 * 3600    # 授权等待超 48 小时仍未生效 → 标记「失败」人工介入

# 建计划和新增素材只在分类完成后的 23:00-次日 07:59；轮询和断点重试不再分时段。
# 计划任务每 15 分钟触发一次，内部也保持相同节奏，避免手动触发任务时重复扫描。
def is_night_window(now: datetime.datetime | None = None) -> bool:
    """夜间窗口 23:00-次日07:59：建计划窗口 + 快档轮询。"""
    now = now or datetime.datetime.now()
    return now.hour >= 23 or now.hour < 8


def round_cadence_seconds() -> int:
    """整轮固定每 15 分钟一次。"""
    return 900


def retry_interval_seconds() -> int:
    """断点重试和授权复查固定每 30 分钟一次。"""
    return 1800

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"poll_{datetime.date.today():%Y%m%d}.log"),
            encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("poll")
plan_db.init_db()


# ---------------------------------------------------------------------------
# 状态文件（state.json 持久化 / status.json 实时可见）
# ---------------------------------------------------------------------------

def load_state():
    """state schema: {processed_ids, attempts:{rid:n}, steps:{rid:{step:ok/fail/wait}},
       errors:{rid:msg}, waiting_auth:{rid:{since,next_check,note}}}"""
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            state = {}
    else:
        state = {}
    state.setdefault("processed_ids", [])
    state.setdefault("attempts", {})
    state.setdefault("steps", {})
    state.setdefault("errors", {})
    state.setdefault("waiting_auth", {})
    state.setdefault("step_notes", {})
    state.setdefault("labels", {})  # rid → 抖音号/作者关键词，供操作台展示
    return state


def save_state(state):
    state["processed_ids"] = sorted(set(state["processed_ids"]))
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _failure_reason(detail: str) -> str:
    """将内部错误压缩成可直接告诉业务人员的失败原因。"""
    text = str(detail or "").replace("\n", " ").replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip()
    rules = (
        ("60020", "企微 API 拒绝访问：本机公网 IP 不在企业微信可信 IP 白名单"),
        ("视频抽屉接口未返回任何视频", "达人当前没有该商品的挂车视频，暂时无法建计划"),
        ("未搜索到抖音号", "千川页面未找到该抖音号，需确认账号归属/授权状态"),
        ("无法勾选抖音号", "该抖音号对此商品没有推广权限，商品投放授权尚未生效"),
        ("scroll into view", "千川页面元素未稳定，自动化操作超时"),
        ("未授权/授权生效", "授权状态仍未完全生效（投放授权未完成）"),
        ("只写了日期、没有姓名", "业务员字段/备注只有日期，缺少业务员姓名"),
        ("识别不出业务员姓名", "备注中的业务员姓名无法识别"),
        ("cannot access local variable 'resume_date'", "系统内部恢复日期变量异常，已修复；请重新点立即重试"),
    )
    for needle, reason in rules:
        if needle in text:
            return reason
    return text[:300] or "未记录具体错误"


def notify_failure(rid: str, simple: dict, detail: str) -> None:
    """失败时把人话原因落入本地通知日志，并反推监控群（2026-09-29 用户口径：
    新计划/新素材失败原因要能在群里直接看到）。推送失败入发件箱待补发。"""
    reason = _failure_reason(detail)
    payload = {
        "record_id": rid,
        "creator": simple.get("抖音号") or simple.get("达人名称") or "",
        "department": simple.get("业务部") or "",
        "reason": reason,
        "detail": str(detail or "")[:500],
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(os.path.join(LOG_DIR, "failure_notifications.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("失败通知写入失败: %s", rid)
    log.error("失败通知 %s（%s）：%s", rid, payload["creator"], reason)
    _push_failure_to_group(payload)


# 失败进度选项映射（与主表 sheets 配置一致；供群推函数在 sheets 局部变量之外使用）
_FAIL_PROGRESS_OPTIONS = {
    "fail": "失败",
    "fail_commission": "佣金过高",
    "fail_no_perm": "达人无推广权限",
    "fail_auth_blocked": "授权被平台拦截",
    "fail_coop_code": "合作码错误",
    "fail_bad_product": "商品ID无效",
    "fail_creator_no_confirm": "达人未确认授权",
}


_release21_batch: list = []  # 21点放行复核批量记录，轮末汇总播报（2026-10-14）


def _push_await_pull(simple: dict, detail: str = "") -> None:
    """「待主号拉取」提醒推业务部门群（2026-10-15 用户令：系统不再授权/建计划，
    提醒操作人员手动去主号四大主品计划拉取）。部门缺失/无群配置回退汇总群。
    绝不抛异常影响主流程。"""
    try:
        import daily_report_push as push_mod
        import alert_outbox
        hooks = push_mod.load_webhooks()
        if not hooks:
            return
        dept = (simple.get("业务部") or "").strip()
        url = hooks.get(dept) or hooks.get("汇总")
        if not url:
            return
        cid = simple.get("抖音号") or ""
        seller = simple.get("业务员") or ""
        md = "\n".join([
            f"**【待主号拉取】{cid}**",
            f"部门：{dept or '未知'} ｜ 业务员：{seller or '未知'} ｜ 商品：{simple.get('商品ID') or ''}",
            "新填表已登记。按新规则系统**不再自动授权/建计划**——"
            "请操作人员去主号四大主品计划手动拉取该达人；拉取后系统检测到会自动闭环。",
        ])
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            url, data=_json.dumps({"msgtype": "markdown",
                                   "markdown": {"content": md}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            body = _json.loads(r.read().decode("utf-8"))
        if body.get("errcode", 0) == 0:
            log.info("待主号拉取提醒已推送到「%s」群（%s）", dept or "汇总", cid)
        else:
            alert_outbox.enqueue("await_pull_notice", md, url)
    except Exception as exc:
        log.warning("待拉取提醒推送异常（不影响流程）: %s", str(exc)[:120])


def _push_pulled_to_group(simple: dict, detail: str, creator_name: str = "") -> None:
    """「主号已拉取」当场推给业务部门群（2026-10-13 用户：要体现给操作人员）——
    让业务员立刻知道该达人主号已在投，不用再跟。部门缺失/无群配置回退汇总群。
    绝不抛异常影响主流程。"""
    try:
        import daily_report_push as push_mod
        import alert_outbox
        hooks = push_mod.load_webhooks()
        if not hooks:
            log.warning("未配置 report_webhooks，主号已拉取通知未推送")
            return
        dept = (simple.get("业务部") or "").strip()
        group = dept if dept in hooks else "汇总"
        url = hooks.get(group) or hooks.get("汇总")
        if not url:
            return
        cid = simple.get("抖音号") or ""
        name = creator_name or cid
        md = "\n".join([
            f"**【主号已拉取】{name}（{cid}）**",
            f"部门：{dept or '未知'} ｜ 商品：{simple.get('商品ID') or ''}",
            f"{detail}",
            "此票不再授权建计划，业务员无需跟进。",
        ])
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            url, data=_json.dumps({"msgtype": "markdown",
                                   "markdown": {"content": md}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            body = _json.loads(r.read().decode("utf-8"))
        if body.get("errcode", 0) == 0:
            log.info("主号已拉取通知已推送到「%s」群（%s）", group, name)
        else:
            log.warning("主号已拉取推送失败: %s", body)
            alert_outbox.enqueue("main_pulled_notice", md, url)
    except Exception as exc:
        log.warning("主号已拉取推送异常（不影响流程）: %s", str(exc)[:120])


def _push_failure_to_group(payload: dict) -> None:
    """终态失败反推到对应业务部门群（2026-09-29 用户口径：分群推送，
    哪个部门的单进哪个部门的群；部门缺失/无群配置回退汇总群）。
    绝不抛异常影响主流程。"""
    try:
        import daily_report_push as push_mod
        import alert_outbox
        hooks = push_mod.load_webhooks()
        if not hooks:
            log.warning("未配置 report_webhooks，失败原因无法反推群里")
            return
        dept = (payload.get("department") or "").strip()
        group = dept if dept in hooks else "汇总"
        url = hooks.get(group) or hooks.get("汇总")
        if not url:
            log.warning("部门群/汇总群都无 webhook（部门=%s），失败原因未推送", dept)
            return
        sheet = _FAIL_PROGRESS_OPTIONS
        progress = fail_progress(sheet, payload.get("detail", "")) if sheet else ""
        now = datetime.datetime.now()
        md = "\n".join([
            f"**【计划/素材失败】{now:%m月%d日 %H:%M}**",
            f"> 达人：{payload.get('creator') or '-'}（{payload.get('department') or '-'}）",
            f"> 原因：{payload.get('reason')}",
            f"> 表格进度已标：{progress or '失败'}",
        ])
        done, left = push_mod.push_missing({group: url}, md, {group: md})
        if left:
            alert_outbox.enqueue(f"计划/素材失败-{group}", md, url)
        else:
            alert_outbox.flush()
    except Exception as exc:  # noqa: BLE001
        log.error("失败原因群推异常: %s", str(exc)[:150])

def write_status(**fields):
    """更新 status.json —— 「卡在哪」的实时答案；任何人随时可读。"""
    status = {}
    if os.path.exists(STATUS_FILE):
        try:
            status = json.load(open(STATUS_FILE, encoding="utf-8"))
        except Exception:
            status = {}
    status.update(fields)
    status["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    json.dump(status, open(STATUS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------

def acquire_instance_lock():
    """单实例锁：拿到返回文件对象（需保持引用），已被占用返回 None。"""
    fd = open(INSTANCE_LOCK_FILE, "a+b")
    try:
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        fd.close()
        return None


def _lock_fresh(path):
    try:
        return os.path.exists(path) and \
            (time.time() - os.path.getmtime(path)) < LOCK_STALE_SECONDS
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def build_params(record_simple, func, action_field, param_name):
    import inspect
    sig = inspect.signature(func).parameters
    params = {}
    for field, value in record_simple.items():
        pname = local_tools.FIELD_MAP.get(field, field)
        if pname in sig:
            params[pname] = value
    if action_field in record_simple:
        params.setdefault(param_name, record_simple[action_field])
    return params


def writeback(client, rid, progress_value):
    """把进度写回记录（单选字段，text 格式匹配已有选项）。"""
    cell = wecom_client.text_cell(progress_value)
    if cell is None:
        return
    try:
        client.update_records([{"record_id": rid, "values": {"进度": cell}}])
        log.info("回写进度: %s → %s", rid, progress_value)
    except Exception as e:
        log.error("回写失败 %s → %s: %s", rid, progress_value, e)


def fail_progress(sheet, msg):
    """终态失败按真实原因回写专属进度选项（2026-09-27 用户口径：
    表格上要直接看得出是佣金/素材/权限等哪种失败，不再一律写「失败」）。
    原因词缺失时回退通用「失败」。"""
    m = msg or ""
    if "佣金过高" in m or "CommissionBlocked" in m:
        return sheet.get("fail_commission") or sheet["fail"]
    if "短视频带货权限" in m:
        return sheet.get("fail_no_perm") or sheet["fail"]
    if "无推广权限" in m:
        return sheet.get("fail_no_perm") or sheet["fail"]
    if "被平台置灰" in m:
        return sheet.get("fail_auth_blocked") or sheet["fail"]
    # 「权限未生效，回退授权等待」多由勾选禁用/无推广权限触发（2026-10-06
    # 实证 rPiQhE/rqJMsk 兜底落「失败」看不出原因），归到无推广权限。
    if "权限未生效" in m or "回退授权" in m:
        return sheet.get("fail_no_perm") or sheet["fail"]
    # 勾选禁用但平台原因读不出来（重复计划/无权限分不清）——停自动重试，
    # 交人工核查，不落兜底「失败」（2026-10-06 rqJMsk/rPiQhE 误判教训）。
    if "待人工核查" in m:
        return "待业务员核查"
    # 抖音号不存在/未认证（企业机构号需先完成认证）——业务核实号码或让达人
    # 去抖音完成企业认证（2026-10-08 rIGhe6 案例，此前只能落光秃「失败」）。
    # 注意平台报错文案里同时含「达人合作码」字样，本条必须放在「合作码错误」之前。
    if "用户不存在" in m or "已认证的抖音号" in m:
        return "抖音号无效"
    if "合作码错误" in m:
        return sheet.get("fail_coop_code") or sheet["fail"]
    if "未知商品" in m:
        return sheet.get("fail_bad_product") or sheet["fail"]
    if "仍未生效" in m:
        return sheet.get("fail_creator_no_confirm") or sheet["fail"]
    return sheet["fail"]


def write_plan_registry(rid, simple, task_status, stage, detail="", event_status="info",
                        plan_label=None):
    """自动化运行信息只写入本地计划台账，绝不覆盖企业微信业务员备注。

    plan_label：表级通道标签（旧素材表「旧素材补投」，2026-10-09 用户定）；
    老计划新素材的 chase 标签只在无通道标签时兜底。
    """
    if not simple.get("抖音号"):
        return
    label = plan_label or None
    plan_id = None
    # 同时记录老计划目标和新建计划产物，供操作台台账/数据库追踪。
    if "chase_plan_id=" in detail:
        match = re.search(r"chase_plan_id=(\d+)", detail)
        if match:
            label = label or "老计划新素材"
            plan_id = match.group(1)
    if not plan_id:
        match = re.search(r"plan_id=(\d+)", detail)
        if match:
            plan_id = match.group(1)
    plan_name = None
    match = re.search(r"plan_name=(\S+)", detail)
    if match:
        plan_name = match.group(1)
    try:
        plan_db.upsert_plan_registry(
            source_record_id=rid,
            fields=simple,
            task_status=task_status,
            stage=stage,
            message=detail,
            plan_label=label,
            plan_id=plan_id,
            event_status=event_status,
        )
    except Exception:
        log.exception("计划台账写入失败: %s", rid)


def process_record(client, sheet, rec, state, args):
    """处理一条记录：模块回调落日志/状态；授权等待单独计时；失败计数重试。

    返回 "ok" / "fail" / "wait"。
    """
    rid = rec["record_id"]
    simple = wecom_client.simplify_record(rec)
    # 录入脏数据兜底:ID 类字段可能带尾随逗号/空格(09-03 rddyio "60235049081, "),
    # 不清洗会连授权页搜索都匹配不到行
    for _k in ("抖音号", "合作码", "商品ID"):
        if simple.get(_k):
            simple[_k] = simple[_k].strip().strip(",，").strip()
    scheduled_start = _scheduled_delivery_date(simple)
    if scheduled_start:
        # 预约日期只限制实际投放动作；授权和旧计划识别应提前完成。
        simple["_scheduled_start"] = scheduled_start.isoformat()
    log.info("处理记录 %s: %s", rid, json.dumps(simple, ensure_ascii=False))

    # 展示用标签：抖音任务用抖音号，视频号任务用作者关键词
    label = (simple.get("抖音号") or simple.get("作者") or "").strip()
    if label:
        state["labels"][rid] = label
    write_plan_registry(rid, simple, simple.get("进度") or "待处理", "开始处理")

    # 断点续跑：上次成功的模块本次跳过（不重复授权/建计划/扣费）
    skip = [s for s, st in state["steps"].get(rid, {}).items() if st == "ok"]
    if skip:
        log.info("断点续跑 %s，跳过已完成模块: %s", rid, "/".join(skip))

    wrote_auth_ok = [False]  # 本轮是否已回写过「可投放」（避免重复写）

    def step_cb(step_name, status, detail=""):
        steps = state["steps"].setdefault(rid, {})
        if status == "成功":
            steps[step_name] = "ok"
            # 当前模块恢复成功后，旧的瞬态错误不应继续在操作台显示为失败。
            state["errors"].pop(rid, None)
            # 成功细节落盘（如旧计划处理的 chase_plan_id），断点续跑时恢复
            state["step_notes"].setdefault(rid, {})[step_name] = detail[:300]
        elif status == "失败":
            steps[step_name] = "fail"
        elif status == "等待":
            steps[step_name] = "wait"   # 不算完成也不算失败，下轮从此模块重跑
        # 授权确认成功（两个授权都生效）→ 立即回写「可投放」，随后同轮接着建计划
        if status == "成功" and step_name == "授权确认" and sheet.get("auth_ok") \
                and not wrote_auth_ok[0] and simple.get("进度") != sheet["auth_ok"]:
            writeback(client, rid, sheet["auth_ok"])
            wrote_auth_ok[0] = True
        write_status(sheet=sheet["name"], record_id=rid,
                     step=f"{step_name} · {status}", note=detail[:300])
        write_plan_registry(rid, simple, simple.get("进度") or "待处理",
                            step_name, detail, status)
        log.info("  模块[%s] %s %s", step_name, status, detail[:300])

    write_status(sheet=sheet["name"], record_id=rid, step="开始处理", note="")
    # 旧素材表（stale_material）：素材不卡 72 小时，用达人最新的挂车视频
    # （2026-10-08 用户拍板接入，仅素材闸与主表不同）。仅本记录处理期间置
    # TOULIU_ALLOW_STALE_ONCE=1，子进程（plan_create/plan_sweep）经 env 继承；
    # finally 恢复原值，主表记录绝不受污染。
    _stale = bool(sheet.get("stale_material"))
    # 单票破例名单（2026-10-08 用户令：「这些就先破例，他们这些达人就一条视频，
    # 明天给他们投放」）：stale_once_rids.json 里列出的 record_id 按旧素材口径
    # 放行最新素材一次。命中即移出名单（一次性），随处理随消耗，不留长期口子。
    try:
        _once_path = os.path.join(HERE, "stale_once_rids.json")
        _once = set(json.load(open(_once_path, encoding="utf-8"))) if os.path.exists(_once_path) else set()
    except Exception:
        _once = set()
    if rid in _once:
        _stale = True
        log.info("记录 %s 命中单票破例名单，素材 72h 闸放行一次", rid)
    _old_env = os.environ.get("TOULIU_ALLOW_STALE_ONCE")
    if _stale:
        os.environ["TOULIU_ALLOW_STALE_ONCE"] = "1"
    try:
        try:
            status, msg = run_direct(simple, skip, step_cb,
                                     state["step_notes"].get(rid, {}),
                                     roi=sheet.get("roi"),
                                     plan_label=sheet.get("plan_label"))
        except Exception as e:
            status, msg = "fail", f"异常: {e}"
    finally:
        if _stale:
            if _old_env is None:
                os.environ.pop("TOULIU_ALLOW_STALE_ONCE", None)
            else:
                os.environ["TOULIU_ALLOW_STALE_ONCE"] = _old_env
    # 破例名单只在真正用到（建成 ok / 终态 fatal）后才消耗；等待夜窗/失败保留
    if rid in _once and status in ("ok", "fatal"):
        try:
            _once.discard(rid)
            json.dump(sorted(_once), open(_once_path, "w", encoding="utf-8"), ensure_ascii=False)
            log.info("记录 %s 破例已消耗（名单剩余 %d）", rid, len(_once))
        except Exception:
            pass
    log.info("记录 %s 结果: %s → %s", rid,
             {"ok": "成功", "fail": "失败", "wait": "等待授权", "fatal": "不可重试",
              "pulled": "主号已拉取"}.get(status, status),
             msg[:500])

    if status == "pulled":
        # 2026-10-14 事故防线：授权前复核命中主号拉取——终止本票，通知业务员。
        writeback(client, rid, "主号已拉取")
        write_plan_registry(rid, simple, "主号已拉取", "授权前复核", msg, "info")
        state["processed_ids"].append(rid)
        for key in ("attempts", "steps", "errors", "waiting_auth", "step_notes"):
            state[key].pop(rid, None)
        _push_pulled_to_group(simple, msg, "")
        return "ok"

    if status == "ok":
        writeback(client, rid, sheet["done"])
        write_plan_registry(rid, simple, sheet["done"], "处理完成", msg, "ok",
                            plan_label=sheet.get("plan_label"))
        state["processed_ids"].append(rid)
        for key in ("attempts", "steps", "errors", "waiting_auth", "step_notes"):
            state[key].pop(rid, None)
        return "ok"

    if status == "fatal":
        # 平台明确的业务拒绝（如个人号需认证企业机构号）：重试无意义，直接终结
        final = fail_progress(sheet, msg)
        writeback(client, rid, final)
        write_plan_registry(rid, simple, final, "处理终止", msg, "fatal")
        state["processed_ids"].append(rid)
        state["waiting_auth"].pop(rid, None)
        state["errors"][rid] = msg[:300]
        notify_failure(rid, simple, msg)
        log.error("记录 %s 不可重试的业务错误，直接标记「%s」，需人工处理: %s",
                  rid, final, msg[:200])
        return "fail"

    if status == "wait":
        now = time.time()
        waits = state["waiting_auth"]
        # 达人无该商品挂车视频：区别于「素材过72h要补新素材」——回写
        # 「没有挂车视频」，停止自动重试，达人发视频后人工改回可投放
        # （2026-10-06 用户口径：没有视频的不该标补新素材）。
        if "暂无该商品挂车视频" in msg:
            progress = "没有挂车视频"
            writeback(client, rid, progress)
            waits.pop(rid, None)
            state["errors"].pop(rid, None)
            write_status(step="没有挂车视频", note=msg[:300])
            write_plan_registry(rid, simple, progress, "达人无挂车视频", msg, "wait")
            log.info("记录 %s 因达人无挂车视频回写「%s」，等达人发布", rid, progress)
            return "wait"
        # 2026-10-15 用户令「不能去授权了」：21 点自动授权废除——任何路径冒出
        # 「等待21点进行授权」一律改写「待授权」（=待主号拉取），不挂 21:00 唤醒。
        if "等待21点进行授权" in msg:
            progress = "待授权"
            writeback(client, rid, progress)
            state["errors"].pop(rid, None)
            waits.pop(rid, None)
            write_status(step="待授权", note=("等主号拉取，系统不授权。" + msg[:260]))
            write_plan_registry(rid, simple, progress, "托管型达人-不再授权", msg, "info")
            log.info("记录 %s 托管型改写「待授权」（21点自动授权已废除）", rid)
            return "wait"
        # 素材 72h 硬闸：停止自动重试，回写专属状态，等业务员补新素材后手动改回可投放。
        if "待业务员补新素材" in msg:
            progress = sheet.get("material_wait") or sheet.get("auth_ok") or "可投放"
            writeback(client, rid, progress)
            waits.pop(rid, None)
            state["errors"].pop(rid, None)
            write_status(step="待业务员补新素材", note=msg[:300])
            write_plan_registry(rid, simple, progress, "素材72小时硬闸", msg, "wait")
            log.info("记录 %s 因素材超过72小时回写「%s」，等待业务员补素材", rid, progress)
            return "wait"
        # 建计划夜间窗口挂起：23:00 分类完成后，才允许执行至次日 07:59。
        # 不算授权等待、不计放弃时限，下一个 00:01 自动续跑
        if "等夜间窗口建计划" in msg:
            now_dt = datetime.datetime.now()
            if now_dt.hour >= 23 or now_dt.hour < 8:
                # 已到夜间窗口但分类尚未完成，短周期复查分类门，不抢跑建计划。
                next_check = now + round_cadence_seconds()
                next_note = "等待 23:00 分类收口完成，15 分钟后复查"
            else:
                next_check = datetime.datetime.combine(
                    datetime.date.today(), datetime.time(23, 1)).timestamp()
                next_note = "23:00 分类收口完成后续跑"
            waits[rid] = {"since": now, "next_check": next_check,
                          "note": "等夜间窗口建计划（23:00-次日07:59，需分类完成）"}
            # 授权已走完、只等夜间窗口的记录回写「可投放」：进度列反映真实
            # 状态，不再挂着「待处理」误导（2026-09-30 用户指认一整天待处理
            # 不减少——实际是标签滞后，不是没处理）。
            if sheet.get("auth_ok") \
                    and state["steps"].get(rid, {}).get("授权确认") == "ok" \
                    and simple.get("进度") != sheet["auth_ok"]:
                writeback(client, rid, sheet["auth_ok"])
            write_status(step="等夜间窗口建计划", note=next_note)
            write_plan_registry(rid, simple, simple.get("进度") or "可投放",
                                "等待建计划窗口", msg, "wait")
            log.info("记录 %s 等待 23:00 分类收口完成后续跑（断点已保存）", rid)
            return "wait"
        # 授权待达人确认：不算失败、不消耗重试次数，每 30 分钟复查一次
        since = (waits.get(rid) or {}).get("since", now)
        waited_h = (now - since) / 3600
        if now - since > AUTH_GIVEUP_SECONDS:
            final = fail_progress(sheet, msg)
            writeback(client, rid, final)
            state["processed_ids"].append(rid)
            waits.pop(rid, None)
            log.error("记录 %s 等待授权 %.1f 小时仍未生效，标记「%s」，需人工处理",
                      rid, waited_h, final)
            return "fail"
        next_check = now + retry_interval_seconds()
        waits[rid] = {"since": since, "next_check": next_check, "note": msg[:200]}
        # 回写中间状态「等待达人接受」，表格上直接看得出卡在授权确认；
        # 等挂车视频属于「可投放」的正常待料状态，不改表格进度（2026-08-22）；
        # 「挂起」类（托管探测失败等系统侧重试）也不写等待达人接受——授权根本没
        # 提交，写它会让业务员以为球在达人那边（2026-10-09 用户指认误导）。
        if sheet.get("auth_wait") and simple.get("进度") != sheet["auth_wait"] \
                and "挂车视频" not in msg and "挂起" not in msg:
            writeback(client, rid, sheet["auth_wait"])
        clock = time.strftime("%H:%M", time.localtime(next_check))
        write_status(step="等待授权", note=f"已等待{waited_h:.1f}小时，{clock}复查")
        write_plan_registry(rid, simple,
                            "系统挂起重试" if "挂起" in msg
                            else (sheet.get("auth_wait") or "等待达人接受"),
                            "等待授权", msg, "wait")
        log.info("记录 %s 授权待达人确认：已等待 %.1f 小时，%s 复查（模块断点已保存）",
                 rid, waited_h, clock)
        return "wait"

    # 基础设施瞬态（标签页上限/CDP 连接失败等）不消耗重试次数、不烧终态：
    # 与记录本身无关，等环境恢复下轮重试即可。2026-09-30 教训：探针把标签顶到
    # 7 页上限，rN2aCT/rQ0gp3 的第 3 次「失败」实为 SessionPrepareError
    # 标签上限，被 MAX_ATTEMPTS 误烧成终态「失败」。
    if any(k in (msg or "") for k in _INFRA_TRANSIENT):
        state.setdefault("last_attempt", {})[rid] = time.time()
        state["errors"][rid] = msg[:300]
        write_plan_registry(rid, simple, simple.get("进度") or "待处理",
                            "环境瞬态等待", msg, "wait")
        log.warning("记录 %s 遇基础设施瞬态（不消耗重试次数），下轮重试: %s",
                    rid, msg[:160])
        return "fail"
    attempts = state["attempts"].get(rid, 0) + 1
    state["attempts"][rid] = attempts
    state.setdefault("last_attempt", {})[rid] = time.time()
    state["errors"][rid] = msg[:300]
    if "勾选禁用" in (msg or ""):
        # 勾选禁用=平台说「可能已有重复计划」，与旧计划扫描的「匹配0条」结论直接
        # 矛盾——信平台不信缓存：作废旧计划扫描断点，下轮用 30 天窗重扫认领
        # （2026-10-09 rL9gR5 实证：首轮扫描瞬态漏行→建计划三连撞→误判待核查，
        # 手动重扫一把 chase 成功；历史上大量「待业务员核查」同根因）。
        steps = state["steps"].get(rid) or {}
        dropped = [k for k in ("旧计划预检", "旧计划处理") if k in steps]
        for k in dropped:
            steps.pop(k, None)
            state["step_notes"].get(rid, {}).pop(k, None)
        if dropped:
            log.warning("记录 %s 勾选禁用与旧计划扫描结论矛盾，作废 %s 下轮重扫",
                        rid, "/".join(dropped))
    # 素材硬闸的失败形态（建计划模块 rc=1 但语义=等业务员补素材）：
    # 与 wait 分支同口径——回写「待业务员补新素材」，不占重试次数（2026-10-15 ruhKDA）。
    if "待业务员补新素材" in msg:
        progress = sheet.get("material_wait") or "待业务员补新素材"
        writeback(client, rid, progress)
        waits.pop(rid, None)
        state["errors"].pop(rid, None)
        state["attempts"].pop(rid, None)
        write_status(step="待业务员补新素材", note=msg[:300])
        write_plan_registry(rid, simple, progress, "素材72小时硬闸", msg, "wait")
        log.info("记录 %s 素材不足回写「%s」（失败形态归并），等待业务员补素材", rid, progress)
        return "wait"
    # 只有最终失败或不可重试时才通知，普通瞬态失败等下轮重试后再汇总
    if attempts >= MAX_ATTEMPTS:
        final = fail_progress(sheet, msg)
        writeback(client, rid, final)
        write_plan_registry(rid, simple, final, "处理失败", msg, "fail")
        state["processed_ids"].append(rid)
        notify_failure(rid, simple, msg)
        log.error("记录 %s 已重试 %d 次仍失败，标记「%s」，需人工处理: %s",
                  rid, attempts, final, msg[:200])
    else:
        # 授权已生效但建计划/追投失败：回写「可投放」，下轮只重跑失败模块
        if sheet.get("auth_ok") \
                and state["steps"].get(rid, {}).get("授权确认") == "ok" \
                and simple.get("进度") != sheet["auth_ok"]:
            writeback(client, rid, sheet["auth_ok"])
        write_plan_registry(rid, simple, sheet.get("auth_ok") or simple.get("进度") or "待处理",
                            "等待重试", msg, "fail")
        log.warning("记录 %s 第 %d 次失败，下轮从失败模块续跑（不重跑已成功模块）: %s",
                    rid, attempts, msg[:200])
    return "fail"


def run_direct(record_simple, skip_steps, step_cb, step_notes=None, roi=None,
               plan_label=None):
    """调用 local_tools 中登记的函数，返回 (status, msg)，status ∈ ok/fail/wait/fatal。

    roi：表级目标 ROI（旧素材表 6.5，2026-10-09 用户规则）；缺省走新计划标准 4.5。
    plan_label：表级通道标签（旧素材表「旧素材补投」），落台账/恢复队列/看板。
    """
    for action_field, (func_name, param_name) in local_tools.ACTIONS.items():
        if action_field not in record_simple:
            continue
        func = getattr(local_tools, func_name)
        params = build_params(record_simple, func, action_field, param_name)
        params["skip_steps"] = skip_steps
        params["step_cb"] = step_cb
        params["step_notes"] = step_notes or {}
        if roi:
            params["roi"] = roi
        if plan_label:
            params["plan_label"] = plan_label
        return func(**params)
    return "fail", "[跳过] 无匹配的触发字段"


def _scheduled_delivery_date(simple):
    """记录任意字段含「M/D投」式备注（如 "8/18投"、"8.18投"、"8月18日投"、"8.13开投"）
    → 返回预约投放日期；没有返回 None。

    2026-08-17 教训：有记录在「业务员」栏写 "8/18投" 当备注，系统把字段当
    业务员读、当天直接建计划发布（白烧一天预算）。约定：任何字段出现这种
    日期+投 的写法即视为预约投放日；授权、授权确认和旧计划识别仍可提前执行，
    仅建计划与新增素材必须等到该日夜间窗口。兼容「开投」写法。
    """
    today = datetime.date.today()
    for v in simple.values():
        m = re.search(r"(\d{1,2})\s*[./月]\s*(\d{1,2})\s*日?\s*开?投", str(v or ""))
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            try:
                return datetime.date(today.year, month, day)
            except ValueError:
                return None
    return None


def process_sheet(client, sheet, state, args):
    if sheet["name"] in ("抖音任务", "旧素材抖音任务"):
        import main_account_flow
        return main_account_flow.process_sheet(client, sheet, state, args)
    client.cfg["sheet_id"] = sheet["sheet_id"]
    records = client.get_all_records()
    log.info("—— [%s] 共 %d 条 ——", sheet["name"], len(records))

    processed_ids = set(state["processed_ids"])
    waits = state["waiting_auth"]
    pending = sheet["pending"]
    now = time.time()
    waiting_n = 0

    def reactivate(rid, reason):
        """重新激活一条已终结的记录：清重试计数/等待计时，保留模块断点续跑。"""
        processed_ids.discard(rid)
        state["processed_ids"] = [x for x in state["processed_ids"] if x != rid]
        state["attempts"].pop(rid, None)
        state["errors"].pop(rid, None)
        waits.pop(rid, None)
        kept = [s for s, st in state["steps"].get(rid, {}).items() if st == "ok"]
        log.warning("记录 %s %s：重新激活，%s",
                    rid, reason,
                    f"保留断点[{ '/'.join(kept) }]，从未完成模块续跑" if kept
                    else "无断点，从头开始")

    def should_process(rec):
        nonlocal waiting_n
        simple = wecom_client.simplify_record(rec)
        if not simple:
            return False
        rid = rec["record_id"]
        # --retry 单条重试：其他记录一律不碰
        only = getattr(args, "retry", None)
        if only and rid != only:
            return False
        prog = simple.get("进度", "")
        # 授权闸（2026-10-13 用户规则「填表先不要授权，需要有个中间量」+ 授权前三查
        # 「托管开了吗/佣金对吗/主号拉取了吗」）：
        # 全新票（业务员刚填的待处理/空进度，无任何处理痕迹）先做授权前预检再分流：
        #   主号已拉取 → 「主号已拉取」冻结；佣金超限 → 「佣金过高」；托管型 →
        #   「等待21点进行授权」（原托管路径）；探测失败 → 保持待处理挂起重试；
        #   全过 → 「待授权」等人工改「允许授权」放行。
        # 有处理痕迹的票（断点续跑/人工翻回重试）和 --force/--retry 手动操作不受此闸。
        if prog in ("", "待处理") and not args.force and not only:
            fresh = (rid not in processed_ids
                     and not state["attempts"].get(rid)
                     and not state["steps"].get(rid)
                     and rid not in waits)
            if fresh:
                route, detail, pulled_name = local_tools.preauth_gate_check(
                    simple.get("抖音号", ""), simple.get("商品ID", ""),
                    simple.get("业务部", ""))
                log.info("记录 %s 授权前预检: %s（%s）", rid, route, detail[:120])
                if route == "retry":
                    waits[rid] = {"since": now,
                                  "next_check": now + retry_interval_seconds(),
                                  "note": detail[:200]}
                    write_plan_registry(rid, simple, "待处理", "授权前预检挂起", detail, "wait")
                    return False
                if route == "pulled":
                    writeback(client, rid, "主号已拉取")
                    write_plan_registry(rid, simple, "主号已拉取", "授权前预检", detail, "info")
                    _push_pulled_to_group(simple, detail, pulled_name)
                    return False
                if route == "commission":
                    writeback(client, rid, sheet.get("fail_commission") or "佣金过高")
                    write_plan_registry(rid, simple, "佣金过高", "授权前预检", detail, "fail")
                    return False
                if route == "managed":
                    # 2026-10-15 用户令「不能去授权了」：托管型也不再等 21 点自动授权，
                    # 统一落「待授权」（=待主号拉取；主号自动投会自己发起托管授权）。
                    writeback(client, rid, "待授权")
                    write_plan_registry(rid, simple, "待授权", "授权前预检",
                                        detail + "（托管型，等主号拉取，系统不授权）", "info")
                    return False
                writeback(client, rid, "待授权")
                log.info("记录 %s 新票进授权闸 → 「待授权」（=待主号拉取；提醒只走日报三档，不当场推）",
                         rid, simple.get("抖音号") or "?")
                return False
        # 新模式（2026-10-15 用户令）：填表后系统【不再授权、不再建计划】，
        # 由操作人员手动去主号四大主品计划拉取；系统只盯快照——发现主号已拉取
        # 就把票翻成「主号已拉取」闭环并通知。每轮直读当天快照（不跑浏览器）。
        if prog == "待授权" and not args.force and not only:
            try:
                hit = local_tools._main_pull_fresh_hit(simple.get("抖音号", ""), simple.get("商品ID", ""))
            except Exception as exc:
                log.warning("记录 %s 拉取盯梢异常（保持待授权）: %s", rid, str(exc)[:100])
                return False
            if hit:
                writeback(client, rid, "主号已拉取")
                write_plan_registry(rid, simple, "主号已拉取", "拉取盯梢", hit, "info")
                _push_pulled_to_group(simple, hit, "")
            return False
        # 进度过滤永远优先：已投放/已完成的记录任何情况（含 --force/--retry）都不重跑，防重复扣费
        if prog in pending:
            if rid in processed_ids:
                # 已终结（成功/失败封顶）但进度被人工改回「待处理」→ 从断点复活
                reactivate(rid, "进度被人工改回「待处理」")
            w = waits.get(rid)
            if w and not args.force and not only and now < w.get("next_check", 0):
                waiting_n += 1
                log.info("  %s 等待授权中（%s 复查），本轮跳过",
                         rid, time.strftime("%H:%M", time.localtime(w["next_check"])))
                return False
            if only and rid in waits:
                log.info("手动重试 %s：提前解除授权等待（原复查时间 %s）", rid,
                         time.strftime("%H:%M", time.localtime(waits[rid].get("next_check", 0))))
                waits.pop(rid, None)
            # 断点重试间隔：失败过的记录每 30 分钟才重试一次；首次处理的新记录随到随处理。
            if state["attempts"].get(rid) and not args.force and not only:
                last_att = state.get("last_attempt", {}).get(rid, 0)
                if now - last_att < retry_interval_seconds():
                    log.info("  %s 距上次尝试不足 %d 分钟（断点重试间隔），本轮跳过",
                             rid, retry_interval_seconds() // 60)
                    return False
            return True
        if prog == sheet["fail"] and (args.force or only):
            reactivate(rid, "手动重试" if only else f"--force 捞回「{sheet['fail']}」")
            return True
        return False

    todo = [r for r in records if should_process(r)]
    # 公平轮转：按「上次尝试时间」升序（从未尝试=0 排最前）。原按 attempts
    # 升序会让不消耗次数的等待类记录（挂车视频）永远霸占每轮名额，失败过
    # 的记录（attempts 高）永久饿死——2026-09-30 实证 r36YYr 整夜 0 次处理
    # （20 条排队、每轮 5 条，它 attempts=3 永远排在第 16 位）。
    # 2026-10-08 用户口径：「待处理」新票永远优先，授权复查/可投放等老票排后。
    # 原公平轮转只按 last_attempt 升序，复查票（等待达人接受/可投放）会和新票
    # 混排抢每轮 5 个名额。
    def _prio(r):
        prog = (wecom_client.simplify_record(r) or {}).get("进度", "")
        fresh = 0 if prog in ("", "待处理") else 1
        return (fresh, state.get("last_attempt", {}).get(r["record_id"], 0))
    todo.sort(key=_prio)
    overflow = max(0, len(todo) - MAX_TASKS_PER_RUN)
    todo = todo[:MAX_TASKS_PER_RUN]
    log.info("[%s] 待处理 %d 条（本轮处理 %d 条，等待授权 %d 条）",
             sheet["name"], len(todo) + overflow, len(todo), waiting_n)
    if overflow:
        log.warning("[%s] 超出每轮上限，%d 条留待下轮", sheet["name"], overflow)

    if args.dry_run:
        for rec in todo:
            simple = wecom_client.simplify_record(rec)
            rid = rec["record_id"]
            tried = state["attempts"].get(rid, 0)
            done_steps = [s for s, st in state["steps"].get(rid, {}).items() if st == "ok"]
            log.info("  [dry-run] 将处理 %s（已试 %d 次，断点: %s）: %s",
                     rid, tried, "/".join(done_steps) or "无",
                     json.dumps(simple, ensure_ascii=False))
        return 0, 0, 0

    ok_n = fail_n = wait_n = 0
    for rec in todo:
        # 大节点播报扫描优先：记录之间让行，扫描结束再继续（单条记录流程
        # 可能跑几分钟，做不到秒级 preempt，只能保证不连续占浏览器）
        wait_report_idle()
        r = process_record(client, sheet, rec, state, args)
        if r == "ok":
            ok_n += 1
        elif r == "wait":
            wait_n += 1
        else:
            fail_n += 1
        save_state(state)
    log.info("[%s] 本轮完成：成功 %d，失败 %d，等待授权 %d",
             sheet["name"], ok_n, fail_n, wait_n)
    # 21点放行复核汇总播报（2026-10-14 用户：21点扫描怎么没有播报）
    if _release21_batch:
        lines = [f"**【21点授权复核】{datetime.datetime.now():%m月%d日 %H:%M}**"]
        released = [b for b in _release21_batch if b["action"] == "released"]
        pulled = [b for b in _release21_batch if b["action"] == "pulled"]
        if released:
            lines.append(f"**未被拉取，已放行授权 {len(released)}**：")
            lines += [f"- {b['cid']}·{b['dept']}" for b in released]
        if pulled:
            lines.append(f"**已被主号拉取，冻结 {len(pulled)}**：")
            lines += [f"- {b['cid']}·{b['dept']}" for b in pulled]
        _release21_batch.clear()
        try:
            import daily_report_push as push_mod
            import json as _json
            import urllib.request
            hooks = push_mod.load_webhooks()
            url = (hooks or {}).get("汇总")
            if url:
                req = urllib.request.Request(
                    url, data=_json.dumps({"msgtype": "markdown",
                                           "markdown": {"content": "\n".join(lines)}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    log.info("21点放行复核汇总已推汇总群: %s", resp.read().decode("utf-8")[:80])
        except Exception as exc:
            log.warning("21点放行复核播报失败（不影响流程）: %s", str(exc)[:100])
    return ok_n, fail_n, wait_n


def automation_enabled(key: str = "enabled", default: bool = False):
    """自动化开关（投流操作台 UI 控制）。缺文件/读取失败按关闭处理。

    key: enabled=总开关；video_enabled=视频号任务表开关（分表暂停用）。
    """
    try:
        with open(AUTOMATION_FLAG_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get(key, default))
    except Exception:
        return default


def _growth_state() -> dict:
    """每日增长动作（达标追投/预算不限）的共享门闩，与投流操作台共用一份：
    谁先触发谁执行，另一边看到 chase_done 就跳过（追投平台侧也幂等）。"""
    path = os.path.join(HERE, "growth_state.json")
    try:
        st = json.load(open(path, encoding="utf-8"))
    except Exception:
        st = {}
    today = datetime.date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "budget_done": False, "budget_raised": []}
    else:
        # 兼容旧状态文件：旧版 chase_done 不能阻止新版当天执行预算检查。
        st.setdefault("budget_done", False)
        st.setdefault("budget_raised", [])
    return st


def _save_growth_state(st: dict) -> None:
    path = os.path.join(HERE, "growth_state.json")
    json.dump(st, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _daily_growth_job(client, state):
    """每日自动增长动作：仅将 ROI>10 且订单>10 计划的预算设为不限。

    config.json: growth_time "10:00"（到点后第一轮执行，默认 10:00）。
    达标自动追投已关闭。与投流操作台的扫描后钩子共用 growth_state.json 门闩，每天只执行一次；
    失败不标记，下轮重试。
    """
    if not automation_enabled("growth_enabled"):
        log.info("每日增长动作开关已关闭，跳过自动追投和预算调整")
        return

    hhmm = str(client.cfg.get("growth_time") or "10:00")
    try:
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
    except Exception:
        hh, mm = 10, 0
    now = datetime.datetime.now()
    if (now.hour, now.minute) < (hh, mm):
        return
    st = _growth_state()
    if st.get("budget_done"):
        return

    log.info("到每日自动增长时间（%s）：检查 ROI>10 且订单>10 计划并调整预算不限…", hhmm)
    write_status(step="每日预算调整")
    ok, data = local_tools.run_budget_raise()
    if ok:
        log.info("预算调整: %s", data.get("message"))
        st["budget_done"] = True
        raised = data.get("raised") or []
        if raised:
            st["budget_raised"] = sorted(set(st.get("budget_raised", [])) | set(raised))
    else:
        log.warning("预算调整失败（下轮重试）: %s", data)
        write_status(step="", note=f"预算调整失败，下轮重试: {str(data)[:120]}")
        return
    _save_growth_state(st)
    write_status(step="", note=f"每日预算调整完成: {data.get('message', '')}")


def plan_power_busy() -> bool:
    """计划启停任务（23:00 收口/12:00 恢复等）运行中：本轮跳过，避免抢同一个 Edge。

    plan_power 操作浏览器前会写 plan_power_busy.json，结束删除；崩溃残留靠
    45 分钟时效兜底（收口正常 10-30 分钟内完成）。

    2026-10-08 起忙标带写入方 pid：进程已死立即按残留处理（连续三天档位
    崩在数据库复核 2/3、忙标残留堵轮询最长 45 分钟）；老格式无 pid 才退回
    时效兜底。
    """
    try:
        payload = json.load(open(os.path.join(HERE, "plan_power_busy.json"), encoding="utf-8"))
    except Exception:
        return False
    pid = payload.get("pid")
    if pid:
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                log.warning("plan_power 忙标残留：pid %s 已退出（action=%s），按失效放行", pid, payload.get("action"))
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            pass  # 探测失败退回时效判断
    try:
        return time.time() - float(payload.get("ts", 0)) < 45 * 60
    except Exception:
        return False


REPORT_SLOTS = ("08:00", "10:00", "12:00", "14:00", "16:00", "17:00", "22:30")


def report_busy() -> bool:
    """大节点播报扫描运行中（2026-09-01 用户定：扫描优先，其他程序让行）。

    daily_report_push 浏览器扫描期间写 report_busy.json，扫描结束即删；
    崩溃残留靠 15 分钟时效兜底（正常扫描 4-6 分钟）。
    """
    try:
        payload = json.load(open(os.path.join(HERE, "report_busy.json"), encoding="utf-8"))
        return time.time() - float(payload.get("ts", 0)) < 15 * 60
    except Exception:
        return False


def near_report_slot(minutes: int = 8) -> bool:
    """距下一播报档不足 minutes 分钟：不开新轮，避免长记录流程撞进扫描窗口。"""
    now = datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    for slot in REPORT_SLOTS:
        hh, mm = int(slot[:2]), int(slot[3:])
        if 0 <= (hh * 60 + mm) - cur <= minutes:
            return True
    return False


def wait_report_idle(max_wait_s: float = 15 * 60) -> None:
    """播报扫描进行中时等待其结束再继续（记录处理中途让行用）。"""
    deadline = time.monotonic() + max_wait_s
    while report_busy():
        if time.monotonic() >= deadline:
            log.warning("等播报扫描结束超时，继续执行（忙标可能残留）")
            return
        log.info("大节点播报扫描中，本轮处理让行 20s…")
        time.sleep(20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-limit", type=int, default=10, help="主号每表本轮上限，0为处理全部候选")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry", metavar="RECORD_ID",
                    help="手动重试单条记录：提前解除授权等待/捞回失败，从断点续跑")
    args = ap.parse_args()

    state = load_state()
    # 固定节奏：计划任务每 15 分钟触发一次；手动 --force/--retry/--dry-run 不受限。
    if not (args.force or args.retry or args.dry_run):
        last_round = state.get("last_round_ts", 0)
        cadence = round_cadence_seconds()
        if time.time() - last_round < cadence:
            log.info("距上一轮不足 %d 分钟（轮询间隔），本轮跳过", cadence // 60)
            return
    # 白名单表格同步（2026-09-07）：主管在企微表格「人工白名单」页维护，poll 每轮
    # 轮首同步到本地缓存，manual_whitelist 判定核心读 [底账+缓存] 合并生效。
    # 纯企微 API + 本地写，不占浏览器——放在浏览器忙标让行判断之前，收口/让行时段
    # 照同步（主管 22:50 改表，23:00 收口新进程能读到最新口径）。失败沿用上次缓存。
    if not args.dry_run:
        try:
            import whitelist_sync
            _wr = whitelist_sync.sync_whitelist_from_sheet()
            if _wr.get("ok"):
                log.info("白名单同步：启用 %d 条，停用墓碑 %d，冲突 %d",
                         _wr.get("enable", 0), _wr.get("deny", 0) + _wr.get("kill", 0),
                         _wr.get("conflicts", 0))
            elif not _wr.get("skipped"):
                log.warning("白名单同步失败（沿用上次缓存）: %s", _wr.get("error"))
        except Exception:
            log.exception("白名单同步异常（不影响本轮）")
    # 启停任务（收口/恢复）持有浏览器期间，整轮跳过，下轮自然重试。
    if not args.dry_run and plan_power_busy():
        log.info("计划启停任务运行中（plan_power_busy），本轮跳过，避免抢浏览器")
        return
    # 大节点播报扫描优先（2026-09-01 用户定）：扫描中让行；临近档位不开新轮，
    # 避免多分钟的记录流程撞进扫描窗口。手动 --force/--retry/--dry-run 不受限。
    if not (args.force or args.retry or args.dry_run):
        if report_busy():
            log.info("大节点播报扫描中（report_busy），本轮让行跳过")
            return
        if near_report_slot():
            log.info("临近播报档（±8 分钟），本轮让行，不耽误大节点扫描")
            return
    client = wecom_client.WeComClient()
    sheets = [
        {"sheet_id": client.cfg.get("sheet_id"), "name": "抖音任务",
         # 2026-10-15 用户令：新票系统不自动授权（预检→待授权→盯主号拉取）；
         # 但操作人员手工改「允许授权」的票放行走授权全流程（用户原话
         # 「等待他们手工填写到允许授权你才能去授权」）——pending 保留完整链路。
         "pending": ["", "待处理", "允许授权", "等待达人接受", "可投放"],
         "done": "已投放", "fail": "失败",
         "auth_wait": "等待达人接受", "auth_ok": "可投放",
         "material_wait": "待业务员补新素材",
         "fail_commission": "佣金过高", "fail_no_perm": "达人无推广权限",
         "fail_auth_blocked": "授权被平台拦截", "fail_coop_code": "合作码错误",
         "fail_bad_product": "商品ID无效", "fail_creator_no_confirm": "达人未确认授权"},
        {"sheet_id": client.cfg.get("old_material_sheet_id"), "name": "旧素材抖音任务",
         "pending": ["", "待处理", "允许授权", "等待达人接受", "可投放"],  # 2026-10-15 同上：仅人工放行才授权
         # 旧素材通道的计划投放时 ROI 目标 6.5（2026-10-09 用户规则）；
         # 透传到建计划 --roi、暂停待发队列与 08:00 归一，全程不打回 4.5。
         "roi": 6.5,
         # 通道标签：台账/恢复队列/看板统一标「旧素材补投」（2026-10-09 用户定）。
         "plan_label": "旧素材补投",
         "done": "已投放", "fail": "失败",
         "auth_wait": "等待达人接受", "auth_ok": "可投放",
         "material_wait": "待业务员补新素材",
         "fail_commission": "佣金过高", "fail_no_perm": "达人无推广权限",
         "fail_auth_blocked": "授权被平台拦截", "fail_coop_code": "合作码错误",
         "fail_bad_product": "商品ID无效", "fail_creator_no_confirm": "达人未确认授权",
         "stale_material": True},
        {"sheet_id": client.cfg.get("video_sheet_id"), "name": "视频号任务",
         "pending": ["", "待处理"],
         "done": "已完成", "fail": "失败",
         "auth_wait": None, "auth_ok": None},
    ]

    if args.dry_run:
        log.info("=== dry-run（只列不执行）===")
        for sheet in sheets:
            if sheet["sheet_id"]:
                process_sheet(client, sheet, state, args)
        return

    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        log.warning("上一轮尚未结束（单实例锁占用），本轮退出")
        return
    try:
        if not automation_enabled():
            log.info("自动化总开关已关闭（投流操作台），本轮跳过")
            write_status(state="idle", step="", note="自动化已关闭")
            return
        log.info("=== 本轮开始 ===")
        state["last_round_ts"] = time.time()
        write_status(state="running", round_started=datetime.datetime.now().isoformat(timespec="seconds"),
                     sheet="", record_id="", step="", note="")
        if _lock_fresh(WEBUI_LOCK_FILE):
            log.info("操作台正在使用浏览器（browser.lock），本轮整体跳过")
            write_status(state="idle", step="", note="跳过：操作台占用浏览器")
            return
        # 持有轮询侧浏览器锁：操作台任务发现后会排队等待
        json.dump({"pid": os.getpid(), "since": time.time()},
                  open(POLL_LOCK_FILE, "w", encoding="utf-8"))
        try:
            total_ok = total_fail = total_wait = 0
            for sheet in sheets:
                if not sheet["sheet_id"]:
                    log.warning("[%s] 未配置 sheet_id，跳过", sheet["name"])
                    continue
                if sheet["name"] == "视频号任务" and not automation_enabled("video_enabled"):
                    log.info("[%s] 分表开关已关闭，本轮跳过", sheet["name"])
                    continue
                write_status(sheet=sheet["name"], step="读取记录")
                ok, fail, wait = process_sheet(client, sheet, state, args)
                total_ok += ok
                total_fail += fail
                total_wait += wait
            _daily_growth_job(client, state)
            # 白名单「全天跑」计划预算监控 + 潜力/重点 30 分钟独立播报
            # （2026-09-01 恩豆妈咪/分类监控需求）：播报档 ±15 分钟内自动跳过；
            # 仍在轮询浏览器锁内，互斥纪律不变。播报与任务通道共享状态，不重复推。
            try:
                import budget_watch
                budget_watch.check_live()
                # 2026-09-03 用户定：监控只监控——告警触发的自动加预算队列
                # 不再在轮末执行（队列机制保留为手动备用，python budget_watch.py）
            except Exception:
                log.exception("预算监控本轮失败（不影响轮询主流程）")
            try:
                import plan_focus_report
                plan_focus_report.maybe_run()
            except Exception:
                log.exception("潜力/重点监控播报本轮失败（不影响轮询主流程）")
            try:
                # 夜间守望（2026-09-02 用户：「别漏暂停了、误启动」）：00:00-07:50
                # 除重点外凡在投一律补停挂票；白天窗口内自动空转。
                import night_watch
                night_watch.maybe_run()
            except Exception:
                log.exception("夜间守望本轮失败（不影响轮询主流程）")
            save_state(state)
            log.info("=== 本轮结束：成功 %d，失败 %d，等待授权 %d ===",
                     total_ok, total_fail, total_wait)
            write_status(state="idle", step="", record_id="",
                         note=f"上轮成功{total_ok} 失败{total_fail} 等待授权{total_wait}")
        finally:
            try:
                os.remove(POLL_LOCK_FILE)
            except OSError:
                pass
    finally:
        instance_lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw 直跑没有控制台，未捕获异常会无声死掉（日志只剩「本轮开始」）。
        # 落日志 + 写 status.json，操作台「自动化」页能直接看到死因。
        log.exception("本轮异常退出")
        msg = traceback.format_exc(limit=3)
        short = ""
        for line in reversed(msg.strip().splitlines()):
            if line.strip():
                short = line.strip()[:200]
                break
        if "60020" in msg or "not allow to access from your ip" in msg:
            short = ("企微API拒绝：本机公网IP不在应用可信IP名单（60020），"
                     "请在企微管理后台添加可信IP后重试")
        try:
            write_status(state="error", step="", note=short)
        except Exception:
            pass
        raise SystemExit(1)
