# -*- coding: utf-8 -*-
"""计划生命周期的 23:00 收口。

规则：
- 23:00 先读取当日指标并完成分类；重点计划为 ROI > 10 且订单数 >= 10。
- 分类完成后，23:00-次日 07:59 才可创建新计划或给旧计划加素材。
- 23:00 仅保留重点计划投放，其余非白名单计划暂停。人工白名单一律不由 AI 改动。
- 其中实际暂停的老计划池计划在次日 12:00 单独恢复；重点、潜力、新计划不随此恢复。

选择器（2026-08-18 实机探测验证）：
- 行内开关：计划列 cell[1] 里 .oc-switch.oc-switch--dark（内层 .ovui-switch，
  开 = ovui-switch--checked）
- 全选：table thead th 里 label.ovui-checkbox
- 批量栏：勾选后出现「已选N条 / 开启 / 暂停 / 删除 / 修改预算 / 修改ROI目标」

Usage:
    python -m 巨量千川.plan_power monitor [--dry-run]
    python -m 巨量千川.plan_power close-old-pool [--dry-run]
    python -m 巨量千川.plan_power resume-old-pool [--dry-run]
    python -m 巨量千川.plan_power status
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .plan_metrics import (
    _ensure_cdp,
    _department_overview_page,
    _goto_next_page,
    _open_overall,
    _parse_plan_row,
    _read_pagination_total,
    _search_plans,
    list_all_plans,
)
from .manual_whitelist import is_manual_whitelisted, manual_label
from .commission_gate import load_commission_blocked_ids
from .lifecycle import (
    NEW_PLAN_ROI_TARGET,
    is_new_plan_roi,
    OLD_POOL_DAILY_BUDGET,
    OLD_POOL_ROI_TARGET,
    POTENTIAL_MIN_ORDERS,
    POTENTIAL_PLAN_DAILY_BUDGET,
    POTENTIAL_PLAN_ROI_TARGET,
    KEY_PLAN_ROI_TARGET,
    KEY_PLAN_DAILY_BUDGET,
    NEW_PLAN_DAILY_BUDGET,
    LifecycleDecision,
    classify_plan_lifecycle,
)
from .session_prepare import ACCOUNT_BY_DEPARTMENT, _account_id, connect_browser, ensure_qianchuan_page, open_fresh_page
from .old_creator_retarget import _open_edit_page, _wait_stable
from .plan_name import update_plan_name_delivery_date, with_delivery_date_prefix
from .plan_sweep import _save_edit_page

log = logging.getLogger("plan_power")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
if sys.stdout is not None:
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_fmt)
    log.addHandler(_ch)

# 定时任务使用 pythonw.exe 静默运行；保留独立日志，避免排障时没有输出可查。
_LOG_DIR = Path(__file__).resolve().parents[2] / "wecom-agent" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(
    _LOG_DIR / f"plan_power_{datetime.date.today():%Y%m%d}.log", encoding="utf-8"
)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

STATE_FILE = Path(__file__).resolve().parent / "plan_power_state.json"
# 老计划池跨日恢复不能放在按日重置的 STATE_FILE 中。
OLD_POOL_RESUME_FILE = Path(__file__).resolve().parent / "old_pool_resume_state.json"
LIFECYCLE_GATE_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "nightly_lifecycle_gate.json"
STALE_ZERO_ORDER_REPORT = _LOG_DIR / "old_pool_15day_readonly_20260831.json"


def _latest_valid_order_report() -> Path:
    """返回最近一次完整的15天核验报告，忽略中断留下的半成品。

    2026-09-01 事故：0828/0831 两份报告的 candidate_count 写的是去重前候选数，
    与桶实际条数不符，被旧校验整体跳过 → 系统连续 4 天默默沿用 0824 快照，
    0824 之后才有订单的计划（六六爱吃、味蕾食光等）永久停在老池循环之外。
    桶数据本身是单次完整扫描的产物，计数不符只记 WARNING，不再丢整份报告；
    选中的报告超过 2 天也记 WARNING，避免再次无声过期。
    """
    candidates = sorted(
        _LOG_DIR.glob("old_pool_15day_readonly_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            buckets = [payload.get(name) for name in ("has_orders", "verified_zero_orders", "unreadable")]
            if not all(isinstance(bucket, list) for bucket in buckets):
                continue
            if not isinstance(payload.get("summary"), dict):
                continue
            declared = int(payload.get("candidate_count") or -1)
            actual = sum(len(bucket) for bucket in buckets)
            if declared != actual:
                log.warning(
                    "15天核验报告 %s candidate_count=%s 与实际桶条数=%d 不符，"
                    "按桶数据继续使用（旧校验会整份丢弃）", path.name, declared, actual,
                )
            try:
                age_days = (datetime.datetime.now()
                            - datetime.datetime.fromtimestamp(path.stat().st_mtime)).days
                if age_days > 2:
                    log.warning(
                        "15天核验报告 %s 已 %d 天未更新，老池补入/无效核验按其旧数据执行",
                        path.name, age_days,
                    )
            except OSError:
                pass
            return path
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return STALE_ZERO_ORDER_REPORT


# 12:00 新计划仅做参数升档，正式生命周期分类仍只在 23:00 收口。
NEW_PLAN_NOON_PASS_ROI = NEW_PLAN_ROI_TARGET
NEW_PLAN_NOON_ADVANCED_ROI = POTENTIAL_PLAN_ROI_TARGET
NEW_PLAN_MIN_REMAINING_BUDGET = 300
# 一部-1已从公共账号配置移除，所有扫描枚举使用同一配置。
DEPARTMENTS = list(ACCOUNT_BY_DEPARTMENT)
# 仅供保留的旧函数 enable_batch 兼容引用；CLI 已不再执行早八批量开启。
PREVIOUS_DAY_ROI_THRESHOLD = 3.0

# 与企微轮询的浏览器互斥（2026-08-21 新增）：两套自动化共用同一个 Edge，
# 启停任务运行期间轮询整轮跳过；启停启动前也先等轮询轮次结束。
_BUSY_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "plan_power_busy.json"
_POLL_LOCK_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "browser_poll.lock"
_POLL_STATUS_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "status.json"
_BROWSER_ACTIONS = {"monitor", "close-old-pool", "close-non-key", "pause-all",
                    "resume-old-pool", "restore-pause-exceptions", "pause-old-pool",
                    "pause-new-low-roi", "pause-potential-low-roi",
                    "demote-key-low-roi",
                    "afternoon-follow-up", "pause-one", "commission-gate"}
# plan_power 动作间互斥（12:00 恢复 vs 12:20 熔断、17:00 熔断 vs 17:10 停老池
# 可能撞车；忙标只挡轮询，挡不住另一个 plan_power）。
_ACTION_LOCK_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "plan_power_action.lock"
# 大节点播报扫描忙标（2026-09-01 用户定：播报扫描优先，启停动作让行）
_REPORT_BUSY_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "report_busy.json"


def _wait_report_idle(max_wait_s: float = 15 * 60) -> None:
    """大节点播报扫描进行中时等其结束再动浏览器；忙标残留 15 分钟时效兜底。"""
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            payload = json.loads(_REPORT_BUSY_FILE.read_text(encoding="utf-8"))
            busy = time.time() - float(payload.get("ts", 0)) < 15 * 60
        except Exception:
            busy = False
        if not busy:
            return
        if time.monotonic() >= deadline:
            log.warning("等播报扫描结束超时，强行继续（可能撞浏览器）")
            return
        log.info("大节点播报扫描中（report_busy），启停动作让行，10s 后重试…")
        time.sleep(10)


def _acquire_action_lock(max_wait_s: float = 900.0):
    """拿启停动作互斥锁；等到 max_wait_s 仍未拿到就强行继续（记 warning）。"""
    import msvcrt
    fd = open(_ACTION_LOCK_FILE, "a+b")
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return fd, True
        except OSError:
            if time.monotonic() >= deadline:
                log.warning("等另一启停任务超时，强行继续（可能撞浏览器）")
                return fd, False
            log.info("另一启停任务运行中，10s 后重试…")
            time.sleep(10)


def _release_action_lock(fd, held: bool) -> None:
    import msvcrt
    try:
        if held:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        fd.close()
    except OSError:
        pass


def _busy_mark(action: str) -> None:
    try:
        _BUSY_FILE.write_text(json.dumps(
            {"action": action, "ts": time.time(), "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _busy_clear() -> None:
    try:
        _BUSY_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _poll_round_running() -> bool:
    """企微轮询轮次活着才返回 True（状态文件 5 分钟未更新视为崩溃残留）。"""
    try:
        if not _POLL_LOCK_FILE.exists():
            return False
        if time.time() - _POLL_STATUS_FILE.stat().st_mtime > 300:
            return False
        return json.loads(_POLL_STATUS_FILE.read_text(encoding="utf-8")).get("state") == "running"
    except Exception:
        return False


def _wait_poll_round_idle(max_wait_s: float = 600.0) -> None:
    deadline = time.monotonic() + max_wait_s
    while _poll_round_running() and time.monotonic() < deadline:
        log.info("企微轮询正在操作浏览器，等待其结束后启停…")
        time.sleep(15)


def _today() -> str:
    """启停状态按本地投放日隔离，避免昨天的名单影响今天。"""
    return datetime.date.today().isoformat()


def _night_start_date(now: datetime.datetime | None = None) -> str:
    """返回当前夜间窗口的起始日期（23:00 至次日 07:59 视为同一夜）。"""
    now = now or datetime.datetime.now()
    day = now.date() if now.hour >= 23 else now.date() - datetime.timedelta(days=1)
    return day.isoformat()


def _alert_gate_blocked(errors: list) -> None:
    """23:00 收口分类扫描失败 → 当晚立刻告警（2026-09-22：昨夜闸门悄没开，
    16 条建计划积压到早上才被发现；此后扫描失败必须当晚推到群里）。"""
    import traceback
    log.warning("闸门告警触发，调用栈：\n%s", "".join(traceback.format_stack(limit=12)))
    try:
        wecom_dir = Path(__file__).resolve().parents[2] / "wecom-agent"
        if str(wecom_dir) not in sys.path:
            sys.path.insert(0, str(wecom_dir))
        import daily_report_push as push_mod
        import alert_outbox
        now = datetime.datetime.now()
        lines = [
            f"**【建计划闸门未开】{now:%m月%d日 %H:%M}**",
            f"> 23:00 收口分类扫描失败 {len(errors)} 处，今晚建计划/加素材被阻断。",
            "> 失败明细：" + "；".join(str(e.get("error"))[:60] for e in errors[:3]),
            "> 影响：待建计划顺延。请检查千川页面是否又改版。",
        ]
        md = "\n".join(lines)
        url = push_mod.load_monitor_webhook()
        if not url:
            log.warning("未配置 monitor_webhook，闸门告警无法推送")
            return
        done, left = push_mod.push_missing({"汇总": url}, md, {})
        if left:
            alert_outbox.enqueue("建计划闸门", md, url)
        else:
            alert_outbox.flush()
    except Exception as exc:
        log.error("闸门告警推送异常: %s", str(exc)[:150])


def _mark_lifecycle_ready(summary: dict) -> None:
    """写入分类完成门，供轮询在同一夜间窗口放行建计划和加素材。"""
    now = datetime.datetime.now()
    errors = [
        entry for entries in (summary.get("plans") or {}).values() for entry in entries
        if entry.get("error")
    ]
    payload = {
        "night_start_date": _night_start_date(now),
        "completed_at": now.isoformat(timespec="seconds"),
        "classification_ready": not errors,
        "scan_errors": errors,
        "kept_key_plans": len(summary.get("kept_key_plans") or []),
        "closed_non_key": len(summary.get("paused") or []),
        "close_failures": len(summary.get("failed") or []),
        "reopen_failures": len(summary.get("reopen_failed") or []),
        # 暂停/恢复失败的计划名单：不挡门（老池没停成不该阻断夜间建新计划），
        # 但必须在门文件里看得见，避免再出现 110 条失败无人察觉的情况。
        "close_failed_plans": [
            str(e.get("plan_name") or e.get("department") or "?")
            for e in (summary.get("failed") or []) + (summary.get("reopen_failed") or [])
            if isinstance(e, dict)
        ][:50],
    }
    tmp = LIFECYCLE_GATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LIFECYCLE_GATE_FILE)
    if not payload["classification_ready"]:
        _alert_gate_blocked(errors)


def _empty_state() -> dict:
    return {
        "date": _today(),
        "midnight_paused": [],
        "night_created": [],
        "enabled": [],
        "key_kept": [],
        "closed_non_key": [],
        "old_pool_paused": [],
        "old_pool_enabled": [],
        "new_plan_promotions": [],
        "new_plan_paused_at_17": [],
    }


def automation_enabled() -> bool:
    """读取企微自动化总开关。缺失或损坏时按关闭处理，避免意外操作计划。"""
    flag_file = Path(__file__).resolve().parents[2] / "wecom-agent" / "automation.json"
    try:
        return bool(json.loads(flag_file.read_text(encoding="utf-8")).get("enabled", False))
    except Exception:
        return False


async def _department_page(context, department: str):
    """复用指定业务部的总览页，防止启停任务额外打开重复标签。"""
    account_id = _account_id(ACCOUNT_BY_DEPARTMENT[department])
    return _department_overview_page(context.pages, account_id) or await open_fresh_page(context)


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            if st.get("date") != _today():
                return _empty_state()
            st.setdefault("midnight_paused", [])
            st.setdefault("night_created", [])
            st.setdefault("enabled", [])
            st.setdefault("key_kept", [])
            st.setdefault("closed_non_key", [])
            st.setdefault("old_pool_paused", [])
            st.setdefault("old_pool_enabled", [])
            st.setdefault("new_plan_promotions", [])
            st.setdefault("new_plan_paused_at_17", [])
            return st
    except Exception:
        pass
    return _empty_state()


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _merge_entries(entries: list[dict], new_items: list[dict]) -> list[dict]:
    """按 plan_id（无 ID 用 plan_name）去重合并。

    2026-09-01：已有条目 resume_date 早于新条目时必须换新。熔断 12:28 把
    「建成即暂停挂 08:00」的新计划再停一次并挂次日 12:00，旧的去重直接丢弃
    新条目 → 计划滞留一张已过期的新计划队列票，次日老池恢复永远等不到它
    （0901_小禾的元气厨房_肖 即此案例）。
    """
    by_key = {e.get("plan_id") or e.get("plan_name"): e for e in entries}
    for item in new_items:
        key = item.get("plan_id") or item.get("plan_name")
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            entries.append(item)
            by_key[key] = item
            continue
        if str(item.get("resume_date") or "") > str(existing.get("resume_date") or ""):
            existing.clear()
            existing.update(item)
    return entries


def _load_old_pool_resume_queue() -> dict:
    try:
        queue = json.loads(OLD_POOL_RESUME_FILE.read_text(encoding="utf-8"))
        if isinstance(queue, dict):
            queue.setdefault("pending", [])
            # 明确被平台标记为系统暂停、且用户要求不再处理的计划。
            # 该名单必须持久化，否则 12:00 的 23:00 标签兜底会再次补回。
            queue.setdefault("ignored", [])
            return queue
    except Exception:
        pass
    return {"pending": [], "ignored": []}


def _resume_key(item: dict) -> str:
    return str(item.get("plan_id") or item.get("plan_name") or "").strip()


def _ignored_resume_keys(queue: dict | None = None) -> set[str]:
    queue = queue or _load_old_pool_resume_queue()
    return {
        _resume_key(item)
        for item in (queue.get("ignored") or [])
        if isinstance(item, dict) and _resume_key(item)
    }


def _save_old_pool_resume_queue(queue: dict) -> None:
    queue["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = OLD_POOL_RESUME_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OLD_POOL_RESUME_FILE)


def _default_resume_date(now: datetime.datetime | None = None) -> str:
    """老池恢复日的默认取值：23:00 正常收口 → 次日 12:00 恢复；
    白天补跑收口（12 点前）→ 当天 12:00 恢复；12 点后补跑 → 顺延次日。

    08-21 事故：09:42 补跑收口按旧逻辑一律取次日，导致当天 12:00 恢复任务
    空跑、35 条老池计划多停了一天。
    """
    now = now or datetime.datetime.now()
    day = now.date() if now.hour < 12 else now.date() + datetime.timedelta(days=1)
    return day.isoformat()


def _resume_time_for(item: dict) -> str:
    """恢复时刻：老池 12:00（每日固定窗口），新计划/潜力计划 08:00（2026-08-23 策略）。"""
    return "12:00" if item.get("category") == "old_pool" else "08:00"


def _queue_old_pool_resume(items: list[dict], resume_date: str | None = None,
                           resume_time: str | None = None) -> list[dict]:
    """登记本次实际暂停的非重点计划（老池/潜力/新测试），到点才允许恢复。

    resume_time 缺省按类别分流：old_pool → 12:00，其余（new_plan/potential）→ 08:00；
    队列里的旧条目没有 resume_time 字段，恢复时按 12:00 处理。
    """
    if not items:
        return []
    queue = _load_old_pool_resume_queue()
    resume_date = resume_date or _default_resume_date()
    # 对新计划而言，恢复队列日期就是实际起投日；计划名时间前缀必须同步。
    normalized_items = []
    for item in items:
        entry = dict(item)
        if entry.get("category") == "new_plan" and entry.get("plan_name"):
            try:
                target_day = datetime.date.fromisoformat(resume_date)
                entry["plan_name"] = with_delivery_date_prefix(entry["plan_name"], target_day)
            except (TypeError, ValueError):
                pass
        normalized_items.append(entry)
    entries = []
    for item in normalized_items:
        entries.append({
            **item,
            "resume_date": resume_date,
            "resume_time": resume_time or _resume_time_for(item),
            "queued_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
    _merge_entries(queue["pending"], entries)
    _save_old_pool_resume_queue(queue)
    return entries


def _load_verified_recent_order_ids() -> dict[str, dict]:
    """Load the verified 15-day order report used to seed old-pool recovery."""
    try:
        payload = json.loads(_latest_valid_order_report().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(item.get("plan_id")): item
        for item in (payload.get("has_orders") or [])
        if item.get("plan_id")
        and (item.get("near_15_days_orders") or 0) > 0
    }


def _verified_stale_zero_order_ids() -> set[str]:
    """返回已核验“创建满15天且近15天0单”的计划，不允许12点自动恢复。"""
    try:
        payload = json.loads(_latest_valid_order_report().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    try:
        as_of = datetime.date.fromisoformat(str(payload.get("as_of")))
    except (TypeError, ValueError):
        return set()
    cutoff = as_of - datetime.timedelta(days=15)
    return {
        str(item.get("plan_id"))
        for item in payload.get("verified_zero_orders") or []
        if item.get("plan_id")
        and item.get("near_15_days_orders") == 0
        and item.get("plan_date")
        and str(item.get("plan_date")) <= cutoff.isoformat()
    }


# 回补存活校验（2026-09-15，幽灵票病根）：15 天核验报告是快照，出报告后被
# 平台删除的计划仍每晚被「近15天有订单」回补重新挂票（荔枝酱 0814 案：
# 09-10 已删，票每晚重生）。catalog 是纯 UPSERT 从不删行，已删计划的
# last_seen_at 会冻结；回补候选全是「近15天有单」的计划——有单就有消耗、
# 白天扫描必见，故 last_seen 超过阈值或 catalog 无记录 = 平台已删，不再回补。
_RECENT_ORDER_GHOST_MAX_AGE_S = 2 * 86400


def _catalog_last_seen_map() -> dict[str, float] | None:
    """plan_id → catalog 最后见到时间（秒）。读失败返回 None=本轮不校验
    （与既有「读不出不动」口径一致：校验缺席宁可放过，不可全灭回补）。"""
    try:
        from webui.db import list_plan_catalog
        return {
            str(item.get("plan_id") or ""): float(item.get("last_seen_at") or 0)
            for item in list_plan_catalog(limit=2000)
            if item.get("plan_id")
        }
    except Exception as exc:
        log.warning("回补存活校验读 catalog 失败，本轮跳过校验: %s", str(exc)[:120])
        return None


def _recent_order_ghost(plan_id: str, catalog_seen: dict[str, float] | None,
                        *, now_ts: float | None = None) -> bool:
    """回补候选是否=平台已删的幽灵（catalog 无记录或最后见到超过阈值）。"""
    if catalog_seen is None:
        return False
    seen_ts = catalog_seen.get(str(plan_id), 0.0)
    return (now_ts or time.time()) - seen_ts > _RECENT_ORDER_GHOST_MAX_AGE_S


# 15 天核验报告定期重跑（2026-09-15，幽灵票病根的另一半）：报告是快照，
# 不刷新则 has_orders 永远停在出具日——出报告后才开单的计划永远进不了回补，
# 出报告后被删的计划每晚被回补成幽灵票（后者由 _recent_order_ghost 兜住，
# 前者只能靠定期重跑）。close 尾巴按报告年龄超龄拉起独立核验进程
# （inspect_old_pool_15day.py 自持动作锁+忙标，与夜间守望互斥；
# 拿不到锁/失败则次晚收口再试，自调节无需人工）。
_ORDER_REPORT_REFRESH_AGE_DAYS = 6


def _order_report_age_days(now: float | None = None) -> float | None:
    """报告年龄（天）。优先按内容 as_of 日期——文件 mtime 会被人工编辑洗新
    （2026-09-14 幽灵票清理改了报告一行，就把 16 天陈报告洗成 1.4 天，
    昨晚 23:00 的 age-gate 因此没拉起核验），as_of 才是数据真实日期。"""
    now_ts = now or time.time()
    try:
        path = _latest_valid_order_report()
    except Exception:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        as_of = datetime.date.fromisoformat(str(payload.get("as_of")))
        ref = datetime.datetime.fromtimestamp(now_ts).date()
        return float((ref - as_of).days)
    except (OSError, ValueError, TypeError):
        pass
    try:
        return (now_ts - path.stat().st_mtime) / 86400
    except OSError:
        return None


def _maybe_spawn_order_report_refresh() -> str:
    """报告超龄则 detached 拉起 15 天核验；返回状态串供日志/测试断言。"""
    age = _order_report_age_days()
    if age is None:
        return "report-unreadable"
    if age < _ORDER_REPORT_REFRESH_AGE_DAYS:
        return f"fresh:{age:.1f}d"
    script = Path(__file__).resolve().parents[2] / "wecom-agent" / "inspect_old_pool_15day.py"
    runtime_py = Path(__file__).resolve().parents[1] / "runtime" / "python.exe"
    py = str(runtime_py) if runtime_py.exists() else sys.executable
    log_path = _LOG_DIR / "inspect_15day_refresh.log"
    try:
        with open(log_path, "ab") as out:
            subprocess.Popen(
                [py, "-X", "utf8", str(script)],
                cwd=str(script.parent), stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception as exc:
        log.error("15天核验报告重跑拉起失败: %s", str(exc)[:150])
        return f"spawn-failed:{str(exc)[:80]}"
    log.warning("15天核验报告已 %.1f 天未更新，拉起定期重跑（独立进程自持锁）", age)
    return f"spawned:{age:.1f}d"


def _plan_created_within_15_days(plan_id: str, as_of: str) -> bool:
    """未满15天的计划不能因近15天0单而判无效，仍应进入老池恢复。"""
    try:
        from webui.db import list_plan_catalog
        row = next((item for item in list_plan_catalog(limit=500)
                    if str(item.get("plan_id") or "") == str(plan_id)), None)
        plan_date = (row or {}).get("plan_date")
        if not plan_date:
            return False
        created = datetime.date.fromisoformat(str(plan_date)[:10])
        reference = datetime.date.fromisoformat(str(as_of)[:10])
        return created > reference - datetime.timedelta(days=15)
    except (TypeError, ValueError, OSError):
        return False


def _save_pause_snapshot(report: dict, action: str) -> Path:
    """在改变计划开关前留存本次读取到的完整计划快照。"""
    captured_at = datetime.datetime.now()
    path = _LOG_DIR / f"plan_pause_snapshot_{captured_at:%Y%m%d_%H%M%S}.json"
    payload = {
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "action": action,
        "counts": report.get("counts") or {},
        "plans": report.get("plans") or {},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _database_old_pool_resume_candidates(today: str) -> list[dict]:
    """从前一日 23:00 标签恢复老池候选，作为队列丢失/被消费后的兜底。

    ``old_pool_resume_state.json`` 只是执行队列，不是业务事实源：队列可能在
    08:00 被消费、被错误清空，或进程重启后只剩下未来日期条目。前一日 23:00
    的日标签才是“昨晚收口时确实属于老池”的可审计依据。这里只取已暂停的
    old_pool，并重新应用僵尸计划与人工白名单排除，绝不按当前 ROI 重新分类。
    """
    try:
        source_date = datetime.date.fromisoformat(today) - datetime.timedelta(days=1)
        from webui.db import list_plan_daily_labels
        rows = [
            row for row in list_plan_daily_labels(source_date.isoformat())
            if row.get("scan_slot") == "23:00"
        ]
    except Exception as exc:
        log.warning("读取前一日老池标签失败: %s", str(exc)[:160])
        return []

    stale_ids = _verified_stale_zero_order_ids()
    # 2026-09-03 用户定：标签是日常/潜力的计划每天都应被拉起，不再要求
    # 「近15天有单」才恢复——零消耗老计划不在扫描视野内、此前被该门槛挡成
    # 永久停放（瑶妈/叶叶家/南汇猫蛋/梦妮×2/小北大×2/小米爱做饭 9 条案例）。
    # 只有正式核验进 15 天零单无效名单（stale_ids）才跳过；核验报告由
    # inspect_old_pool_15day 定期更新，是唯一的「无效」事实源。
    candidates: list[dict] = []
    for row in rows:
        if row.get("category") not in ("old_pool", "potential_plan"):
            continue
        plan_id = str(row.get("plan_id") or "").strip()
        plan_name = str(row.get("plan_name") or "").strip()
        department = str(row.get("department") or "").strip()
        if not plan_id or not department or not plan_name:
            continue
        if "暂停" not in str(row.get("plan_status") or ""):
            continue
        is_young = _plan_created_within_15_days(plan_id, today)
        if plan_id in stale_ids and not is_young:
            continue
        if is_manual_whitelisted(
            department=department, plan_id=plan_id, plan_name=plan_name
        ):
            continue
        category = str(row.get("category"))
        candidates.append({
            **row,
            "department": department,
            "plan_id": plan_id,
            "plan_name": plan_name,
            "category": category,
            "resume_date": today,
            "resume_time": "12:00" if category == "old_pool" else "08:00",
            "time_range": None,
            "queued_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "fallback_source": "previous_day_23:00_labels",
        })
    return candidates


def _recover_old_pool_resume_queue() -> list[dict]:
    """恢复到点队列，并用前一日 23:00 标签补齐丢失的老池队列。"""
    queue = _load_old_pool_resume_queue()
    ignored_keys = _ignored_resume_keys(queue)
    pending = [item for item in queue["pending"] if _resume_key(item) not in ignored_keys]
    if len(pending) != len(queue["pending"]):
        queue["pending"] = pending
        _save_old_pool_resume_queue(queue)
    today = _today()
    now_hhmm = datetime.datetime.now().strftime("%H:%M")
    due_old = [
        item for item in pending
        if (item.get("category") or "old_pool") == "old_pool"
        and item.get("resume_date", today) <= today
        and (item.get("resume_time") or "12:00") <= now_hhmm
    ]
    # 每次 12:00 后都以昨晚23:00标签交叉核对；不能因队列里“恰好有一条”
    # 到期项就放弃补齐其余老池。已完成项记录在当日 state，故补跑只会重试失败项。
    if now_hhmm >= "12:00":
        completed_keys = {
            str(item.get("plan_id") or item.get("plan_name") or "")
            for item in (load_state().get("old_pool_enabled") or [])
            if item.get("plan_id") or item.get("plan_name")
        }
        # 无论队列中是否仍有到期失败项，都必须用23:00事实标签补齐：
        # 队列是执行状态，不是完整业务名单，不能因“还有一条失败项”就漏掉
        # 其它计划。已完成项和队列中已有项由 _merge_entries 去重。
        fallback = [
            item for item in _database_old_pool_resume_candidates(today)
            if _resume_key(item) not in completed_keys
            and _resume_key(item) not in ignored_keys
        ]
        # 保留队列上线前的兼容路径：测试/历史环境可能没有日标签表，
        # 此时仍从上一份收口状态恢复，避免升级后彻底丢失恢复能力。
        if not fallback:
            try:
                previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                source_date = datetime.date.fromisoformat(previous.get("date", ""))
                resume_date = (source_date + datetime.timedelta(days=1)).isoformat()
            except Exception:
                previous, resume_date = {}, today
            fallback = [
                {**item, "resume_date": resume_date, "resume_time": "12:00",
                 "time_range": None, "fallback_source": "previous_state"}
                for item in (previous.get("closed_non_key") or [])
                if item.get("category") == "old_pool"
                and _resume_key(item) not in completed_keys
                and _resume_key(item) not in ignored_keys
            ]
        before = len(pending)
        _merge_entries(pending, fallback)
        added = len(pending) - before
        if added:
            queue["pending"] = pending
            _save_old_pool_resume_queue(queue)
            log.warning(
                "恢复队列与昨日23:00老池/潜力标签不一致，已补齐 %d 条（已有到期%d条）",
                added, len(due_old),
            )
    # 消费白名单「停用收养」文件：主管把白名单行改停用后，sync 会把计划写到这里，
    # 恢复槽直接挂票（不再依赖当晚23:00打标+次日12:00补票的隔晚链路，
    # 也兜住总览扫描看不到的超老暂停计划，2026-09-29 用户反馈）。
    adopted = _consume_whitelist_adoptions(today, ignored_keys)
    if adopted:
        before = len(pending)
        _merge_entries(pending, adopted)
        if len(pending) != before:
            queue["pending"] = pending
            _save_old_pool_resume_queue(queue)
        log.warning("白名单停用收养挂票 %d 条: %s", len(adopted),
                    [str(i.get("plan_id")) for i in adopted][:5])
    return _load_old_pool_resume_queue().get("pending") or pending


def _consume_whitelist_adoptions(today: str, ignored_keys: set) -> list[dict]:
    """读取并清除 whitelist_adoptions.json，返回可挂票的恢复条目。

    过滤：ignored 死票、15 天零单无效名单、仍命中人工白名单的（停用后又改回启用）。
    文件读取失败/不存在返回空，绝不抛异常阻塞恢复槽。
    """
    path = Path(__file__).resolve().parents[2] / "wecom-agent" / "whitelist_adoptions.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not entries:
            return []
    except (OSError, json.JSONDecodeError):
        return []
    stale_ids = _verified_stale_zero_order_ids()
    # 恢复槽只在 08:00/12:00 跑：12 点后收养的老池票必须挂次日 12:00，
    # 否则 resume_time=当下时刻永远等不到「到点」（槽位时间不会超过 resume_time）。
    resume_date = _default_resume_date()
    adopted = []
    for e in entries:
        pid = str(e.get("plan_id") or "").strip()
        name = str(e.get("plan_name") or "").strip()
        dep = str(e.get("department") or "").strip()
        if not pid or pid in ignored_keys or pid in stale_ids:
            continue
        if is_manual_whitelisted(department=dep, plan_id=pid, plan_name=name):
            continue
        adopted.append({
            "plan_id": pid,
            "plan_name": name,
            "department": dep,
            "category": "old_pool",
            "plan_status": "已暂停",
            "resume_date": resume_date,
            "resume_time": "12:00",
            "time_range": None,
            "queued_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "fallback_source": "whitelist_tombstone",
        })
    try:
        path.unlink()
    except OSError:
        pass
    return adopted


def capture_old_pool_resume(dry_run: bool = False) -> dict:
    """从 23:00 收口状态补记老计划池恢复队列，不访问浏览器、不改计划。"""
    try:
        current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"action": "capture-old-pool", "captured": 0, "message": str(exc)[:200]}
    candidates = [
        item for item in (current.get("closed_non_key") or [])
        if item.get("category") == "old_pool"
    ]
    try:
        source_date = datetime.date.fromisoformat(current.get("date", ""))
        resume_date = (source_date + datetime.timedelta(days=1)).isoformat()
    except ValueError:
        resume_date = None
    if not dry_run:
        _queue_old_pool_resume(candidates, resume_date)
    return {
        "action": "capture-old-pool",
        "dry_run": dry_run,
        "candidates": len(candidates),
        "queued": len(_load_old_pool_resume_queue().get("pending") or []),
    }


def record_night_created(department: str, plan_id: str | None, plan_name: str | None,
                         paused: bool) -> None:
    """plan_create --pause-after 建完计划后调用：记入夜间新建名单（08:00 开启用）。

    即使行内暂停失败也记录——00:00 批量暂停会兜底把它暂停并记入 midnight_paused，
    enable-batch 幂等，重复开启无害。
    """
    state = load_state()
    _merge_entries(state["night_created"], [{
        "department": department,
        "plan_id": plan_id,
        "plan_name": plan_name,
        "paused": paused,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }])
    save_state(state)


# ---------------------------------------------------------------------------
# 行内开关 / 弹窗
# ---------------------------------------------------------------------------

async def _row_status_text(tr) -> str:
    try:
        text = await tr.evaluate("""el => {
            const cells = el.querySelectorAll('td, th');
            const c = cells[2];
            return c ? (c.innerText || '') : '';
        }""")
        return (text or "").split("\n")[0].strip()
    except Exception:
        return ""


async def _set_row_checkbox(tr, checked: bool) -> bool:
    """勾选/取消勾选行复选框（点 label，千川 Vue 只认这个）。

    2026-09-22 千川改版：label.ovui-checkbox → label.aurora-qc-checkbox-wrapper
    （内层 span.aurora-qc-checkbox + input.aurora-qc-checkbox-input）。
    勾选态以 input.checked 为准，组件类名只做兜底。
    """
    try:
        label = tr.locator(
            "label.aurora-qc-checkbox-wrapper, label.ovui-checkbox").first
        if not await label.count():
            return False
        inp = tr.locator("input[type='checkbox']").first

        async def is_checked() -> bool | None:
            try:
                if await inp.count():
                    return bool(await inp.is_checked())
            except Exception:
                pass
            try:
                cls = await label.get_attribute("class") or ""
                inner = tr.locator(".aurora-qc-checkbox, .ovui-checkbox").first
                cls += " " + (await inner.get_attribute("class") or "")
                return "checked" in cls
            except Exception:
                return None

        state = await is_checked()
        if state is not None and state == checked:
            return True
        await label.evaluate("el => el.click()")
        for _ in range(10):
            state = await is_checked()
            if state is not None and state == checked:
                return True
            await asyncio.sleep(0.2)
        return False
    except Exception:
        return False


async def _power_via_batch_bar(page, tr, turn_on: bool, verify_word: str,
                               timeout_s: float = 60.0) -> str:
    """勾选本行 → 批量栏「开启/暂停」→ 弹窗确认 → 验证状态列。

    2026-08-21 起唯一启停路径：行内开关对 JS/鼠标合成事件均无响应（08-20 晚
    110 条全部失败实测），全选+批量栏是唯一验证可靠的操作方式；单条操作
    即勾选 1 行走同一批量栏。
    08-21 往返实测：开启 30 秒内状态列刷新；暂停实际生效但状态列刷新超过
    30 秒（约 1-2 分钟），故默认超时放宽到 60 秒，且确认弹窗改为等待出现。
    """
    word = "开启" if turn_on else "暂停"
    current = await _row_status_text(tr)
    if verify_word in current:
        return "already"
    # 2026-10-15 实证：「系统暂停」是平台层暂停（余额不足/违规等），批量栏
    # 点了也不生效——直接秒报跳过，别走 60s 验证空等（12:20 槽拖慢的根因）。
    if turn_on and "系统暂停" in current:
        return "failed:系统暂停(平台层，需查余额/违规)"
    if not await _set_row_checkbox(tr, True):
        return "failed:checkbox"
    try:
        # 等批量栏出现
        for _ in range(10):
            body = await page.locator("body").evaluate("e => e.innerText")
            if "已选" in body:
                break
            await page.wait_for_timeout(300)
        clicked = await _batch_click(page, word)
        if clicked == "not_found":
            return "failed:batch_button_missing"
        # 等确认弹窗真正渲染出来再点确认（固定 sleep 曾早于弹窗出现导致空点）
        for _ in range(12):
            await page.wait_for_timeout(500)
            if await page.evaluate("""() => {
                const els = document.querySelectorAll('[role="dialog"], .ovui-modal, .oc-modal, [class*="modal"], [class*="dialog"]');
                for (const d of els) {
                    if (d.offsetParent === null) continue;
                    const btns = Array.from(d.querySelectorAll('button')).filter(b => b.offsetParent !== null);
                    if (btns.length) return true;
                }
                return false;
            }"""):
                break
        await _confirm_modal(page)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1000)
            if verify_word in await _row_status_text(tr):
                return "ok"
        return f"failed:batch={clicked};status={await _row_status_text(tr)}"
    finally:
        # 收尾取消勾选，避免污染同页会话的下一次操作
        await _set_row_checkbox(tr, False)


async def _confirm_modal(page) -> bool:
    """点击当前弹窗/对话框中的主按钮。

    2026-10-15 新 UI 实证坑：确认弹窗可能是「再想想 / 确定」组合——旧逻辑点
    「第一个非取消按钮」会点到「再想想」（=取消操作），批量开启点了等于没点。
    现在优先按确认语义白名单点名，兜底才用非取消逻辑。
    """
    return await page.evaluate("""() => {
        const CONFIRM_WORDS = ['确定', '确认', '开启', '暂停', '仍要关闭', '知道了', '好的', '提交'];
        const dialogs = document.querySelectorAll('[role="dialog"], .ovui-modal, .oc-modal, [class*="modal"], [class*="dialog"]');
        for (const d of dialogs) {
            if (d.offsetParent === null) continue;
            const btns = Array.from(d.querySelectorAll('button')).filter(b => b.offsetParent !== null);
            if (!btns.length) continue;
            for (const w of CONFIRM_WORDS) {
                const hit = btns.find(b => (b.innerText || '').trim() === w);
                if (hit) { hit.click(); return true; }
            }
            // 兜底：最后一个非取消按钮（主按钮通常在右/最后）
            const nonCancel = btns.filter(b => !['取消', 'Cancel', '再想想'].includes((b.innerText || '').trim()));
            if (nonCancel.length) { nonCancel[nonCancel.length - 1].click(); return true; }
        }
        for (const b of document.querySelectorAll('button')) {
            const text = (b.innerText || '').trim();
            if (b.offsetParent !== null && (text === '确认' || text === '确定')) {
                b.click();
                return true;
            }
        }
        return false;
    }""")


async def _confirm_batch_action(page, action_word: str) -> bool:
    """批量开启/暂停后的确认识别（2026-09-08）。

    小批量平台直接生效无确认；大批量（09-07 55条/09-08 56条）点击后 0 翻
    状态且对话框检测为空——嫌疑是确认 UI 走 popconfirm/popover 气泡、或按钮
    文案为「确认开启/确认暂停」（_confirm_modal 全局兜底只吃精确「确认/确定」）。
    顺序：对话框主按钮（沿用 _confirm_modal 语义）→ 含动作词的气泡主按钮 →
    全局精确文案兜底（确认/确定/确认开启/确认暂停，不点裸「开启」防误触）。
    """
    return await page.evaluate("""(actionWord) => {
        const isVis = el => el && el.offsetParent !== null;
        const norm = s => (s || '').replace(/\\s+/g, '');
        // ① 对话框：点第一个非「取消」可见按钮
        const dialogs = document.querySelectorAll('[role="dialog"], .ovui-modal, .oc-modal, [class*="modal"], [class*="dialog"]');
        for (const d of dialogs) {
            if (!isVis(d)) continue;
            const btns = Array.from(d.querySelectorAll('button')).filter(isVis);
            for (const b of btns) {
                const t = norm(b.innerText);
                if (t && t !== '取消' && t !== 'Cancel') { b.click(); return true; }
            }
        }
        // ② 气泡（popconfirm/popover/popper）：文本含动作词或「确认/确定」才点，
        //    防误点筛选浮层/日期选择器等常驻气泡
        const bubbles = document.querySelectorAll('[class*="popconfirm"], [class*="popover"], [class*="popper"]');
        for (const d of bubbles) {
            if (!isVis(d)) continue;
            const dt = norm(d.innerText);
            if (!dt.includes(actionWord) && !dt.includes('确认') && !dt.includes('确定')) continue;
            const btns = Array.from(d.querySelectorAll('button')).filter(isVis);
            for (const b of btns) {
                const t = norm(b.innerText);
                if (t && t !== '取消' && t !== 'Cancel') { b.click(); return true; }
            }
        }
        // ③ 全局精确文案（含「确认开启/确认暂停」）
        const EXACT = ['确认', '确定', '确认' + actionWord];
        for (const b of document.querySelectorAll('button')) {
            if (!isVis(b)) continue;
            if (EXACT.includes(norm(b.innerText))) { b.click(); return true; }
        }
        return false;
    }""", action_word)


async def _dismiss_promotion_modal(page) -> bool:
    """关闭千川全店托管等推广弹窗，只点右上角关闭控件，绝不点开通按钮。"""
    return await page.evaluate("""() => {
        const visible = el => el && el.offsetParent !== null;
        const promos = Array.from(document.querySelectorAll(
            '[role="dialog"], [class*="modal"], [class*="popup"]'
        )).filter(el => visible(el) && /全店托管升级|多号多账户投放/.test(el.innerText || ''));
        for (const modal of promos) {
            const candidates = Array.from(modal.querySelectorAll(
                'button, [role="button"], [aria-label], [class*="close"], iconpark-icon'
            )).filter(visible);
            const close = candidates.find(el => {
                const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                const cls = (el.className || '').toString().toLowerCase();
                const icon = (el.getAttribute('name') || '').toLowerCase();
                return text === '×' || text === 'x' || /关闭|close/.test(text)
                    || cls.includes('close') || icon.includes('close');
            });
            if (close) {
                close.click();
                return true;
            }
        }
        return false;
    }""")


async def _set_plan_power(page, tr, turn_on: bool, verify_word: str,
                          timeout_s: float = 60.0) -> str:
    """兼容包装：行内开关已于 2026-08-21 废弃，统一走批量栏路径。"""
    return await _power_via_batch_bar(page, tr, turn_on, verify_word, timeout_s)


async def pause_plan(page, plan_name: str, plan_id: str | None, department: str) -> tuple[bool, str]:
    """按名称搜索定位单行并暂停（plan_create --pause-after 用）。"""
    if is_manual_whitelisted(department=department, plan_id=plan_id or "", plan_name=plan_name):
        return True, f"{manual_label(department=department, plan_id=plan_id or '', plan_name=plan_name)}人工白名单，未暂停"
    rows = await _search_plans(page, plan_name, department, by_creator=False)
    if not rows:
        return False, f"总览页未搜到计划: {plan_name}"
    target = None
    for tr in rows:
        try:
            metrics = await _parse_plan_row(tr)
        except Exception:
            continue
        if metrics is None:  # 畸形行已留 WARNING，跳过
            continue
        if plan_id and metrics.plan_id == plan_id:
            target = tr
            break
        if not plan_id and metrics.plan_name == plan_name:
            target = tr
            break
    if target is None:
        return False, f"搜索结果中未找到计划 ID: {plan_id or plan_name}"
    if await _dismiss_promotion_modal(page):
        await page.wait_for_timeout(500)
    result = await _set_plan_power(page, target, turn_on=False, verify_word="已暂停")
    if result in ("ok", "already"):
        return True, result
    return False, result


# ---------------------------------------------------------------------------
# 00:00 批量暂停
# ---------------------------------------------------------------------------

# 大批量分批阈值：平台批量操作对大单量静默失效（09-07 55条开启、09-08 55条暂停
# 均点击后 0 翻状态，弹窗确认正常），≤7 条两天各档全部成功。取 10 稳妥。
_BATCH_CHUNK_SIZE = 10


async def _batch_click(page, word: str) -> str:
    """点击批量操作栏里的指定按钮（开启/暂停）。

    2026-10-15 新 UI 实证：JS click 不触发批量栏按钮的 React 处理器（点了
    不弹确认框、状态不变）。改为 JS 打标 + Playwright 原生鼠标点击。
    """
    tag = await page.evaluate("""(word) => {
        const isVis = el => el.offsetParent !== null;
        for (const e of document.querySelectorAll('[data-pp-batch]')) e.removeAttribute('data-pp-batch');
        const bars = Array.from(document.querySelectorAll('[class*="batch"], [class*="Batch"]'));
        for (const bar of bars) {
            if (!isVis(bar)) continue;
            for (const b of bar.querySelectorAll('button, [role="button"], span, a')) {
                if (isVis(b) && (b.innerText || '').trim() === word) {
                    b.setAttribute('data-pp-batch', '1'); return 'tagged:batch';
                }
            }
        }
        for (const b of document.querySelectorAll('button, [role="button"]')) {
            if (isVis(b) && (b.innerText || '').trim() === word) {
                b.setAttribute('data-pp-batch', '1'); return 'tagged:fallback';
            }
        }
        return 'not_found';
    }""", word)
    if tag == "not_found":
        return "not_found"
    try:
        await page.locator('[data-pp-batch="1"]').first.click(timeout=5000)
        return "clicked:" + tag.split(":")[1]
    except Exception:
        # 原生点击失败再退化 JS 点
        await page.evaluate("""() => {
            const b = document.querySelector('[data-pp-batch="1"]');
            if (b) b.click();
        }""")
        return "clicked:js_fallback"


async def _batch_power_targets(
    page, department: str, targets: list[dict], *, turn_on: bool,
) -> dict:
    """按计划 ID 在全表中批量开启/暂停精确目标，逐页复核结果。

    不采用“逐条搜索→逐条暂停”：先扫描分页表定位目标，再一次勾选同页
    目标并点击批量按钮。只有批量后仍未达到目标状态的条目才由上层回退。
    这样不会为了提速而把重点、潜力或人工白名单误暂停。
    """
    action_word = "开启" if turn_on else "暂停"
    desired_word = "投放中" if turn_on else "暂停"
    result = {"enabled": [], "paused": [], "already": [], "failed": []}
    if not targets:
        return result
    wanted = {
        str(item.get("plan_id") or item.get("plan_name") or "").strip(): item
        for item in targets
        if str(item.get("plan_id") or item.get("plan_name") or "").strip()
    }
    found: set[str] = set()
    await _open_overall(page, department)
    await page.wait_for_timeout(800)
    if await _dismiss_promotion_modal(page):
        await page.wait_for_timeout(300)

    for _page_no in range(50):
        rows = []
        for _ in range(60):
            trs = page.locator("table tbody tr")
            visible = [trs.nth(i) for i in range(await trs.count())
                       if await trs.nth(i).is_visible()]
            if visible:
                rows = visible
                break
            await page.wait_for_timeout(500)
        if not rows:
            break
        page_keys: set[str] = set()
        matches = []
        for row in rows:
            try:
                metrics = await _parse_plan_row(row)
            except Exception:
                continue
            if metrics is None:  # 畸形行已留 WARNING，跳过
                continue
            row_key = str(metrics.plan_id or metrics.plan_name or "").strip()
            if row_key:
                page_keys.add(row_key)
            item = wanted.get(str(metrics.plan_id or ""))
            if item is None and not metrics.plan_id:
                item = wanted.get(str(metrics.plan_name or ""))
            if item is None:
                continue
            key = str(item.get("plan_id") or item.get("plan_name") or "").strip()
            found.add(key)
            if desired_word in (metrics.status or ""):
                result["already"].append(item)
            # 2026-09-04 修复：暂停动作要勾的是「投放中」的行（状态不含「暂停」），
            # 此前两个分支都写成「暂停」in status——在投计划永远进不了 matches，
            # 批量暂停从未生效（17:00 老池停 110 条全落 failed 走逐条的根因）。
            elif ((not turn_on and "暂停" not in (metrics.status or "")) or
                  (turn_on and "暂停" in (metrics.status or ""))):
                matches.append((item, row))
            else:
                result["failed"].append({**item, "detail": f"unexpected_status:{metrics.status}"})
        # 大批量分批点（2026-09-08）：实测 ≤7 条稳定生效、≥32 条点击后 0 翻状态
        # （09-07 业务3部 55 条开启、09-08 业务2部 55 条/业务3部 51 条暂停全吞，
        # 弹窗/确认均正常但状态不翻），平台阈值未知，取 10 条一批。
        # 批间表格可能因前一批动作重渲染，第 2 批起勾前逐行重核归属防勾错。
        for chunk_no, chunk_start in enumerate(range(0, len(matches), _BATCH_CHUNK_SIZE)):
            chunk = matches[chunk_start:chunk_start + _BATCH_CHUNK_SIZE]
            selected = []
            for item, row in chunk:
                if chunk_no > 0:
                    try:
                        m_now = await _parse_plan_row(row)
                    except Exception:
                        m_now = None
                    want_pid = str(item.get("plan_id") or "")
                    shifted = (
                        m_now is None
                        or (want_pid and str(m_now.plan_id or "") != want_pid)
                        or (not want_pid
                            and str(m_now.plan_name or "") != str(item.get("plan_name") or ""))
                    )
                    if shifted:
                        result["failed"].append({**item, "detail": "failed:row_shifted"})
                        continue
                    if desired_word in (m_now.status or ""):
                        result["already"].append(item)  # 被前一批动作连带翻转
                        continue
                if await _set_row_checkbox(row, True):
                    selected.append((item, row))
                else:
                    result["failed"].append({**item, "detail": "failed:checkbox"})
            try:
                if selected:
                    clicked = await _batch_click(page, action_word)
                    # 2026-09-03 批量失效排查：17:00 老池停 110 条全部落 failed 桶
                    # （无一条批量成功），补日志定位断点（按钮/弹窗/轮询）。
                    log.info("[%s] 第%d页 批量%s（第%d批/%d条）：勾选%d，点击=%s",
                             department, _page_no + 1, action_word,
                             chunk_no + 1, len(chunk), len(selected), clicked)
                    if clicked == "not_found":
                        result["failed"].extend({**item, "detail": "failed:batch_button_missing"}
                                                for item, _ in selected)
                    else:
                        remaining = list(selected)
                        clicked_word = clicked
                        for attempt in (1, 2):
                            if attempt == 2:
                                # 首试 0 翻才到这：表格可能已重渲染（09-07 勾选32
                                # 曾把 evaluate 打到导航销毁），重核行归属再勾再点，
                                # 核验不过的留 failed 走逐条精确搜索，绝不勾错计划。
                                retry_sel = []
                                for item, row in remaining:
                                    try:
                                        m2 = await _parse_plan_row(row)
                                    except Exception:
                                        m2 = None
                                    want_pid = str(item.get("plan_id") or "")
                                    if m2 is None or (want_pid and str(m2.plan_id or "") != want_pid):
                                        continue
                                    if desired_word in (m2.status or ""):
                                        result["enabled" if turn_on else "paused"].append(item)
                                        continue
                                    if await _set_row_checkbox(row, True):
                                        retry_sel.append((item, row))
                                remaining = retry_sel
                                if not remaining:
                                    break
                                clicked_word = await _batch_click(page, action_word)
                                log.info("[%s] 第%d页 批量%s：第2次点击=%s（%d条）",
                                         department, _page_no + 1, action_word,
                                         clicked_word, len(remaining))
                                if clicked_word == "not_found":
                                    break
                            modal_seen = False
                            for _ in range(20):
                                await page.wait_for_timeout(300)
                                if await page.evaluate("""() => Array.from(document.querySelectorAll('[role="dialog"], .ovui-modal, .oc-modal, [class*="modal"], [class*="dialog"], [class*="popconfirm"], [class*="popover"]')).some(d => d.offsetParent !== null && d.querySelectorAll('button').length > 0)"""):
                                    modal_seen = True
                                    break
                            confirmed = await _confirm_batch_action(page, action_word)
                            log.info("[%s] 第%d页 批量%s：弹窗=%s，确认=%s（第%d次）",
                                     department, _page_no + 1, action_word,
                                     modal_seen, confirmed, attempt)
                            deadline = time.monotonic() + 60
                            while remaining and time.monotonic() < deadline:
                                await page.wait_for_timeout(1000)
                                next_remaining = []
                                for item, row in remaining:
                                    status = await _row_status_text(row)
                                    if desired_word in status:
                                        result["enabled" if turn_on else "paused"].append(item)
                                    else:
                                        next_remaining.append((item, row))
                                remaining = next_remaining
                            if not remaining:
                                break
                            flipped = len(selected) - len(remaining)
                            if flipped > 0:
                                break  # 部分生效：剩余属状态卡死（如系统暂停），走逐条
                            if attempt == 1:
                                # 0 翻=整批静默失效（09-08 业务2部 56 条实测）：
                                # 落诊断（可见浮层/批量栏/气泡文案）再重试一次
                                hint = await page.evaluate("""() => {
                                    const isVis = el => el && el.offsetParent !== null;
                                    const out = [];
                                    for (const d of document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="popconfirm"], [class*="popover"], [class*="popper"], [class*="batch"], [class*="Batch"], [class*="message"], [class*="toast"], [class*="notify"]')) {
                                        if (!isVis(d)) continue;
                                        const t = (d.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
                                        if (t) out.push(t);
                                    }
                                    return out.slice(0, 6);
                                }""")
                                log.warning("[%s] 第%d页 批量%s 0 翻状态，可见浮层=%s，重试一次",
                                            department, _page_no + 1, action_word, hint)
                        # 这里不能用包含 await 的生成器表达式：会生成 async_generator，
                        # list.extend 无法消费，导致批量动作已执行后在汇总阶段抛 TypeError。
                        for item, row in remaining:
                            status = await _row_status_text(row)
                            result["failed"].append({
                                **item,
                                "detail": f"failed:batch={clicked_word};status={status}",
                            })
                        log.info("[%s] 第%d页 批量%s结果：翻状态%d，未翻%d",
                                 department, _page_no + 1, action_word,
                                 len(selected) - len(remaining), len(remaining))
            finally:
                for _, row in selected:
                    await _set_row_checkbox(row, False)
        total = await _read_pagination_total(page)
        if total is None or len(found) >= len(wanted) or not page_keys:
            break
        if not await _goto_next_page(page):
            break
    for key, item in wanted.items():
        if key not in found:
            result["failed"].append({**item, "detail": "target_not_found_after_full_scan"})
    return result


async def _batch_enable_targets(page, department: str, targets: list[dict]) -> dict:
    """扫描全表后按页批量开启目标。"""
    return await _batch_power_targets(page, department, targets, turn_on=True)


async def _batch_pause_targets(page, department: str, targets: list[dict]) -> dict:
    """扫描全表后按页批量暂停精确目标。"""
    return await _batch_power_targets(page, department, targets, turn_on=False)


async def _pause_department(
    department: str,
    dry_run: bool,
    *,
    record_midnight: bool = True,
) -> dict:
    """单部门：扫描记录投放中计划 → 全选 → 批量暂停 → 复查。"""
    plans = await list_all_plans(department)
    active = [p for p in plans if "投放中" in (p.status or "")]
    protected = [p for p in active if is_manual_whitelisted(
        department=department, plan_id=p.plan_id or "", plan_name=p.plan_name or "")]
    if protected:
        log.info("[%s] 人工白名单 %d 条将随批量暂停后恢复: %s", department, len(protected),
                 "、".join(p.plan_name or p.plan_id or "" for p in protected))
    log.info("[%s] 共 %d 条计划，投放中 %d 条", department, len(plans), len(active))

    if dry_run:
        return {"department": department, "active": len(active), "dry_run": True}

    if record_midnight:
        state = load_state()
        _merge_entries(state["midnight_paused"], [{
            "department": department,
            "plan_id": p.plan_id,
            "plan_name": p.plan_name,
            "paused_at": datetime.datetime.now().isoformat(timespec="seconds"),
        } for p in active])
        save_state(state)

    if not active:
        return {
            "department": department,
            "active": 0,
            "paused": 0,
            "paused_plan_ids": [],
            "still_active_plan_ids": [],
        }

    batch_error = None
    # 只把本次确实允许暂停的计划交给批量操作；人工白名单不加入目标，
    # 不再采用“全选暂停后再逐条开启例外”的慢路径。
    targets = [{
        "department": department, "plan_id": p.plan_id,
        "plan_name": p.plan_name,
    } for p in active if not is_manual_whitelisted(
        department=department, plan_id=p.plan_id or "", plan_name=p.plan_name or ""
    )]
    try:
        _ensure_cdp()
        async with async_playwright() as pw:
            browser = await connect_browser(pw, timeout=8000)
            page = await _department_page(browser.contexts[0], department)
            batch = await _batch_pause_targets(page, department, targets)
            paused_ids = {str(item.get("plan_id") or "") for item in batch["paused"]}
            already_ids = {str(item.get("plan_id") or "") for item in batch["already"]}
            log.info("[%s] 批量暂停完成：成功%d，已暂停%d，失败%d（目标%d）",
                     department, len(paused_ids), len(already_ids),
                     len(batch["failed"]), len(targets))
            # 失败条目才逐条回退，避免批量操作异常时静默漏停。
            for item in batch["failed"]:
                try:
                    ok, detail = await pause_plan(
                        page, item.get("plan_name") or "", item.get("plan_id"), department)
                except Exception as exc:
                    ok, detail = False, f"error:{str(exc)[:150]}"
                if ok:
                    paused_ids.add(str(item.get("plan_id") or ""))
                    log.warning("[%s] 批量暂停失败条目回退成功: %s (%s)",
                                department, item.get("plan_name"), detail)
                else:
                    log.error("[%s] 批量暂停失败条目回退失败: %s (%s)",
                              department, item.get("plan_name"), detail)
    except Exception as exc:
        batch_error = str(exc)[:200]
        log.error("[%s] 批量暂停未完成（未对全表逐条操作）: %s", department, batch_error)


    # 复查：还剩多少投放中
    plans_after = await list_all_plans(department)
    still_all = [p for p in plans_after if "投放中" in (p.status or "")]
    still = [p for p in still_all if not is_manual_whitelisted(
        department=department, plan_id=p.plan_id or "", plan_name=p.plan_name or "")]
    fallback_paused = 0
    if still:
        log.error("[%s] 批量暂停后仍有 %d 条投放中，改为逐条暂停并验证: %s",
                  department, len(still), "、".join(p.plan_name for p in still[:10]))
        _ensure_cdp()
        async with async_playwright() as pw:
            browser = await connect_browser(pw, timeout=8000)
            page = await _department_page(browser.contexts[0], department)
            for plan in still:
                ok, detail = await pause_plan(
                    page, plan.plan_name, plan.plan_id, department
                )
                if ok:
                    fallback_paused += 1
                    log.info("[%s] 逐条暂停成功: %s（%s）",
                             department, plan.plan_name, detail)
                else:
                    log.error("[%s] 逐条暂停失败: %s（%s）",
                              department, plan.plan_name, detail)
        plans_after = await list_all_plans(department)
        still_all = [p for p in plans_after if "投放中" in (p.status or "")]
        still = [p for p in still_all if not is_manual_whitelisted(
            department=department, plan_id=p.plan_id or "", plan_name=p.plan_name or "")]
    else:
        log.info("[%s] 复查通过：无投放中计划", department)
    still_all_ids = {p.plan_id for p in still_all if p.plan_id}
    paused_ids = [p.plan_id for p in active if p.plan_id and p.plan_id not in still_all_ids]
    return {
        "department": department,
        "active": len(active),
        "paused": len(paused_ids),
        "still_active": len(still),
        "paused_plan_ids": paused_ids,
        "still_active_plan_ids": [p.plan_id for p in still if p.plan_id],
        "still_protected_active": len(still_all) - len(still),
        "fallback_paused": fallback_paused,
        "batch_error": batch_error,
    }


async def _legacy_pause_all(dry_run: bool = False) -> dict:
    results = []
    for dept in DEPARTMENTS:
        try:
            results.append(await _pause_department(dept, dry_run))
        except Exception as exc:
            log.error("[%s] 批量暂停失败: %s", dept, exc)
            results.append({"department": dept, "error": str(exc)[:200]})
    return {"action": "pause-all", "results": results}


# ---------------------------------------------------------------------------
# 23:00 生命周期收口
# ---------------------------------------------------------------------------

def _daytime_preserved_categories(scan_slot: str) -> dict[str, str]:
    """Return categories explicitly established before this daytime scan.

    A potential/key plan is not reclassified from a momentary ROI dip before
    23:00.  This map combines today's earlier labels, the resume queue, and
    the catalog fallback so a 12:00/17:00 scan keeps the daytime identity.
    """
    allowed = {"potential_plan", "key_plan"}
    order = {"08:00": 0, "12:00": 1, "17:00": 2, "22:30": 3, "23:00": 4}
    out: dict[str, str] = {}
    try:
        from webui.db import list_plan_daily_labels, list_plan_catalog
        for row in list_plan_daily_labels(_today()):
            pid = str(row.get("plan_id") or "").strip()
            category = str(row.get("category") or "")
            if pid and category in allowed and pid not in out:
                out[pid] = category
        for row in list_plan_catalog():
            pid = str(row.get("plan_id") or "").strip()
            category = str(row.get("category") or "")
            if pid and category in allowed:
                out.setdefault(pid, category)
    except Exception as exc:
        log.warning("读取日间分类保持信息失败: %s", str(exc)[:120])
    try:
        state = load_state()
        for key in ("old_pool_enabled", "enabled", "key_kept"):
            for row in state.get(key) or []:
                pid = str(row.get("plan_id") or "").strip()
                category = str(row.get("category") or "")
                if pid and category in allowed:
                    out.setdefault(pid, category)
    except Exception as exc:
        log.warning("读取恢复队列分类保持信息失败: %s", str(exc)[:120])
    return out


def _yesterday_final_categories() -> dict[str, str]:
    """昨日 23:00 终分类（阶梯降级的事实源，独立于日间保持链）。

    2026-09-04 用户定：昨日重点计划破线先掉潜力、不直接掉老池。日间保持链
    （_daytime_preserved_categories）可能被当日错标污染，这里直接读昨日
    23:00 槽标签做权威依据。
    """
    try:
        from webui.db import list_plan_daily_labels
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        out: dict[str, str] = {}
        for row in list_plan_daily_labels(yesterday) or []:
            if str(row.get("scan_slot") or "") != "23:00":
                continue
            pid = str(row.get("plan_id") or "").strip()
            category = str(row.get("category") or "").strip()
            if pid and category:
                out[pid] = category
        return out
    except Exception as exc:
        log.warning("读取昨日终分类失败: %s", str(exc)[:120])
        return {}


def _lifecycle_entry(department: str, plan, *, final_classification: bool = False, preserved_category: str | None = None, prior_category: str | None = None) -> dict:
    """Convert overview metrics to an operating-stage or final lifecycle entry.

    Before 23:00, a plan being tested today remains labelled ``new_plan`` even
    after the 12:00 4.5 -> 7.3 parameter upgrade. Its final key/potential/old
    lifecycle category is calculated only by the 23:00 close run.
    """
    name = plan.plan_name or ""
    plan_date = _plan_date_from_name(name)
    try:
        current_roi_target = float(plan.roi_target) if plan.roi_target is not None else None
    except (TypeError, ValueError):
        current_roi_target = None
    try:
        current_budget = float(plan.budget) if float(plan.budget or 0) > 0 else None
    except (TypeError, ValueError):
        current_budget = None

    # 预约新计划（名称日期在明天及以后、尚未开投）：不是「当天测试零数据」，
    # 任何时刻都保持 new_plan 身份等投放日，23:00 终分类不降日常
    # （2026-09-04：0904_牛仔骨/肥牛片 09-03 23:00 被误标老池，次日收养又被
    # 「系统已见过」挡死——用户定「今天建好明天 8 点开启 = 新计划」）。
    try:
        zero_spend = float(plan.spent or 0) <= 0
    except (TypeError, ValueError):
        zero_spend = False
    if plan_date and plan_date > _today() and zero_spend:
        return {
            "department": department,
            "plan_id": plan.plan_id,
            "plan_name": plan.plan_name,
            "status": plan.status,
            "roi": plan.roi,
            "orders": plan.orders,
            "spent": plan.spent,
            "budget": current_budget,
            "category": "new_plan",
            "reason": f"预约新计划（投放日 {plan_date}），未到投放日不分类",
            "is_today_new": False,
            "future_scheduled": True,
            "new_plan_stage": "roi_4_5",
            "roi_target": current_roi_target or NEW_PLAN_ROI_TARGET,
            "current_roi_target": current_roi_target,
            "daily_budget": current_budget or NEW_PLAN_DAILY_BUDGET,
            "keep_running_at_close": False,
            "preserve_promoted_budget": False,
        }

    at_base_4_5 = (
        current_roi_target is not None
        and is_new_plan_roi(current_roi_target)
    )
    at_advanced_7_3 = (
        current_roi_target is not None
        and abs(current_roi_target - NEW_PLAN_NOON_ADVANCED_ROI) < 0.001
    )
    # ROI 4.5 also identifies a cross-day newly-added-material test. ROI 7.3
    # is a new-plan stage only when its name has today's delivery-date prefix.
    is_test_new = at_base_4_5 or (
        plan_date == _today()
        and (current_roi_target is None or at_base_4_5 or at_advanced_7_3)
    )

    if is_test_new and not final_classification:
        stage = "roi_7_3" if at_advanced_7_3 else "roi_4_5"
        reason = (
            "新计划已通过12:00 ROI4.5检查，按ROI7.3继续测试，23:00再分类"
            if stage == "roi_7_3"
            else "新计划ROI4.5测试中，23:00再分类"
        )
        return {
            "department": department,
            "plan_id": plan.plan_id,
            "plan_name": plan.plan_name,
            "status": plan.status,
            "roi": plan.roi,
            "orders": plan.orders,
            "spent": plan.spent,
            "budget": current_budget,
            "category": "new_plan",
            "reason": reason,
            "is_today_new": True,
            "new_plan_stage": stage,
            "roi_target": current_roi_target or NEW_PLAN_ROI_TARGET,
            "current_roi_target": current_roi_target,
            "daily_budget": current_budget,
            "keep_running_at_close": False,
            "preserve_promoted_budget": False,
        }

    # 盘中保持重点/潜力标签，只适用于仍使用该档参数的计划。若实际 ROI 已被
    # 降为日常 10.3，则不得用旧标签覆盖真实参数，否则会把日常计划继续播成潜力。
    demoted_to_old_pool = (
        current_roi_target is not None
        and abs(current_roi_target - OLD_POOL_ROI_TARGET) < 0.001
    )
    if (not final_classification
            and not demoted_to_old_pool
            and preserved_category in {"potential_plan", "key_plan"}):
        is_key = preserved_category == "key_plan"
        return {
            "department": department,
            "plan_id": plan.plan_id,
            "plan_name": plan.plan_name,
            "status": plan.status,
            "roi": plan.roi,
            "orders": plan.orders,
            "spent": plan.spent,
            "budget": current_budget,
            "category": preserved_category,
            "reason": ("日间保持重点计划，23:00再分类" if is_key else "日间保持潜力计划，23:00再分类"),
            "is_today_new": False,
            "new_plan_stage": "",
            "roi_target": KEY_PLAN_ROI_TARGET if is_key else POTENTIAL_PLAN_ROI_TARGET,
            "current_roi_target": current_roi_target,
            "daily_budget": KEY_PLAN_DAILY_BUDGET if is_key else POTENTIAL_PLAN_DAILY_BUDGET,
            "keep_running_at_close": is_key,
            "preserve_promoted_budget": False,
        }

    decision = classify_plan_lifecycle(
        roi=plan.roi,
        orders=plan.orders,
        is_today_new=is_test_new,
        name_label=name,
    )
    # 2026-09-04 用户定阶梯降级：昨日重点计划破线先掉潜力，不直接掉老池
    # （重点→潜力→日常逐层把关；潜力层后续由 12:00/17:00 潜力闸判定）。
    laddered_to_potential = False
    if decision.category == "old_pool" and prior_category == "key_plan":
        decision = LifecycleDecision(
            "potential_plan",
            "昨日重点计划破线，按阶梯降潜力计划（7.3/300）继续观察",
            POTENTIAL_PLAN_ROI_TARGET, POTENTIAL_PLAN_DAILY_BUDGET, False,
        )
        laddered_to_potential = True
    preserve_promoted_budget = bool(
        decision.category == "potential_plan"
        and is_test_new
        and at_advanced_7_3
    )
    return {
        "department": department,
        "plan_id": plan.plan_id,
        "plan_name": plan.plan_name,
        "status": plan.status,
        "roi": plan.roi,
        "orders": plan.orders,
        "spent": plan.spent,
        "budget": current_budget,
        "category": decision.category,
        "reason": decision.reason,
        "is_today_new": is_test_new,
        "new_plan_stage": "roi_7_3" if at_advanced_7_3 else ("roi_4_5" if at_base_4_5 else ""),
        # 阶梯降潜力的计划按档位值展示 7.3/300（页面漂移参数由对齐机制另改），
        # 与盘中保持分支的展示口径一致。
        "roi_target": decision.roi_target if laddered_to_potential else (
            current_roi_target if current_roi_target is not None else decision.roi_target),
        "current_roi_target": current_roi_target,
        "daily_budget": decision.daily_budget if laddered_to_potential else (
            current_budget if current_budget is not None else decision.daily_budget),
        "keep_running_at_close": decision.keep_running_at_close,
        "preserve_promoted_budget": preserve_promoted_budget,
    }


def _plan_date_from_name(plan_name: str) -> str | None:
    """从计划名前缀识别当日新计划，和台账分类保持一致。"""
    name = (plan_name or "").strip()
    match = re.match(r"^(\d{1,2})\s*[._/-]\s*(\d{1,2})(?:\D|$)", name)
    if not match:
        match = re.match(r"^(\d{2})(\d{2})(?:[_-]|\D)", name)
    if not match:
        match = re.match(r"^(\d)(\d{2})(?:[_-]|\D)", name)
    if not match:
        return None
    try:
        return datetime.date(datetime.date.today().year, int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError:
        return None


def _label_slot_now() -> str:
    """每日标签档位：手动扫描按当前时间归入定时档（08/12/17/23），与看板列对齐。"""
    hour = datetime.datetime.now().hour
    if 5 <= hour < 11:
        return "08:00"
    if 11 <= hour < 15:
        return "12:00"
    if 15 <= hour < 21:
        return "17:00"
    return "23:00"


def _apply_stale_label(entry: dict, stale_ids: set[str]) -> None:
    """已核验「创建满15天且近15天0单」的计划标记为无效计划（2026-08-24 白板策略⑤）。

    两个例外（2026-08-24 用户定）：
    - 人工白名单优先（人工自会处理）；
    - 有新素材加入（is_today_new，ROI 目标 4.5 重进测试）不覆盖——标签跟随新素材变化。
    其余分类一律覆盖，并强制 keep_running_at_close=False，
    避免数据突变被误判重点计划而在 23:00 收口后被重新开启。
    """
    if entry.get("category") == "manual_review":
        return
    if entry.get("is_today_new"):
        return
    if str(entry.get("plan_id") or "") in stale_ids:
        entry["category"] = "invalid_plan"
        entry["reason"] = "已核验 15 天 0 单，无效计划不自动恢复"
        entry["keep_running_at_close"] = False


async def monitor(dry_run: bool = False, scan_slot: str | None = None,
                  final_classification: bool | None = None) -> dict:
    """Read-only scan; final lifecycle labels are allowed only at 23:00."""
    slot = scan_slot or _label_slot_now()
    if final_classification is None:
        final_classification = scan_slot == "23:00" or (
            scan_slot is None and datetime.datetime.now().hour >= 23
        )
    result: dict[str, list[dict]] = {}
    stale_ids = _verified_stale_zero_order_ids()
    preserved_categories = _daytime_preserved_categories(slot) if not final_classification else {}
    prior_final_categories = _yesterday_final_categories()
    paused_at_17_ids = {
        str(item.get("plan_id") or "")
        for item in (load_state().get("new_plan_paused_at_17") or [])
        if item.get("plan_id")
    }
    for department in DEPARTMENTS:
        try:
            plans = await list_all_plans(department, time_range="今天")
            entries = []
            for plan in plans:
                entry = _lifecycle_entry(
                    department, plan, final_classification=bool(final_classification),
                    preserved_category=preserved_categories.get(str(plan.plan_id or "").strip()),
                    prior_category=prior_final_categories.get(str(plan.plan_id or "").strip()),
                )
                if is_manual_whitelisted(
                    creator=plan.creator_name or "", department=department,
                    plan_id=plan.plan_id or "", plan_name=plan.plan_name or "",
                ):
                    entry["category"] = "manual_review"
                    entry["reason"] = (
                        f"{manual_label(department=department, plan_id=plan.plan_id or '', plan_name=plan.plan_name or '')}"
                        "人工白名单，AI 不改计划"
                    )
                    entry["keep_running_at_close"] = False
                _apply_stale_label(entry, stale_ids)
                if (
                    not final_classification
                    and entry.get("is_today_new")
                    and str(entry.get("plan_id") or "") in paused_at_17_ids
                    and "暂停" in str(entry.get("status") or "")
                ):
                    entry["paused_at_17"] = True
                    entry["reason"] = "17:00 ROI<6 日内暂停，保持新计划，23:00再分类"
                entries.append(entry)
            result[department] = entries
        except Exception as exc:
            # 扫描失败必须留痕：2026-08-24 浏览器崩溃后 17:00 熔断/补扫两次空转，
            # 异常被吞进 result 无任何日志，事后无法定位。
            log.error("[%s] 扫描失败: %s", department, str(exc)[:200])
            result[department] = [{"error": str(exc)[:200]}]

    counts: dict[str, int] = {}
    for entries in result.values():
        for entry in entries:
            category = entry.get("category")
            if category:
                counts[category] = counts.get(category, 0) + 1
    # 对账基座：每次扫描同步进计划库。数据库故障绝不能影响动作，吞掉异常——
    # 但必须记日志：2026-08-24 DB Browser 挂写事务锁库两小时，落库全灭而
    # 外界毫无感知（except: pass 的代价）。
    try:
        from webui.db import record_plan_daily_labels, sync_lifecycle_catalog

        sync_lifecycle_catalog(result)
        # 计划每日标签：每次扫描按 slot 留痕（2026-08-24 起）
        record_plan_daily_labels(result, scan_slot=slot)
    except Exception as exc:
        log.error("扫描落库失败（标签/目录未更新）: %s", str(exc)[:200])
    return {
        "action": "monitor", "dry_run": dry_run, "scan_slot": slot,
        "final_classification": bool(final_classification),
        "counts": counts, "plans": result,
    }


async def close_non_key(dry_run: bool = False) -> dict:
    """23:00 暂停非重点计划，重点计划和人工白名单不自动改动。"""
    report = await monitor(dry_run=True)
    targets_by_department: dict[str, list[dict]] = {}
    kept: list[dict] = []
    whitelist: list[dict] = []

    for department, entries in report["plans"].items():
        for entry in entries:
            if entry.get("error"):
                continue
            if entry["category"] == "manual_review":
                whitelist.append(entry)
            elif entry["keep_running_at_close"]:
                kept.append(entry)
            elif "投放中" in (entry.get("status") or ""):
                targets_by_department.setdefault(department, []).append(entry)

    targets = [item for items in targets_by_department.values() for item in items]
    summary = {
        "action": "close-non-key",
        "dry_run": dry_run,
        "kept_key_plans": kept,
        "manual_whitelist": whitelist,
        "targets": targets,
        "paused": [],
        "failed": [],
    }
    if dry_run:
        return summary

    state = load_state()
    _merge_entries(state["key_kept"], kept)
    save_state(state)
    for department, items in targets_by_department.items():
        _ensure_cdp()
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await open_fresh_page(browser.contexts[0])
                for item in items:
                    ok, detail = await pause_plan(
                        page, item["plan_name"] or "", item["plan_id"], department
                    )
                    if ok:
                        paused_item = {**item, "detail": detail}
                        summary["paused"].append(paused_item)
                        if paused_item.get("category") == "old_pool":
                            # 每条成功暂停后立刻落跨日队列，长批次跨零点也不丢失恢复名单。
                            _queue_old_pool_resume([paused_item])
                        log.info("[%s] 23:00 暂停非重点计划: %s", department, item["plan_name"])
                    else:
                        summary["failed"].append({**item, "detail": detail})
                        log.error("[%s] 23:00 暂停失败: %s (%s)", department, item["plan_name"], detail)
        except Exception as exc:
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)

    state = load_state()
    _merge_entries(state["closed_non_key"], summary["paused"])
    old_pool_paused = [item for item in summary["paused"] if item.get("category") == "old_pool"]
    _merge_entries(state["old_pool_paused"], old_pool_paused)
    save_state(state)
    _queue_old_pool_resume(old_pool_paused)
    _mark_lifecycle_ready({**report, **summary})
    return summary


async def close_old_pool(dry_run: bool = False) -> dict:
    """23:00 分类收口：除重点计划外全部暂停（2026-08-21 起）。

    批量暂停是页面上最稳定、最快的动作；暂停后只把重点计划逐条恢复。
    恢复队列按类别分流：潜力挂次日 08:00，日常计划（含 23:00 降级的
    新测试，2026-08-24 新策略）挂次日 12:00。人工白名单两侧都排除。
    常规潜力计划在批量暂停前改写为 ROI 7.3、日预算 300；已在12:00
    升档的新计划保留其“剩余预算补足300元”后的预算。未达标/零数据新计划
    先改写为日常参数 ROI 10.3、日预算 300。
    """
    report = await monitor(dry_run=True, scan_slot="23:00")
    new_plan_migrations: list[dict] = []
    potential_targets: list[dict] = []
    active_entries: dict[str, list[dict]] = {}
    exception_entries: dict[str, list[dict]] = {}
    scan_errors: list[dict] = []
    for department, entries in report["plans"].items():
        for entry in entries:
            if entry.get("error"):
                # 扫描失败的部门显式可见（此前被静默跳过，曾导致整部门未收口）
                scan_errors.append({"department": department, "error": entry["error"]})
                continue
            status = str(entry.get("status") or "")
            is_running = "投放中" in status
            # 17:00 被风险暂停的新计划仍需在23:00完成最终分类；此前跳过暂停行会
            # 让它永远卡在 new_plan，且无法在23:00再改写为老池/潜力的最终参数。
            # 2026-10-10 收紧：必须「有消耗」才算 17:00 风险暂停——建成即暂停
            # （挂 08:00 恢复、从未投放）的计划零数据是天经地义，吃这条会被
            # 误判「未达标转老池」丢掉新计划 08:00 票（阿娇/嘉嘉/草莓/木子美事故）。
            try:
                _spent = float(entry.get("spent") or 0)
            except (TypeError, ValueError):
                _spent = 0.0
            is_17_paused_new = (entry.get("is_today_new") and "暂停" in status
                                and _spent > 0)
            if not is_running and not is_17_paused_new:
                continue
            if entry.get("is_today_new") and entry.get("category") in ("old_pool", "new_plan"):
                # 测试计划未达标（含 23:00 仍零数据）：改成日常计划参数。
                # 17:00 已暂停的同类计划也在这里完成正式降级，不会提前在17:00改标签。
                new_plan_migrations.append(entry)
            if entry.get("category") == "potential_plan":
                potential_targets.append({**entry, "department": department})
            if is_running:
                active_entries.setdefault(department, []).append(entry)
                # 批量暂停后只有重点计划恢复投放；潜力/日常进入恢复队列。
                if entry.get("category") == "key_plan":
                    exception_entries.setdefault(department, []).append(entry)

    # 不能只依赖“本次刚刚暂停成功”的计划。补入已暂停但经15天报告确认
    # 有订单的老池计划；人工白名单和已核验无效计划排除。
    recent_order_map = _load_verified_recent_order_ids()
    stale_ids = _verified_stale_zero_order_ids()
    catalog_seen = _catalog_last_seen_map()  # None=读失败本轮不校验
    ghost_skipped: list[dict] = []
    queued_old_pool_candidates: list[dict] = []
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for plan_id, evidence in recent_order_map.items():
        if _recent_order_ghost(plan_id, catalog_seen):
            ghost_skipped.append({
                "plan_id": plan_id,
                "plan_name": evidence.get("plan_name") or plan_id,
                "department": evidence.get("department") or "",
            })
            continue
        if plan_id in stale_ids or is_manual_whitelisted(
            department=str(evidence.get("department") or ""),
            plan_id=plan_id,
            plan_name=str(evidence.get("plan_name") or ""),
        ):
            continue
        if any(str(x.get("plan_id") or "") == plan_id for x in new_plan_migrations + potential_targets):
            continue
        queued_old_pool_candidates.append({
            "plan_id": plan_id,
            "plan_name": evidence.get("plan_name") or plan_id,
            "department": evidence.get("department") or "",
            "category": "old_pool",
            "status": evidence.get("status") or "已暂停",
            "roi": None,
            "orders": evidence.get("near_15_days_orders") or 0,
            "spent": evidence.get("near_15_days_spent") or 0,
            "roi_target": OLD_POOL_ROI_TARGET,
            "daily_budget": OLD_POOL_DAILY_BUDGET,
            "reason": "近15天有订单，补入老计划池次日12:00恢复候选",
            "resume_date": tomorrow,
            "resume_time": "12:00",
        })
    if ghost_skipped:
        log.warning(
            "回补存活校验跳过 %d 条平台已删计划（15天报告快照残留）: %s",
            len(ghost_skipped),
            "、".join(str(x["plan_name"]) for x in ghost_skipped[:10]),
        )

    summary = {
        "action": "close-old-pool",
        "dry_run": dry_run,
        "scan_errors": scan_errors,
        "targets": [item for items in active_entries.values() for item in items
                    if item.get("category") == "old_pool"],
        "recent_order_recovery_candidates": queued_old_pool_candidates,
        "recent_order_ghost_skipped": ghost_skipped,
        "new_plan_migrations": new_plan_migrations,
        "prepaused_new_plan_migrations": [
            item for item in new_plan_migrations if "暂停" in str(item.get("status") or "")
        ],
        "migrated_to_old_pool": [],
        "migration_failed": [],
        "potential_param_targets": potential_targets,
        "potential_param_changed": [],
        "potential_param_preserved": [],
        "potential_param_failed": [],
        "batch": [],
        "paused": [],
        "reopened": [],
        "reopen_failed": [],
        "failed": [],
    }
    if dry_run:
        return summary

    if queued_old_pool_candidates:
        _queue_old_pool_resume(
            queued_old_pool_candidates, resume_date=tomorrow, resume_time="12:00"
        )
        log.info(
            "补入近15天有订单老池恢复候选: %d 条，挂次日12:00",
            len(queued_old_pool_candidates),
        )

    if new_plan_migrations:
        # 复用已验证的单计划编辑流程：按 ID 定位，写 ROI 10.3、预算 300，再保存。
        from .old_plan_pool import apply_old_pool

        # 17:00 已暂停的新计划在23:00才允许正式改写为老池参数；常规
        # 在投迁移仍沿用默认保护，避免覆盖人工暂停意图。
        has_prepaused_migration = any(
            "暂停" in str(item.get("status") or "") for item in new_plan_migrations
        )
        migration = await apply_old_pool(
            new_plan_migrations, **({"allow_paused": True} if has_prepaused_migration else {})
        )
        migrated_ids = set(migration.get("changed") or [])
        for item in new_plan_migrations:
            if item.get("plan_id") in migrated_ids:
                # 参数已落 10.3/300：身份同步为日常计划（dict 与 active_entries 共享引用），
                # 后续 _queue_old_pool_resume 按 old_pool 挂次日 12:00，状态记录口径一致。
                item["category"] = "old_pool"
                item["reason"] = "23:00 新计划未达标/零数据，转日常计划"
                item["roi_target"] = OLD_POOL_ROI_TARGET
                item["daily_budget"] = OLD_POOL_DAILY_BUDGET
        summary["migrated_to_old_pool"] = [
            item for item in new_plan_migrations if item.get("plan_id") in migrated_ids
        ]
        summary["migration_failed"] = list(migration.get("failed") or []) + list(migration.get("skipped") or [])
        # 已在17:00暂停、现已于23:00正式转老池的计划无需再参与批量暂停，
        # 直接挂次日12:00老池窗口恢复。
        prepaused_ids = {
            str(item.get("plan_id") or "") for item in summary["prepaused_new_plan_migrations"]
        }
        prepaused_migrated = [
            {**item, "category": "old_pool"}
            for item in summary["migrated_to_old_pool"]
            if str(item.get("plan_id") or "") in prepaused_ids
        ]
        if prepaused_migrated:
            _queue_old_pool_resume(prepaused_migrated)

    if potential_targets:
        # A new plan promoted at 12:00 already has ROI 7.3 and a budget sized
        # to retain 300 yuan of remaining spend. 23:00 only assigns its final
        # potential label; it must not overwrite that calculated budget with 300.
        preserved = [item for item in potential_targets if item.get("preserve_promoted_budget")]
        rewrite_targets = [item for item in potential_targets if not item.get("preserve_promoted_budget")]
        summary["potential_param_preserved"] = preserved
        if rewrite_targets:
            from .old_plan_pool import apply_old_pool

            applied = await apply_old_pool(
                rewrite_targets,
                roi=POTENTIAL_PLAN_ROI_TARGET,
                budget=POTENTIAL_PLAN_DAILY_BUDGET,
            )
            changed_ids = set(applied.get("changed") or [])
            summary["potential_param_changed"] = [
                item for item in rewrite_targets if item.get("plan_id") in changed_ids
            ]
            summary["potential_param_failed"] = list(applied.get("failed") or [])

    # 先按部门批量暂停全部非白名单在投计划，例外稍后恢复。
    for department in active_entries:
        try:
            batch_result = await _pause_department(
                department, dry_run=False, record_midnight=False
            )
            summary["batch"].append(batch_result)
            paused_ids = set(batch_result.get("paused_plan_ids") or [])
            for item in active_entries[department]:
                if item.get("plan_id") not in paused_ids:
                    continue
                paused_item = {**item, "detail": "批量暂停后复查确认"}
                summary["paused"].append(paused_item)
                if item.get("category") != "key_plan":
                    # 队列内部按类别分流恢复时间：潜力 08:00 / 日常 12:00；
                    # 重点计划在下方恢复环节逐条开启，不进队列。
                    _queue_old_pool_resume([paused_item])
                    log.info("[%s] 批量暂停%s: %s", department,
                             item.get("category"), item.get("plan_name"))
        except Exception as exc:
            summary["failed"].append({
                "department": department,
                "error": str(exc)[:200],
            })

    # 批量暂停后只恢复重点计划；潜力/日常已挂入分流恢复队列（08:00/12:00）。
    _ensure_cdp()
    for department, items in exception_entries.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                context = browser.contexts[0]
                page = await _department_page(context, department)
                for item in items:
                    result = await _enable_one(page, {**item, "time_range": None})
                    if result in ("enabled", "already"):
                        summary["reopened"].append({**item, "detail": result})
                        log.info("[%s] 批量暂停后恢复重点计划: %s",
                                 department, item.get("plan_name"))
                    else:
                        summary["reopen_failed"].append({**item, "detail": result})
                        log.error("[%s] 恢复重点计划失败: %s (%s)",
                                  department, item.get("plan_name"), result)
        except Exception as exc:
            summary["reopen_failed"].extend(
                {**item, "detail": str(exc)[:200]} for item in items
            )

    # 收口尾巴：预算归位到标签预算（2026-09-02 用户定：重点 1000、其余 300，
    # 无白名单豁免）。report 里的分类已是 23:00 终分类（迁移条目上面已同步为 old_pool）。
    try:
        summary["budget_normalized"] = await normalize_budgets_by_label(report)
    except Exception as exc:
        log.error("预算归位异常（不影响收口结果）: %s", str(exc)[:200])
        summary["budget_normalized"] = {"error": str(exc)[:200]}

    # 收口尾巴 2：15 天核验报告超龄则拉起独立重跑进程（2026-09-15 幽灵票根治）。
    try:
        summary["order_report_refresh"] = _maybe_spawn_order_report_refresh()
    except Exception as exc:
        log.error("15天报告重跑闸异常（不影响收口结果）: %s", str(exc)[:150])
        summary["order_report_refresh"] = f"error:{str(exc)[:100]}"

    state = load_state()
    old_pool_paused = [item for item in summary["paused"] if item.get("category") == "old_pool"]
    # 补回本次先前已实际暂停、但进程中断前尚未来得及写入日状态的计划。
    next_resume_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    queued_old_pool = [
        item for item in _load_old_pool_resume_queue().get("pending") or []
        if item.get("category") == "old_pool" and item.get("resume_date") == next_resume_date
    ]
    _merge_entries(state["old_pool_paused"], queued_old_pool)
    _merge_entries(state["old_pool_paused"], old_pool_paused)
    save_state(state)
    _mark_lifecycle_ready({
        **report,
        **summary,
        "kept_key_plans": [item for items in exception_entries.values() for item in items],
    })
    return summary


async def pause_all(dry_run: bool = False) -> dict:
    """旧 API 兼容入口：不再全量暂停，改执行 23:00 的非重点收口规则。"""
    result = await close_non_key(dry_run)
    result["legacy_alias"] = "pause-all 已改为 close-non-key"
    return result


# ---------------------------------------------------------------------------
# 23:00 收口尾巴：预算归位到标签预算（2026-09-02 用户定）
# ---------------------------------------------------------------------------

# 重点 1000、其余 300；无白名单豁免（恩豆已出白名单按重点计）。
# 当天告警加钱（+300/+200）/升档补足造成的偏离只在当天有效，夜里归位。
_LABEL_BUDGETS = {
    "key_plan": KEY_PLAN_DAILY_BUDGET,
    "potential_plan": POTENTIAL_PLAN_DAILY_BUDGET,
    "new_plan": NEW_PLAN_DAILY_BUDGET,
    "old_pool": OLD_POOL_DAILY_BUDGET,
}

# 2026-09-03 用户定：ROI 目标也按标签归位（此前只归预算，旧潜力 7.3/古早 8.6
# 的日常计划永远没人改回 10.3，reconcile 天天报「参数漂移」但无自愈）。
_LABEL_ROIS = {
    "key_plan": KEY_PLAN_ROI_TARGET,
    "potential_plan": POTENTIAL_PLAN_ROI_TARGET,
    "new_plan": NEW_PLAN_ROI_TARGET,
    "old_pool": OLD_POOL_ROI_TARGET,
}


def budget_drift_targets(report: dict, roi_overrides: dict | None = None) -> list[dict]:
    """从收口扫描报告挑出参数偏离标签值的计划（纯函数，便于单测）。

    预算：entry 的 ``budget`` 字段是列表扫描读到的当前日预算；None/0 视为读不出，
    不动（避免误伤）。偏离 <1 元不动。
    ROI（2026-09-03 起）：``roi_target`` 读不出不动；与标签值差 ≥0.01 才算漂移。
    两者任一漂移即入名单，执行时按 budget_drift/roi_drift 分项改写。
    manual_review/invalid_plan 不在表内。
    roi_overrides（2026-10-09）：{plan_id: 意图 ROI}——旧素材通道 6.5 等
    特殊通道计划按建票意图值核对，不按分类默认 4.5。
    """
    roi_overrides = roi_overrides or {}
    targets: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error"):
                continue
            category = entry.get("category")
            want = _LABEL_BUDGETS.get(category)
            want_roi = _LABEL_ROIS.get(category)
            if not want or want_roi is None:
                continue
            override = roi_overrides.get(str(entry.get("plan_id") or ""))
            if override is not None:
                want_roi = override
            try:
                current = float(entry.get("budget") or 0)
            except (TypeError, ValueError):
                current = 0.0
            try:
                current_roi = float(entry.get("roi_target"))
            except (TypeError, ValueError):
                current_roi = None
            budget_drift = current > 0 and abs(current - float(want)) >= 1.0
            roi_drift = (current_roi is not None
                         and abs(current_roi - float(want_roi)) >= 0.01)
            if not budget_drift and not roi_drift:
                continue
            targets.append({
                **entry, "department": department,
                "current_budget": current, "label_budget": float(want),
                "current_roi": current_roi, "label_roi": float(want_roi),
                "budget_drift": budget_drift, "roi_drift": roi_drift,
            })
    return targets


async def _set_budget_absolute(page, item: dict, value: float | None,
                               *, roi: float | None = None) -> dict:
    """编辑页把计划日预算/ROI 目标写成绝对值（哪个给了写哪个）。

    返回 {before, after, roi_after}；失败抛异常。
    """
    from .budget_raise import _read_budget, _set_budget
    from .old_plan_pool import _set_roi, _wait_edit_inputs

    department = str(item.get("department") or "")
    plan_id = str(item.get("plan_id") or "")
    plan_name = str(item.get("plan_name") or "")
    rows = await _search_plans(
        page, plan_id or plan_name, department, by_creator=False,
        time_range="last_7_days")
    target_row = None
    for row in rows:
        metrics = await _parse_plan_row(row)
        if metrics is None:  # 畸形行已留 WARNING，跳过
            continue
        if (plan_id and str(metrics.plan_id) == plan_id) or (
            not plan_id and metrics.plan_name == plan_name
        ):
            target_row = row
            break
    if target_row is None:
        raise RuntimeError("参数归位未找到计划行（last_7_days 按 ID 搜索）")
    await _open_edit_page(page, target_row)
    await _wait_stable(page, 1.5)
    await _wait_edit_inputs(page)
    out: dict = {}
    if roi is not None:
        await _set_roi(page, roi)
        out["roi_after"] = roi
    if value is not None:
        before = await _read_budget(page)
        await _set_budget(page, int(value))
        out["before"] = before
        out["after"] = value
    if not out:
        return out
    if not await _save_edit_page(page):
        raise RuntimeError("参数归位未找到保存按钮")
    return out


async def normalize_budgets_by_label(report: dict, *, dry_run: bool = False) -> dict:
    """把预算/ROI 偏离标签值的计划逐条改回标签参数（23:00 收口尾巴调用）。

    ROI 意图值优先取看板建票时的 roi_target（旧素材通道 6.5 等），取不到
    才回退分类默认值——防止把特殊通道计划当漂移打回 4.5（2026-10-09）。
    """
    roi_overrides: dict = {}
    try:
        from webui.db import get_latest_label_roi

        roi_overrides = get_latest_label_roi([
            str(e.get("plan_id") or "")
            for entries in (report.get("plans") or {}).values()
            for e in entries
        ])
    except Exception as exc:
        log.warning("看板 ROI 意图值读取失败，归位退回分类默认值: %s", str(exc)[:120])
    targets = budget_drift_targets(report, roi_overrides=roi_overrides)
    summary = {"targets": targets, "changed": [], "failed": [], "dry_run": dry_run}
    if dry_run or not targets:
        return summary
    by_dep: dict[str, list[dict]] = {}
    for item in targets:
        by_dep.setdefault(str(item.get("department") or ""), []).append(item)
    _ensure_cdp()
    for department, items in by_dep.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                for item in items:
                    want_budget = item["label_budget"] if item.get("budget_drift") else None
                    want_roi = item.get("label_roi") if item.get("roi_drift") else None
                    try:
                        r = await _set_budget_absolute(page, item, want_budget, roi=want_roi)
                        summary["changed"].append({**item, **r})
                        log.info("[%s] 参数归位: %s 预算%s→%s ROI%s→%s（%s）", department,
                                 item.get("plan_name"),
                                 item.get("current_budget"), r.get("after", "-"),
                                 item.get("current_roi"), r.get("roi_after", "-"),
                                 item.get("category"))
                    except Exception as exc:
                        log.warning("[%s] 参数归位异常，刷新重试: %s (%s)",
                                    department, item.get("plan_name"), str(exc)[:120])
                        try:
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(5000)
                            r = await _set_budget_absolute(page, item, want_budget, roi=want_roi)
                            summary["changed"].append({**item, **r})
                        except Exception as exc2:
                            summary["failed"].append(
                                {**item, "detail": str(exc2)[:150]})
                            log.error("[%s] 参数归位失败: %s (%s)", department,
                                      item.get("plan_name"), str(exc2)[:150])
        except Exception as exc:
            log.error("[%s] 参数归位部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)
    log.info("参数归位完成：偏离 %d，改回 %d，失败 %d",
             len(targets), len(summary["changed"]), len(summary["failed"]))
    return summary


# ---------------------------------------------------------------------------
# 盘中预算安全闸（2026-09-15 费用安全件）：99999 拦截 + 漂移下压
# ---------------------------------------------------------------------------
# 99999=「不限预算」在当前策略下永不合法（ROI>10 改不限的 growth 开关早已
# 关闭），任何时刻扫到一律按档位写回；漂移只下压不上抬——抬预算是升档/
# 告警逻辑的活，23:00 夜归位管双向（本闸只管「烧得快」的方向）。当天升档
# 补足（剩余 300+）的新计划是合法超额，跳过。无白名单豁免（与 09-02 夜归位
# 同口径）。挂点=:15/:45 监控播报（30 分钟一轮，已持动作锁+忙标）。
SAFETY_UNLIMITED_BUDGET = 99999.0
# 无看板标签的计划撞到 99999 时的兜底写回值（档位里最保守的一档）。
SAFETY_DEFAULT_CLAMP_BUDGET = float(OLD_POOL_DAILY_BUDGET)


def _field(obj, key):
    """dict 与 PlanMetrics 对象双兼容取字段。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def budget_safety_clamp_targets(plans_by_dep: dict, board: dict[str, str],
                                promoted_ids: set[str],
                                skip_ids: set[str] | None = None) -> list[dict]:
    """从一次全量扫描挑出须立即下压预算的计划（纯函数，便于单测）。

    board=今日看板标签（pid→category）；promoted_ids=当天已升档新计划。
    skip_ids=ignored 名单 ∪ 人工白名单（平台系统暂停/主管人工管理：已暂停不
    烧钱或「程序完全不管」，每 30 分钟白试编辑页只会撞平台阻断或压了不该压
    的——09-15 铭妈两连败 + 09-16 味蕾食光/恩豆/陈树莉误压双实证）。
    规则：预算读不出不动；预算≥99999（不限）→ 写回档位预算（无标签兜底
    300）；预算超档位 1 元以上 → 下压回档位（只下压不上抬）；升档新计划
    （仍是 new_plan 标签）跳过；无标签计划只拦 99999；平台「预算修改幅度
    不能小于 100 元」，差额不足 100 的漂移写了也静默落空（09-15 我家厨房
    308→300 三次「成功」纹丝不动实证），直接跳过不空转。
    """
    targets: list[dict] = []
    skipped = skip_ids or set()
    for department, plans in (plans_by_dep or {}).items():
        for plan in plans or []:
            pid = str(_field(plan, "plan_id") or "").strip()
            if not pid or pid in skipped:
                continue
            try:
                current = float(_field(plan, "budget") or 0)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue
            label = str(board.get(pid) or "")
            if pid in promoted_ids and label == "new_plan":
                continue  # 升档补足（剩余 300+）是合法超额
            want = _LABEL_BUDGETS.get(label)
            if current >= SAFETY_UNLIMITED_BUDGET:
                target = float(want) if want else SAFETY_DEFAULT_CLAMP_BUDGET
                reason = "unlimited_intercept"
            elif want and current - float(want) >= 1.0:
                target = float(want)
                reason = "over_cap"
            else:
                continue
            # 平台规则「预算修改幅度不能小于 100 元」：差额不足 100 的写回会
            # 被平台静默拒（无 toast、保存按钮照点），不纳入目标避免每轮空转。
            if abs(current - target) < 100.0:
                continue
            targets.append({
                "plan_id": pid,
                "plan_name": str(_field(plan, "plan_name") or pid),
                "department": str(department or ""),
                "status": str(_field(plan, "status") or ""),
                "category": label,
                "current_budget": current,
                "label_budget": target,
                "reason": reason,
            })
    return targets


async def clamp_budget_safety(targets: list[dict], *, dry_run: bool = False) -> dict:
    """把盘中预算安全闸目标逐条写回（编辑页绝对值，只写预算不写 ROI）。"""
    summary = {"targets": targets, "changed": [], "failed": [], "dry_run": dry_run}
    if dry_run or not targets:
        return summary
    by_dep: dict[str, list[dict]] = {}
    for item in targets:
        by_dep.setdefault(str(item.get("department") or ""), []).append(item)
    _ensure_cdp()
    for department, items in by_dep.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                for item in items:
                    try:
                        r = await _set_budget_absolute(page, item, item["label_budget"])
                        summary["changed"].append({**item, **r})
                        log.info("[%s] 预算安全闸: %s 预算 %s→%s（%s）", department,
                                 item.get("plan_name"), item.get("current_budget"),
                                 r.get("after", item["label_budget"]),
                                 item.get("reason"))
                    except Exception as exc:
                        log.warning("[%s] 预算安全闸异常，刷新重试: %s (%s)",
                                    department, item.get("plan_name"), str(exc)[:120])
                        try:
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(5000)
                            r = await _set_budget_absolute(page, item, item["label_budget"])
                            summary["changed"].append({**item, **r})
                        except Exception as exc2:
                            summary["failed"].append(
                                {**item, "detail": str(exc2)[:150]})
                            log.error("[%s] 预算安全闸失败: %s (%s)", department,
                                      item.get("plan_name"), str(exc2)[:150])
        except Exception as exc:
            log.error("[%s] 预算安全闸部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)
    log.info("预算安全闸完成：目标 %d，改回 %d，失败 %d",
             len(targets), len(summary["changed"]), len(summary["failed"]))
    return summary


def _old_pool_delivery_window(hhmm: str | None = None) -> bool:
    """老计划池仅在 12:00-17:10 的日间投放窗口允许自动恢复。"""
    now = hhmm or datetime.datetime.now().strftime("%H:%M")
    return "12:00" <= now < "17:10"


async def restore_pause_exceptions(dry_run: bool = False) -> dict:
    """恢复投放窗口内被误暂停的自动计划。

    包含新计划、潜力/重点计划，以及 12:00-17:10 窗口内的老计划池；
    人工白名单、系统暂停与已核验的无效计划始终不自动恢复。
    """
    report = await monitor(dry_run=True)
    snapshot = _save_pause_snapshot(report, "restore-pause-exceptions")
    targets_by_department: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    for department, entries in report["plans"].items():
        for item in entries:
            status = str(item.get("status") or "")
            if item.get("error") or "暂停" not in status:
                continue
            if "系统暂停" in status:
                skipped.append({**item, "detail": "系统暂停，不自动恢复"})
                continue
            if is_manual_whitelisted(
                department=department,
                plan_id=str(item.get("plan_id") or ""),
                plan_name=str(item.get("plan_name") or ""),
            ):
                skipped.append({**item, "detail": "人工白名单，不自动恢复"})
                continue
            category = str(item.get("category") or "")
            roi_target = item.get("roi_target")
            is_new_test = roi_target is not None and is_new_plan_roi(roi_target)
            should_restore = (
                category in {"new_plan", "potential_plan", "key_plan"}
                or is_new_test
                or (category == "old_pool" and _old_pool_delivery_window())
            )
            if should_restore:
                targets_by_department.setdefault(department, []).append({
                    **item,
                    "time_range": None,
                })

    targets = [item for items in targets_by_department.values() for item in items]
    summary = {
        "action": "restore-pause-exceptions",
        "dry_run": dry_run,
        "snapshot": str(snapshot),
        "targets": targets,
        "skipped": skipped,
        "enabled": [],
        "already": [],
        "failed": [],
    }
    if dry_run:
        return summary

    _ensure_cdp()
    for department, items in targets_by_department.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                batch = await _batch_enable_targets(page, department, items)
                summary["enabled"].extend(batch["enabled"])
                summary["already"].extend(batch["already"])
                for item in batch["enabled"]:
                    log.info("[%s] 批量恢复暂停例外: %s", department, item.get("plan_name"))
                # 批量操作后仍显示暂停的才逐条回退；不采用先全停再逐条开的方式。
                for item in batch["failed"]:
                    try:
                        result = await _enable_one(page, item)
                    except Exception as exc:
                        result = f"error:{str(exc)[:150]}"
                    record = {**item, "detail": result, "enable_mode": "fallback"}
                    if result == "enabled":
                        summary["enabled"].append(record)
                        log.info("[%s] 恢复暂停例外单条补开: %s", department, item.get("plan_name"))
                    elif result == "already":
                        summary["already"].append(record)
                    else:
                        summary["failed"].append(record)
                        log.error("[%s] 恢复暂停例外失败: %s (%s)",
                                  department, item.get("plan_name"), result)
        except Exception as exc:
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)
    return summary


# ---------------------------------------------------------------------------
# 手动新建计划收养（2026-09-03 用户定：矩阵号素材/建计划人工做，系统接管生命周期）
# ---------------------------------------------------------------------------

def _seen_plan_ids() -> set[str]:
    """系统见过的 plan_id 全集（每日看板+台账）。人工新建零消耗计划不在其中。"""
    ids: set[str] = set()
    try:
        from webui.db import list_plan_catalog, list_plan_daily_board
        for row in list_plan_daily_board() or []:
            pid = str(row.get("plan_id") or "").strip()
            if pid:
                ids.add(pid)
        for row in list_plan_catalog() or []:
            pid = str(row.get("plan_id") or "").strip()
            if pid:
                ids.add(pid)
    except Exception as exc:
        log.warning("读取已见计划集合失败: %s", str(exc)[:120])
    return ids


def _fresh_manual_candidates(
    plans: list, *, department: str, seen_ids: set[str],
    pending_keys: set[str], stale_ids: set[str],
) -> list:
    """从近7天全量列表筛「人工新建、系统未接管」的计划（纯函数，便于单测）。

    判定：已暂停 + 零消耗 + 无待执行恢复票 + 非人工白名单 + 不在已核验无效名单。
    投放中的不碰（23:00 收口会分类）；有消耗但被暂停的不碰（可能是人工主观
    停投）；名称不要求日期前缀。
    2026-09-04：系统「见过」不再挡收养——预约新计划（明日前缀）曾被 23:00
    误标老池，次日收养被 _seen_plan_ids 挡死。见过的计划要求仍是新计划形态
    （ROI 目标 4.5）才收，防止把已归类的老池计划反复收养归一。
    """
    out: list = []
    for p in plans:
        pid = str(getattr(p, "plan_id", "") or "").strip()
        name = str(getattr(p, "plan_name", "") or "")
        if not pid:
            continue
        if "暂停" not in str(getattr(p, "status", "") or ""):
            continue
        try:
            if float(getattr(p, "spent", 0) or 0) > 0:
                continue
        except (TypeError, ValueError):
            continue
        if pid in seen_ids:
            try:
                seen_roi = float(getattr(p, "roi_target", None) or 0)
            except (TypeError, ValueError):
                continue
            if abs(seen_roi - NEW_PLAN_ROI_TARGET) >= 0.001:
                continue
        if pid in pending_keys or pid in stale_ids:
            continue
        if is_manual_whitelisted(department=department, plan_id=pid, plan_name=name):
            continue
        out.append(p)
    return out


async def _adopt_manual_new_plans() -> dict:
    """08:00 档收养人工新建计划：归一 4.5/300 → 开启 → 投放日期改名。

    人工白天建的暂停计划夜间扫描看不到（零消耗已停不入「今天」视图），
    等 23:00 收口也等不到标签——晨检用「近7天」全量列表把它们找出来直接开。
    开启后进入「今天」视图，当日扫描/23:00 分类/归位等既有链路自动接管。
    """
    seen = _seen_plan_ids()
    queue = _load_old_pool_resume_queue()
    pending_keys = {
        str(i.get("plan_id") or i.get("plan_name") or "")
        for i in queue.get("pending") or []
    }
    stale_ids = _verified_stale_zero_order_ids()
    today = _today()
    summary: dict = {"candidates": 0, "adopted": [], "failed": []}
    _ensure_cdp()
    for department in DEPARTMENTS:
        try:
            plans = await list_all_plans(department, time_range="近7天")
        except Exception as exc:
            log.warning("[%s] 手动新建检测扫描失败: %s", department, str(exc)[:120])
            continue
        candidates = _fresh_manual_candidates(
            plans, department=department, seen_ids=seen,
            pending_keys=pending_keys, stale_ids=stale_ids,
        )
        if not candidates:
            continue
        summary["candidates"] += len(candidates)
        log.info("[%s] 发现 %d 条人工新建未接管计划: %s", department, len(candidates),
                 "、".join(str(p.plan_name or p.plan_id) for p in candidates[:8]))
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                for p in candidates:
                    item = {
                        "department": department,
                        "plan_id": str(p.plan_id or ""),
                        "plan_name": str(p.plan_name or ""),
                        "category": "new_plan",
                        "resume_date": today, "resume_time": "08:00",
                        "time_range": "last_7_days",
                    }
                    try:
                        try:
                            cur_roi = float(p.roi_target) if p.roi_target is not None else None
                        except (TypeError, ValueError):
                            cur_roi = None
                        try:
                            cur_budget = float(p.budget or 0)
                        except (TypeError, ValueError):
                            cur_budget = 0.0
                        if (cur_roi is None
                                or abs(cur_roi - NEW_PLAN_ROI_TARGET) >= 0.01
                                or abs(cur_budget - NEW_PLAN_DAILY_BUDGET) >= 1.0):
                            r = await _set_budget_absolute(
                                page, item, NEW_PLAN_DAILY_BUDGET,
                                roi=NEW_PLAN_ROI_TARGET)
                            log.info("[%s] 收养参数归一: %s 预算%s→%s ROI%s→%s",
                                     department, item["plan_name"],
                                     cur_budget, NEW_PLAN_DAILY_BUDGET,
                                     cur_roi, NEW_PLAN_ROI_TARGET)
                        result = await _enable_one(page, item)
                        if result not in ("enabled", "already"):
                            summary["failed"].append({**item, "detail": result})
                            log.error("[%s] 收养开启失败: %s (%s)",
                                      department, item["plan_name"], result)
                            continue
                        try:
                            name_change = await _stamp_8am_new_plan_delivery_date(page, item)
                            if name_change["changed"]:
                                item["plan_name"] = name_change["after"]
                        except Exception as exc:
                            log.warning("[%s] 收养后改名失败(已开启): %s (%s)",
                                        department, item["plan_name"], str(exc)[:120])
                        summary["adopted"].append(item)
                        log.info("[%s] 收养人工新建计划并开启: %s", department,
                                 item["plan_name"])
                    except Exception as exc:
                        summary["failed"].append({**item, "detail": str(exc)[:150]})
                        log.error("[%s] 收养异常: %s (%s)", department,
                                  item["plan_name"], str(exc)[:150])
        except Exception as exc:
            log.error("[%s] 收养部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend(
                {"department": department, "plan_id": str(p.plan_id or ""),
                 "plan_name": str(p.plan_name or ""), "detail": str(exc)[:200]}
                for p in candidates
            )
    return summary


async def resume_old_pool(dry_run: bool = False) -> dict:
    """到点恢复前一晚实际暂停的非重点计划：新/潜力 08:00，老池 12:00（2026-08-23 起分时）。

    08:00 档额外收养人工新建计划（2026-09-03 用户定：矩阵素材/建计划人工做，
    系统接管生命周期）——仅 09:00 前执行，见 _adopt_manual_slot/_adopt_manual_new_plans。
    同档还跑重点 0-8 盘中闸（白板③：0-8 ROI<6 降潜力 7.3/300 继续投放）。
    """
    queue = _recover_old_pool_resume_queue()
    today = _today()
    now_hhmm = datetime.datetime.now().strftime("%H:%M")
    ignored_keys = _ignored_resume_keys(_load_old_pool_resume_queue())
    due = [
        item for item in queue
        if _resume_key(item) not in ignored_keys
        and item.get("department") != "业务1部-1"
        and item.get("resume_date", today) <= today
        and (item.get("resume_time") or "12:00") <= now_hhmm
    ]
    stale_zero_order_ids = _verified_stale_zero_order_ids()
    # 佣金闸阻断名单（2026-09-07）：>上限被闸停的计划不再自动恢复，
    # 防「恢复→闸停→次日再恢复」每日空转；解禁=人工 --unblock-plan-id 后手动开。
    commission_blocked_ids = load_commission_blocked_ids()
    pending_by_department: dict[str, list[dict]] = {}
    for item in due:
        pid = str(item.get("plan_id") or "")
        if pid in stale_zero_order_ids or pid in commission_blocked_ids:
            continue
        pending_by_department.setdefault(item["department"], []).append({
            **item,
            # 已暂停计划当天可能无消耗，不能用“今天”筛选，否则会找不到。
            "time_range": None,
        })

    targets = [item for items in pending_by_department.values() for item in items]
    summary = {
        "action": "resume-old-pool",
        "dry_run": dry_run,
        "targets": targets,
        "enabled": [],
        "already": [],
        "renamed": [],
        "rename_failed": [],
        "skipped_stale_zero_order": [],
        "skipped_commission_blocked": [],
        "failed": [],
        "manual_adopt": None,
        "key_gate_08": None,
    }

    async def _adopt_manual_slot() -> None:
        """08:00 档顺带动作（与恢复票处理互独立，三条出口都要调）。

        ① 收养人工新建计划；② 重点 0-8 盘中闸（白板③，2026-09-03 用户定全建）。
        只在 09:00 前跑：再晚开启的计划到 12:00 新计划闸不足 4 小时数据，
        会被误判降日常——错过晨档就等次日 08:00。
        """
        if dry_run or now_hhmm >= "09:00":
            return
        try:
            adopt = await _adopt_manual_new_plans()
        except Exception as exc:
            log.error("人工新建收养整体异常: %s", str(exc)[:200])
            summary["manual_adopt"] = {"error": str(exc)[:200]}
            adopt = None
        if adopt is not None:
            summary["manual_adopt"] = adopt
            if adopt.get("adopted"):
                state = load_state()
                _merge_entries(state["old_pool_enabled"], adopt["adopted"])
                save_state(state)
        try:
            summary["key_gate_08"] = await demote_key_low_roi(6.0, scan_slot="08:00")
        except Exception as exc:
            log.error("08:00 重点降级闸异常: %s", str(exc)[:200])
            summary["key_gate_08"] = {"error": str(exc)[:200]}

    if dry_run or not due:
        # 票空不代表没事做：08:00 档还要收养人工新建计划（与票处理独立）
        await _adopt_manual_slot()
        return summary

    completed_keys: set[str] = set()
    for item in due:
        key = item.get("plan_id") or item.get("plan_name")
        pid = str(item.get("plan_id") or "")
        if pid in stale_zero_order_ids:
            summary["skipped_stale_zero_order"].append(item)
            if key:
                completed_keys.add(key)
        elif pid in commission_blocked_ids:
            # 阻断条目按已处理清出队列（不再恢复，不占票位）
            summary["skipped_commission_blocked"].append(item)
            if key:
                completed_keys.add(key)

    if not targets:
        queue_state = _load_old_pool_resume_queue()
        queue_state["pending"] = [
            item for item in queue_state["pending"]
            if (item.get("plan_id") or item.get("plan_name")) not in completed_keys
        ]
        _save_old_pool_resume_queue(queue_state)
        await _adopt_manual_slot()
        return summary

    _ensure_cdp()
    for department, items in pending_by_department.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                # 批量优先（2026-09-01 用户拍板「批量开启/暂停 + 事后逐条查验」）：
                # 全表扫一次、按页勾选目标、一次批量开启，行状态复核由
                # _batch_power_targets 内置（逐条等状态翻转，60s 兜底）。
                # 批量未成功的条目回退 _enable_one 单条重试，双保险不丢计划；
                # 逐条慢路径只在批量整体异常时使用。
                singles: list[tuple[dict, str]] = []  # (item, "enabled"/"already")
                fallback_items: list[dict] = []
                try:
                    batch = await _batch_enable_targets(page, department, items)
                except Exception as exc:
                    log.warning("[%s] 批量开启整体异常，回退逐条模式: %s",
                                department, str(exc)[:120])
                    batch = None
                if batch is not None:
                    ok_keys: set[str] = set()
                    for it in batch.get("enabled", []):
                        singles.append((it, "enabled"))
                        ok_keys.add(it.get("plan_id") or it.get("plan_name"))
                    for it in batch.get("already", []):
                        singles.append((it, "already"))
                        ok_keys.add(it.get("plan_id") or it.get("plan_name"))
                    batch_failed = {
                        (it.get("plan_id") or it.get("plan_name")): it.get("detail")
                        for it in batch.get("failed", [])
                    }
                    fallback_items = [
                        item for item in items
                        if (item.get("plan_id") or item.get("plan_name")) not in ok_keys
                    ]
                    if fallback_items:
                        log.info("[%s] 批量后剩 %d 条转单条重试: %s", department,
                                 len(fallback_items),
                                 "、".join(str(i.get("plan_name") or i.get("plan_id"))
                                           for i in fallback_items[:5]))
                else:
                    batch_failed = {}
                    fallback_items = list(items)
                for item in fallback_items:
                    key = item.get("plan_id") or item.get("plan_name")
                    batch_detail = batch_failed.get(key)
                    try:
                        result = await _enable_one(page, item)
                    except Exception as exc:
                        # 搜索框偶发失联（页面自动刷新等）：刷新页面重试一次，
                        # 不让单条异常中断整个部门（08-21 实测 19 条被连坐）。
                        log.warning("[%s] 恢复异常，刷新页面重试: %s (%s)",
                                    department, item.get("plan_name"), str(exc)[:120])
                        try:
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(5000)
                            result = await _enable_one(page, item)
                        except Exception as exc2:
                            result = f"error:{str(exc2)[:150]}"
                    if result in ("enabled", "already"):
                        singles.append((item, result))
                    else:
                        detail = f"batch={batch_detail}; retry={result}" if batch_detail else result
                        summary["failed"].append({**item, "detail": detail})
                        log.error("[%s] 恢复失败: %s (%s)",
                                  department, item.get("plan_name"), detail)
                # 08:00 新计划先归一 4.5/300（编辑页本质逐条）：票上标了 new_plan
                # 但平台实际参数可能仍是旧档（如人工加素材后按新计划重启的肥牛片，
                # 票据层改了 new_plan/4.5，平台还是 10.3）。不归一的话开启后
                # _lifecycle_entry 按 at_base_4_5 判不出新计划，盘中闸/归位全漏。
                for item, result in singles:
                    if (item.get("resume_time") or "12:00") != "08:00":
                        continue
                    if item.get("category") != "new_plan":
                        continue
                    # 2026-09-07 用户指认：白名单计划被 08:00 归一改了参数
                    # （味蕾食光 ROI→4.5/预算300）。白名单一律不碰。
                    if is_manual_whitelisted(
                        department=department,
                        plan_id=str(item.get("plan_id") or ""),
                        plan_name=str(item.get("plan_name") or ""),
                    ):
                        continue
                    # 归一目标 ROI 按票据值（旧素材通道 6.5 等，2026-10-09 用户规则），
                    # 票据没写才回退新计划标准 4.5——不能一律打回 4.5。
                    # 2026-10-09 二次教训（咔蛋亚裆）：票据无 roi_target（None）的老票，
                    # 平台 ROI 可能已被人工调过（9.5），归一写默认 4.5 会踩人工调整。
                    # 票据没带 ROI 意图 → 只归预算，ROI 不动。
                    roi_explicit = item.get("roi_target") is not None
                    try:
                        target_roi = (float(item["roi_target"])
                                      if roi_explicit
                                      else NEW_PLAN_ROI_TARGET)
                    except (TypeError, ValueError):
                        target_roi = NEW_PLAN_ROI_TARGET
                    try:
                        cur_roi = float(item.get("current_roi_target")
                                        if item.get("current_roi_target") is not None
                                        else item.get("roi_target"))
                    except (TypeError, ValueError):
                        cur_roi = None
                    try:
                        cur_budget = float(item.get("daily_budget") or item.get("budget") or 0)
                    except (TypeError, ValueError):
                        cur_budget = 0.0
                    if roi_explicit:
                        if (cur_roi is not None
                                and abs(cur_roi - target_roi) < 0.01
                                and abs(cur_budget - NEW_PLAN_DAILY_BUDGET) < 1.0):
                            continue
                    else:
                        # 票据无 ROI 意图：只按预算判断是否需要归一，ROI 保持平台现值
                        if abs(cur_budget - NEW_PLAN_DAILY_BUDGET) < 1.0:
                            continue
                    try:
                        r = await _set_budget_absolute(
                            page, item, NEW_PLAN_DAILY_BUDGET,
                            roi=target_roi if roi_explicit else None)
                        log.info("[%s] 08:00 新计划参数归一: %s 预算%s→%s ROI%s→%s",
                                 department, item.get("plan_name"),
                                 cur_budget, NEW_PLAN_DAILY_BUDGET,
                                 cur_roi, target_roi if roi_explicit else "不动(票据无意图)")
                    except Exception as exc:
                        # 归一失败不阻断已开启状态，记 rename_failed 让票留存待重试
                        summary["rename_failed"].append({**item, "detail": f"参数归一:{str(exc)[:120]}"})
                        log.error("[%s] 新计划已开启但参数归一失败: %s (%s)",
                                  department, item.get("plan_name"), str(exc)[:150])
                # 改名仍逐条（编辑页本质逐条）：只对已开启/已在投的做；
                # 非 08:00 新计划在 _needs_8am_delivery_date_stamp 处即返回，零开销。
                for item, result in singles:
                    key = item.get("plan_id") or item.get("plan_name")
                    resolved_item = item
                    # 2026-09-07：白名单计划不做投放日期改名（味蕾食光 0817→0907 误改）。
                    if is_manual_whitelisted(
                        department=department,
                        plan_id=str(item.get("plan_id") or ""),
                        plan_name=str(item.get("plan_name") or ""),
                    ):
                        continue
                    try:
                        name_change = await _stamp_8am_new_plan_delivery_date(page, item)
                        if name_change["changed"]:
                            resolved_item = {**item, "plan_name": name_change["after"]}
                            summary["renamed"].append({
                                "plan_id": item.get("plan_id") or "",
                                "department": department,
                                "before": name_change["before"],
                                "after": name_change["after"],
                            })
                            log.info("[%s] 08:00 新计划投放日期改名: %s → %s",
                                     department, name_change["before"], name_change["after"])
                    except Exception as exc:
                        # The plan has been enabled, but retain the queue entry so a later
                        # resume run can retry the required date stamp safely.
                        summary["rename_failed"].append({
                            **item, "detail": str(exc)[:150],
                        })
                        log.error("[%s] 新计划已恢复但投放日期改名失败: %s (%s)",
                                  department, item.get("plan_name"), str(exc)[:150])
                        continue
                    if result == "enabled":
                        summary["enabled"].append(resolved_item)
                        log.info("[%s] 恢复%s: %s", department,
                                 item.get("category") or "old_pool", resolved_item.get("plan_name"))
                    else:
                        summary["already"].append(resolved_item)
                    if key:
                        completed_keys.add(key)
        except Exception as exc:
            log.error("[%s] 恢复部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend(
                {**item, "detail": str(exc)[:200]} for item in items
                if (item.get("plan_id") or item.get("plan_name")) not in completed_keys
            )

    queue_state = _load_old_pool_resume_queue()
    # 白名单=人工管启停和 ROI（2026-10-09 用户口径）：恢复票永远不会被系统执行，
    # 滞留只会在解禁瞬间按化石口径爆发（咔蛋亚裆 9.5→4.5 事故）。
    # 豁免票当场出队；移出白名单后的接管由当晚 23:00 收口按平台现状重新分类。
    whitelist_drop = {
        (item.get("plan_id") or item.get("plan_name"))
        for item in summary["failed"]
        if "manual_whitelist" in str(item.get("detail") or "")
    }
    if whitelist_drop:
        log.info("白名单豁免票出队 %d 条（人工管启停，不再滞留）: %s",
                 len(whitelist_drop), "、".join(sorted(whitelist_drop)))
    queue_state["pending"] = [
        item for item in queue_state["pending"]
        if (item.get("plan_id") or item.get("plan_name")) not in completed_keys
        and (item.get("plan_id") or item.get("plan_name")) not in whitelist_drop
    ]
    _save_old_pool_resume_queue(queue_state)

    await _adopt_manual_slot()

    state = load_state()
    _merge_entries(state["old_pool_enabled"], summary["enabled"] + summary["already"])
    save_state(state)
    adopt = summary.get("manual_adopt") or {}
    log.info(
        "恢复动作完成：目标%d，已开启%d，已在投%d，跳过无效%d，失败%d；收养人工新建%d/%d；剩余队列%d",
        len(summary["targets"]), len(summary["enabled"]), len(summary["already"]),
        len(summary["skipped_stale_zero_order"]), len(summary["failed"]),
        len(adopt.get("adopted") or []), adopt.get("candidates") or 0,
        len(_load_old_pool_resume_queue().get("pending") or []),
    )
    return summary


# ---------------------------------------------------------------------------
# 17:10 老计划池日间停运（老池每日固定窗口 12:00-17:00，2026-08-21 起）
# ---------------------------------------------------------------------------

def _old_pool_pause_targets(report: dict) -> tuple[dict[str, list[dict]], list[dict]]:
    """从监控扫描中挑出 17:10 应暂停的老计划池：在投 + old_pool + 非当天新建。

    当天新建计划仍在测试期，留到 23:00 收口统一迁移参数并暂停；
    重点/潜力计划不受本动作影响，继续跑到 23:00。
    """
    by_dept: dict[str, list[dict]] = {}
    scan_errors: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error"):
                # 扫描失败的部门显式可见，不静默跳过（与 23:00 收口同规）。
                scan_errors.append({"department": department, "error": entry["error"]})
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            if entry.get("category") != "old_pool" or entry.get("is_today_new"):
                continue
            by_dept.setdefault(department, []).append({**entry, "department": department})
    return by_dept, scan_errors


async def pause_old_pool(dry_run: bool = False) -> dict:
    """17:10 暂停当天在投的老计划池，并把实际暂停的挂入次日 12:00 恢复队列。

    谁暂停谁挂恢复：17:10 ≥ 12 点，_default_resume_date 取次日，语义一致。
    只队列本动作实际暂停的计划——预算耗尽/人工暂停的老池计划不自动复活
    （与 23:00 收口只队列 paused_ids 的行为一致）。23:00 收口照常运行：
    老池此时已停，不参与批量暂停，但分类、写门、新计划迁移不受影响。
    """
    report = await monitor(dry_run=True)
    by_dept, scan_errors = _old_pool_pause_targets(report)
    summary = {
        "action": "pause-old-pool",
        "dry_run": dry_run,
        "scan_errors": scan_errors,
        "targets": [item for items in by_dept.values() for item in items],
        "paused": [],
        "already": [],
        "failed": [],
    }
    if dry_run or not by_dept:
        return summary

    _ensure_cdp()
    for department, items in by_dept.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                # 批量优先 + 事后逐条验收（2026-09-01 用户拍板，与 12:00 恢复同款）：
                # 全表按页勾选一次批量暂停；未翻状态的条目再逐条搜索复核暂停。
                # 纯逐条路径 35 条跑了 21 分钟（09-01 17:05→17:26）。
                try:
                    batch = await _batch_pause_targets(page, department, items)
                except Exception as exc:
                    log.warning("[%s] 批量暂停整体异常，回退逐条模式: %s",
                                department, str(exc)[:160])
                    batch = {"paused": [], "already": [],
                             "failed": [{**item, "detail": f"batch_error:{str(exc)[:120]}"}
                                        for item in items]}
                for item in batch["paused"]:
                    summary["paused"].append({**item, "detail": "ok", "pause_mode": "batch"})
                    log.info("[%s] 17:10 暂停老计划池: %s", department, item.get("plan_name"))
                summary["already"].extend(
                    {**item, "detail": "already", "pause_mode": "batch"}
                    for item in batch["already"])
                # 细节验收：批量后仍未暂停的逐条搜索复核
                failed_now = []
                for item in batch["failed"]:
                    try:
                        ok, detail = await pause_plan(
                            page, item.get("plan_name") or "",
                            item.get("plan_id"), department)
                    except Exception as exc:
                        log.warning("[%s] 17:10 验收暂停异常，刷新页面重试: %s (%s)",
                                    department, item.get("plan_name"), str(exc)[:120])
                        try:
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(5000)
                            ok, detail = await pause_plan(
                                page, item.get("plan_name") or "",
                                item.get("plan_id"), department)
                        except Exception as exc2:
                            ok, detail = False, f"error:{str(exc2)[:150]}"
                    record = {**item, "detail": detail, "pause_mode": "fallback"}
                    if ok and detail == "ok":
                        summary["paused"].append(record)
                        log.info("[%s] 17:10 验收补停: %s", department, item.get("plan_name"))
                    elif ok:
                        summary["already"].append(record)
                    else:
                        failed_now.append(record)
                        summary["failed"].append(record)
                        log.error("[%s] 17:10 暂停老计划池失败: %s (%s)",
                                  department, item.get("plan_name"), detail)
        except Exception as exc:
            log.error("[%s] 17:10 暂停老计划池部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)

    if summary["paused"]:
        _queue_old_pool_resume(summary["paused"])
    state = load_state()
    _merge_entries(state["old_pool_paused"], summary["paused"])
    save_state(state)
    return summary


# ---------------------------------------------------------------------------
# 当日新计划检查：12:00 停零消耗/ROI<4.5，并将达标的 4.5 档升到 7.3；
# 17:00 ROI<6 仍暂停新计划（包括已升 7.3 档），但这只是日内风控暂停：
# 保留 new_plan 标签和当前参数，23:00 才正式分类/转老池。12:00 不达标关闭才即时
# 改写老池参数 10.3/300 并挂次日 12:00 恢复。
# ---------------------------------------------------------------------------

def _new_plan_low_roi_targets(
    report: dict, threshold: float, *, close_zero_spend: bool = False
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Find active new-test plans that fail a checkpoint.

    At 12:00, zero spend is explicitly treated as a failed first-stage test and
    is demoted to daily-pool params (10.3/300, keeps running until 17:10).
    At the 17:00 checkpoint the existing ROI<6 rule remains (intraday pause).
    """
    by_dept: dict[str, list[dict]] = {}
    scan_errors: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error"):
                scan_errors.append({"department": department, "error": entry["error"]})
                continue
            if not entry.get("is_today_new"):
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            if entry.get("category") == "manual_review":
                continue
            spent = float(entry.get("spent") or 0)
            if spent <= 0:
                if close_zero_spend:
                    by_dept.setdefault(department, []).append({
                        **entry, "department": department,
                        "circuit_reason": "12:00 消耗为0，按不达标转日常(10.3/300 继续投放)",
                    })
                continue
            roi = entry.get("roi")
            if roi is not None and float(roi) >= threshold:
                continue
            reason = (
                f"ROI无数据，按ROI<{threshold:g}处理"
                if roi is None else f"ROI={float(roi):g}<{threshold:g}"
            )
            by_dept.setdefault(department, []).append({
                **entry, "department": department, "circuit_reason": reason,
            })
    return by_dept, scan_errors


def _new_plan_noon_promotion_targets(report: dict, *, scan_slot: str = "12:00") -> dict[str, list[dict]]:
    """Find 4.5-stage new plans that pass a daytime checkpoint.

    At 12:00, 14:00, or 16:00 a passing 4.5-stage new/material-test plan is
    advanced to ROI 7.3. The lifecycle label remains ``new_plan`` until 23:00.

    The plan remains a new plan; this only advances operating parameters to the
    7.3 stage. Requiring an actual current target of 4.5 makes the action
    idempotent and prevents a second scheduled/manual run from topping up again
    after a plan is already at 7.3.
    """
    by_dept: dict[str, list[dict]] = {}
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error") or not entry.get("is_today_new"):
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            if entry.get("category") == "manual_review":
                continue
            try:
                current_target = float(entry.get("current_roi_target"))
            except (TypeError, ValueError):
                continue
            if not is_new_plan_roi(current_target):
                continue
            spent = float(entry.get("spent") or 0)
            roi = entry.get("roi")
            if spent <= 0 or roi is None or float(roi) < NEW_PLAN_NOON_PASS_ROI:
                continue
            by_dept.setdefault(department, []).append({
                **entry, "department": department,
                "promotion_reason": f"{scan_slot} ROI={float(roi):g}达标，升ROI7.3",
            })
    return by_dept


def _top_up_budget_to_remaining(
    spent: float, current_budget: float,
    minimum_remaining: int = NEW_PLAN_MIN_REMAINING_BUDGET,
) -> dict:
    """Compute an increase that leaves at least ``minimum_remaining`` today.

    The budget is never reduced. With 180.2 already spent and a current cap of
    300, the target becomes ceil(180.2 + 300) = 481, leaving 300.8 available.
    """
    spent = max(float(spent or 0), 0.0)
    current_budget = max(float(current_budget or 0), 0.0)
    desired_budget = max(
        int(math.ceil(spent + minimum_remaining)), int(math.ceil(current_budget))
    )
    increase = max(desired_budget - current_budget, 0.0)
    return {
        "current_budget": current_budget,
        "spent": spent,
        "remaining_before": max(current_budget - spent, 0.0),
        "desired_budget": desired_budget,
        "increase": int(math.ceil(increase)),
        "remaining_after": desired_budget - spent,
    }


async def _promote_new_plan_noon(page, item: dict, *, scan_slot: str = "12:00") -> dict:
    """Apply a daytime 4.5 -> 7.3 parameter stage to one exact plan.

    The current cap is read from the edit page. We only raise it enough that
    today has 300 yuan remaining; no fixed +200/+300 increase is used.
    """
    from .budget_raise import _read_budget, _set_budget
    from .old_plan_pool import _set_roi, _wait_edit_inputs

    department = str(item.get("department") or "")
    plan_id = str(item.get("plan_id") or "")
    plan_name = str(item.get("plan_name") or "")
    if not department or not (plan_id or plan_name):
        raise RuntimeError(f"缺少部门或计划标识，无法执行{scan_slot}新计划升档")

    rows = await _search_plans(
        page, plan_id or plan_name, department, by_creator=False, time_range=None
    )
    target_row = None
    for row in rows:
        metrics = await _parse_plan_row(row)
        if metrics is None:  # 畸形行已留 WARNING，跳过
            continue
        if (plan_id and metrics.plan_id == plan_id) or (
            not plan_id and metrics.plan_name == plan_name
        ):
            target_row = row
            break
    if target_row is None:
        raise RuntimeError(f"{scan_slot}升档时未找到精确计划行")

    try:
        await _open_edit_page(page, target_row)
        await _wait_stable(page, 1.5)
        await _wait_edit_inputs(page)
        current_budget = await _read_budget(page)
        budget_plan = _top_up_budget_to_remaining(
            float(item.get("spent") or 0), current_budget
        )
        await _set_roi(page, NEW_PLAN_NOON_ADVANCED_ROI)
        if budget_plan["increase"] > 0:
            await _set_budget(page, int(budget_plan["desired_budget"]))
        if not await _save_edit_page(page):
            raise RuntimeError(f"{scan_slot}升档未找到保存按钮")
        return {
            "plan_id": plan_id,
            "department": department,
            "plan_name": plan_name,
            "status": item.get("status"),
            "roi": item.get("roi"),
            "orders": item.get("orders"),
            "roi_before": float(item.get("current_roi_target") or NEW_PLAN_ROI_TARGET),
            "roi_after": NEW_PLAN_NOON_ADVANCED_ROI,
            **budget_plan,
        }
    finally:
        try:
            await _open_overall(page, department)
        except Exception as exc:
            log.warning("[%s] %s升档后回到总览页失败: %s", department, scan_slot, str(exc)[:120])


async def afternoon_follow_up(scan_slot: str, dry_run: bool = False) -> dict:
    """Follow 14:00/16:00 promotion and budget-risk checkpoints.

    This action only promotes currently running 4.5-stage new/material-test
    plans that have passed ROI 4.5. It never performs the 17:00 ROI<6 pause or
    final lifecycle classification. Re-running the same slot is safe because
    only plans whose current target is still 4.5 are selected.
    """
    if scan_slot not in {"14:00", "16:00"}:
        raise ValueError("afternoon-follow-up 只支持 14:00 或 16:00")
    report = await monitor(dry_run=True, scan_slot=scan_slot, final_classification=False)
    promotion_by_dept = _new_plan_noon_promotion_targets(report, scan_slot=scan_slot)
    summary = {
        "action": "afternoon-follow-up", "scan_slot": scan_slot,
        "dry_run": dry_run,
        "promotion_targets": [item for items in promotion_by_dept.values() for item in items],
        "promoted": [], "promotion_failed": [],
    }
    if dry_run or not promotion_by_dept:
        return summary
    _ensure_cdp()
    for department, items in promotion_by_dept.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                for item in items:
                    try:
                        promoted = await _promote_new_plan_noon(page, item, scan_slot=scan_slot)
                        summary["promoted"].append(promoted)
                        log.info(
                            "[%s] %s新计划升档: %s ROI %.1f→%.1f，预算 %.0f→%d，剩余 %.1f→%.1f",
                            department, scan_slot, item.get("plan_name"), NEW_PLAN_ROI_TARGET,
                            NEW_PLAN_NOON_ADVANCED_ROI, promoted["current_budget"],
                            promoted["desired_budget"], promoted["remaining_before"],
                            promoted["remaining_after"],
                        )
                    except Exception as exc:
                        failed = {**item, "detail": str(exc)[:200]}
                        summary["promotion_failed"].append(failed)
                        log.error("[%s] %s新计划升档失败: %s (%s)",
                                  department, scan_slot, item.get("plan_name"), str(exc)[:200])
        except Exception as exc:
            summary["promotion_failed"].extend(
                {**item, "detail": str(exc)[:200]} for item in items
            )
    if summary["promoted"]:
        state = load_state()
        _merge_entries(state["new_plan_promotions"], summary["promoted"])
        save_state(state)
        try:
            from webui.db import record_plan_daily_labels
            labels: dict[str, list[dict]] = {}
            for item in summary["promoted"]:
                labels.setdefault(item["department"], []).append({
                    **item, "category": "new_plan",
                    "reason": f"{scan_slot} ROI4.5达标，升ROI7.3，剩余预算补足300元",
                    "roi_target": item["roi_after"],
                    "daily_budget": item["desired_budget"],
                })
            record_plan_daily_labels(labels, scan_slot=scan_slot)
        except Exception as exc:
            log.error("%s升档标签落库失败: %s", scan_slot, str(exc)[:200])
    return summary


async def pause_new_low_roi(threshold: float, dry_run: bool = False,
                            scan_slot: str | None = None) -> dict:
    """新计划检查点动作。

    12:00 首检（阈值 4.5）：不达标不暂停——即时改写 10.3/300 转日常计划，
    继续跟老池投放到 17:10 随池暂停（2026-09-01 用户口径，此前是暂停挂
    次日 12:00，参数改写还因「暂停默认跳过」落空）。
    17:00 复检（阈值 6）：日内暂停、保留新计划身份，23:00 收口再正式分类。
    """
    # 标签 slot：默认按阈值映射定时档位（4.5→12:00、6→17:00），可用 --scan-slot 覆盖
    slot = scan_slot or ("12:00" if threshold <= 4.5 else
                         "17:00" if threshold <= 6 else
                         _label_slot_now())
    is_noon_first_checkpoint = threshold <= NEW_PLAN_NOON_PASS_ROI and slot == "12:00"
    report = await monitor(dry_run=True, scan_slot=slot, final_classification=False)
    by_dept, scan_errors = _new_plan_low_roi_targets(
        report, threshold, close_zero_spend=is_noon_first_checkpoint
    )
    promotion_by_dept = (
        _new_plan_noon_promotion_targets(report, scan_slot=slot) if is_noon_first_checkpoint else {}
    )
    defer_category_to_2300 = threshold > NEW_PLAN_NOON_PASS_ROI
    # 每日标签：被熔断的新计划覆盖为日常计划标签（通过检查的保留 monitor 原始分类）。
    # 与 monitor 一样吞异常：落库失败不影响熔断动作；但必须记日志（2026-08-24
    # DB Browser 锁库导致 17:00 熔断标签全灭且毫无痕迹）。
    try:
        from webui.db import record_plan_daily_labels

        labels: dict[str, list[dict]] = {}
        for department, items in by_dept.items():
            labels[department] = []
            for item in items:
                if defer_category_to_2300:
                    labels[department].append({
                        **item,
                        "category": "new_plan",
                        "paused_at_17": True,
                        "reason": f"17:00 ROI<{threshold:g} 日内暂停，保持新计划，23:00再分类",
                        "roi_target": item.get("current_roi_target") or item.get("roi_target"),
                        "daily_budget": item.get("daily_budget"),
                    })
                else:
                    labels[department].append({
                        **item,
                        "category": "old_pool",
                        "reason": str(item.get("circuit_reason") or f"{slot} 熔断 ROI<{threshold:g} 降日常"),
                        "roi_target": OLD_POOL_ROI_TARGET,
                        "daily_budget": OLD_POOL_DAILY_BUDGET,
                    })
        record_plan_daily_labels(labels, scan_slot=slot)
    except Exception as exc:
        log.error("熔断标签落库失败: %s", str(exc)[:200])
    summary = {
        "action": "pause-new-low-roi",
        "threshold": threshold,
        "dry_run": dry_run,
        "scan_errors": scan_errors,
        "targets": [item for items in by_dept.values() for item in items],
        "promotion_targets": [item for items in promotion_by_dept.values() for item in items],
        "deferred_to_2300": [],
        "promoted": [],
        "promotion_failed": [],
        "paused": [],
        "already": [],
        "failed": [],
        "param_rewrite": None,
    }
    if dry_run:
        return summary

    if promotion_by_dept:
        _ensure_cdp()
        for department, items in promotion_by_dept.items():
            try:
                async with async_playwright() as pw:
                    browser = await connect_browser(pw, timeout=8000)
                    page = await _department_page(browser.contexts[0], department)
                    for item in items:
                        try:
                            promoted = await _promote_new_plan_noon(page, item, scan_slot=slot)
                            summary["promoted"].append(promoted)
                            log.info(
                                "[%s] 12:00新计划升档: %s ROI %.1f→%.1f，预算 %.0f→%d，剩余 %.1f→%.1f",
                                department, item.get("plan_name"), NEW_PLAN_ROI_TARGET,
                                NEW_PLAN_NOON_ADVANCED_ROI, promoted["current_budget"],
                                promoted["desired_budget"], promoted["remaining_before"],
                                promoted["remaining_after"],
                            )
                        except Exception as exc:
                            failed = {**item, "detail": str(exc)[:200]}
                            summary["promotion_failed"].append(failed)
                            log.error("[%s] 12:00新计划升档失败: %s (%s)",
                                      department, item.get("plan_name"), str(exc)[:200])
            except Exception as exc:
                summary["promotion_failed"].extend(
                    {**item, "detail": str(exc)[:200]} for item in items
                )

    if summary["promoted"]:
        state = load_state()
        _merge_entries(state["new_plan_promotions"], summary["promoted"])
        save_state(state)
        try:
            from webui.db import record_plan_daily_labels
            labels: dict[str, list[dict]] = {}
            for item in summary["promoted"]:
                labels.setdefault(item["department"], []).append({
                    **item,
                    "category": "new_plan",
                    "reason": f"{slot} ROI4.5达标，升ROI7.3，剩余预算补足300元",
                    "roi_target": item["roi_after"],
                    "daily_budget": item["desired_budget"],
                })
            record_plan_daily_labels(labels, scan_slot=slot)
        except Exception as exc:
            log.error("12:00升档标签落库失败: %s", str(exc)[:200])

    if not by_dept:
        return summary

    _save_pause_snapshot(report, f"pause-new-low-roi-{threshold}")

    if not defer_category_to_2300:
        # 2026-09-01 用户定：12:00 首检不达标不再暂停——即时改写老池参数
        # 10.3/300 转「日常计划」继续投放，跟老池一起跑到 17:10 随池暂停，
        # 23:00 收口照常终分类。改写后 roi_target=10.3 使日间扫描自动将其
        # 归 old_pool（不再是新计划测试），17:10 pause-old-pool 自然收编。
        # 旧行为（先暂停再改写）被 apply_old_pool 的暂停跳过规则架空：
        # 09-01 12:28 三条全部「跳过」，参数没落、下午投放窗口也白费。
        from .old_plan_pool import OLD_POOL_BUDGET, OLD_POOL_ROI, apply_old_pool

        rewrite_targets = [
            {"plan_name": p.get("plan_name"), "plan_id": p.get("plan_id"),
             "department": p["department"]}
            for items in by_dept.values() for p in items
        ]
        rewrite = await apply_old_pool(rewrite_targets, roi=OLD_POOL_ROI,
                                       budget=OLD_POOL_BUDGET)
        summary["param_rewrite"] = rewrite
        summary["demoted_keep_running"] = rewrite.get("changed") or []
        log.info("12:00 新计划不达标转日常(10.3/300 继续投放，17:10随老池暂停): "
                 "改写成功 %d，失败 %d，跳过 %d；失败明细: %s",
                 len(rewrite.get("changed") or []),
                 len(rewrite.get("failed") or []),
                 len(rewrite.get("skipped") or []),
                 json.dumps((rewrite.get("failed") or [])[:5], ensure_ascii=False))
        log.info(
            "熔断动作完成（转日常不暂停）：目标%d，升档%d，转日常改写%d，改写失败%d，升档失败%d",
            len(summary["targets"]), len(summary["promoted"]),
            len(rewrite.get("changed") or []), len(rewrite.get("failed") or []),
            len(summary["promotion_failed"]),
        )
        return summary

    _ensure_cdp()
    for department, items in by_dept.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                page = await _department_page(browser.contexts[0], department)
                batch = await _batch_pause_targets(page, department, items)
                summary["paused"].extend(
                    {**item, "detail": "ok", "pause_mode": "batch"}
                    for item in batch["paused"]
                )
                summary["already"].extend(
                    {**item, "detail": "already", "pause_mode": "batch"}
                    for item in batch["already"]
                )
                # 仅对批量复核失败的条目逐条回退，正常路径不再一条条操作。
                for item in batch["failed"]:
                    try:
                        ok, detail = await pause_plan(
                            page, item.get("plan_name") or "", item.get("plan_id"), department)
                    except Exception as exc:
                        ok, detail = False, f"error:{str(exc)[:150]}"
                    record = {**item, "detail": detail, "pause_mode": "fallback"}
                    (summary["paused"] if ok and detail == "ok" else
                     summary["already"] if ok else summary["failed"]).append(record)
        except Exception as exc:
            log.error("[%s] 熔断批量暂停部门级异常: %s", department, str(exc)[:200])
            summary["failed"].extend({**item, "detail": str(exc)[:200]} for item in items)

    if summary["paused"]:
        # 17:00 是日内暂停，并非生命周期降级：保留 ROI 7.3/当前预算，
        # 也不挂次日老池恢复队列；23:00 再按全天结果正式分类。
        deferred = [{
            **item,
            "category": "new_plan",
            "paused_at_17": True,
            "reason": f"17:00 ROI<{threshold:g} 日内暂停，23:00再分类",
        } for item in summary["paused"]]
        summary["deferred_to_2300"] = deferred
        state = load_state()
        _merge_entries(state["new_plan_paused_at_17"], deferred)
        save_state(state)
        log.info("17:00 新计划日内暂停 %d 条，未转老池，等待23:00分类", len(deferred))
    log.info(
        "熔断动作完成：目标%d，升档%d，暂停%d，已在暂停%d，失败%d，升档失败%d",
        len(summary["targets"]), len(summary["promoted"]), len(summary["paused"]),
        len(summary["already"]), len(summary["failed"]),
        len(summary["promotion_failed"]),
    )
    return summary


async def pause_potential_low_roi(threshold: float, dry_run: bool = False,
                                  scan_slot: str | None = None) -> dict:
    """潜力计划盘中降级闸。

    不达标 = 今日订单 < POTENTIAL_MIN_ORDERS(4)，或 ROI 低于阈值
    （有消耗但 ROI 无数据按低于阈值计）。
    12:00 档：破线改写为日常计划（ROI 10.3、日预算 300）并继续投放，17:00
    老池停收编；不暂停、不进入次日恢复队列。
    17:00 档（2026-09-03 用户定「先暂停老计划，别浪费钱」）：破线改写 10.3/300
    后立即暂停止血，并挂次日 12:00 恢复票——收口链已把老池停提到最前，
    降级计划不再「继续投放」烧钱。重点计划与人工白名单不在本闸处理范围内。

    2026-09-03 追加：达标但参数偏离的潜力计划同场对齐 7.3/300（param_align），
    播报标签与平台参数盘中即一致，不再等 23:00 收口。
    """
    slot = scan_slot or ("12:00" if threshold <= 6.0 else "17:00")
    is_noon = threshold <= 6.0 and slot == "12:00"
    report = await monitor(dry_run=True, scan_slot=slot, final_classification=False)
    by_dept: dict[str, list[dict]] = {}
    scan_errors: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error"):
                scan_errors.append({"department": department, "error": entry["error"]})
                continue
            if entry.get("category") != "potential_plan":
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            orders = int(entry.get("orders") or 0)
            spent = float(entry.get("spent") or 0)
            roi = entry.get("roi")
            reasons: list[str] = []
            if orders < POTENTIAL_MIN_ORDERS:
                reasons.append(f"订单{orders}<{POTENTIAL_MIN_ORDERS}")
            if roi is None:
                if spent > 0:
                    reasons.append("ROI无数据")
            elif float(roi) < threshold:
                reasons.append(f"ROI={float(roi):g}<{threshold:g}")
            if not reasons:
                continue
            by_dept.setdefault(department, []).append({
                **entry, "department": department,
                "circuit_reason": "、".join(reasons),
            })
    summary = {
        "action": "pause-potential-low-roi",
        "threshold": threshold,
        "scan_slot": slot,
        "dry_run": dry_run,
        "scan_errors": scan_errors,
        "targets": [item for items in by_dept.values() for item in items],
        "paused": [],
        "already": [],
        "failed": [],
        "param_rewrite": None,
        "demoted_keep_running": [],
        "demoted_paused": [],
        "param_align": [],
        "param_aligned": [],
    }
    demote_ids = {
        str(item.get("plan_id") or "")
        for items in by_dept.values() for item in items
    }
    align_targets = _potential_param_align_targets(report, exclude_ids=demote_ids)
    summary["param_align"] = align_targets
    if dry_run or (not by_dept and not align_targets):
        return summary

    # 先真实改写平台参数；只有改写成功后才同步 old_pool 标签，避免数据库
    # 先变更、平台失败时把仍是潜力计划的计划误写成日常计划。
    from .old_plan_pool import OLD_POOL_BUDGET, OLD_POOL_ROI, apply_old_pool

    if by_dept:
        rewrite_targets = [
            {"plan_name": item.get("plan_name"), "plan_id": item.get("plan_id"),
             "department": item["department"], "status": item.get("status")}
            for items in by_dept.values() for item in items
        ]
        rewrite = await apply_old_pool(
            rewrite_targets, roi=OLD_POOL_ROI, budget=OLD_POOL_BUDGET)
        summary["param_rewrite"] = rewrite
        summary["demoted_keep_running"] = rewrite.get("changed") or []
        summary["failed"].extend(rewrite.get("failed") or [])

        changed_ids = {str(plan_id) for plan_id in (rewrite.get("changed") or []) if plan_id}
        if changed_ids:
            try:
                from webui.db import record_plan_daily_labels

                labels: dict[str, list[dict]] = {}
                for department, items in by_dept.items():
                    for item in items:
                        if str(item.get("plan_id") or "") not in changed_ids:
                            continue
                        labels.setdefault(department, []).append({
                            **item,
                            "status": "已暂停" if slot == "17:00" else "投放中",
                            "category": "old_pool",
                            "reason": (
                                f"{slot} 潜力 ROI/订单破线：{item.get('circuit_reason')}，"
                                + ("降为日常计划并暂停止血，挂次日12:00恢复"
                                   if slot == "17:00"
                                   else "降为日常计划并继续投放")
                            ),
                            "roi_target": OLD_POOL_ROI_TARGET,
                            "daily_budget": OLD_POOL_DAILY_BUDGET,
                        })
                if labels:
                    record_plan_daily_labels(labels, scan_slot=slot)
            except Exception as exc:
                log.error("潜力降级标签落库失败: %s", str(exc)[:200])

        # 17:00 档降级即停（2026-09-03 用户定「先暂停老计划，别浪费钱」）：
        # 改写 10.3/300 成功后立即暂停并挂次日 12:00 恢复票；12:00 档维持
        # 转日常继续投放（当日下午窗口未过，老池停会收编）。
        demoted_items = [
            item for items in by_dept.values() for item in items
            if str(item.get("plan_id") or "") in changed_ids
        ]
        if slot == "17:00" and demoted_items:
            _ensure_cdp()
            for department, items in by_dept.items():
                dept_items = [i for i in items
                              if str(i.get("plan_id") or "") in changed_ids]
                if not dept_items:
                    continue
                try:
                    async with async_playwright() as pw:
                        browser = await connect_browser(pw, timeout=8000)
                        page = await _department_page(browser.contexts[0], department)
                        batch = await _batch_pause_targets(page, department, dept_items)
                        summary["demoted_paused"].extend(
                            batch["paused"] + batch["already"])
                        for item in batch["failed"]:
                            try:
                                ok, detail = await pause_plan(
                                    page, item.get("plan_name") or "",
                                    item.get("plan_id"), department)
                            except Exception as exc:
                                ok, detail = False, f"error:{str(exc)[:150]}"
                            if ok:
                                summary["demoted_paused"].append(item)
                            else:
                                summary["failed"].append(
                                    {**item, "detail": f"降级后暂停失败:{detail}"})
                except Exception as exc:
                    log.error("[%s] 17:00 降级后暂停异常: %s",
                              department, str(exc)[:200])
                    summary["failed"].extend(
                        {**i, "detail": str(exc)[:200]} for i in dept_items)
            if summary["demoted_paused"]:
                _queue_old_pool_resume([
                    {**item, "category": "old_pool", "status": "已暂停",
                     "reason": f"{slot} 潜力破线降日常并暂停止血"}
                    for item in summary["demoted_paused"]
                ])
                log.info("17:00 潜力降级即停 %d 条，挂次日12:00恢复",
                         len(summary["demoted_paused"]))

        log.info(
            "%s 潜力降级转日常(10.3/300)：目标%d，改写成功%d，失败%d，跳过%d%s",
            slot, len(summary["targets"]), len(rewrite.get("changed") or []),
            len(rewrite.get("failed") or []), len(rewrite.get("skipped") or []),
            ("；已暂停%d条并挂次日12:00恢复" % len(summary["demoted_paused"]))
            if slot == "17:00" else "；继续投放，不执行暂停或恢复入队",
        )

    # 2026-09-03 用户定：播报标了潜力的计划，参数盘中立即对齐 7.3/300。
    if align_targets:
        align = await apply_old_pool(
            [{"plan_name": item.get("plan_name"), "plan_id": item.get("plan_id"),
              "department": item["department"], "status": item.get("status")}
             for item in align_targets],
            roi=POTENTIAL_PLAN_ROI_TARGET, budget=POTENTIAL_PLAN_DAILY_BUDGET)
        summary["param_aligned"] = align.get("changed") or []
        summary["failed"].extend(align.get("failed") or [])
        log.info("%s 潜力参数对齐(7.3/300)：目标%d，改写成功%d，失败%d",
                 slot, len(align_targets), len(align.get("changed") or []),
                 len(align.get("failed") or []))
    return summary


def _potential_param_align_targets(
    report: dict, *, exclude_ids: set[str]
) -> list[dict]:
    """达标潜力计划中参数偏离 7.3/300 的名单（纯函数）。

    2026-09-03 用户定：播报标了「潜力计划」的，参数盘中就要对齐（此前只 23:00
    收口改写，用户看到 10.3/300 的「潜力计划」认为系统没操作）。破线降日常的
    （exclude_ids）不在此列；ROI/预算读不出的分项不动（避免误伤）。
    """
    out: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error") or entry.get("category") != "potential_plan":
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            pid = str(entry.get("plan_id") or "")
            if not pid or pid in exclude_ids:
                continue
            try:
                cur_roi = float(entry.get("current_roi_target"))
            except (TypeError, ValueError):
                cur_roi = None
            try:
                cur_budget = float(entry.get("budget") or 0)
            except (TypeError, ValueError):
                cur_budget = 0.0
            roi_drift = (cur_roi is not None
                         and abs(cur_roi - POTENTIAL_PLAN_ROI_TARGET) >= 0.01)
            budget_drift = (cur_budget > 0
                            and abs(cur_budget - POTENTIAL_PLAN_DAILY_BUDGET) >= 1.0)
            if not roi_drift and not budget_drift:
                continue
            out.append({
                **entry, "department": department,
                "align_reason": (
                    f"ROI目标{cur_roi}→{POTENTIAL_PLAN_ROI_TARGET:g}" if roi_drift else ""
                ) + (
                    f" 预算{cur_budget:g}→{POTENTIAL_PLAN_DAILY_BUDGET:g}" if budget_drift else ""
                ),
            })
    return out


# ---------------------------------------------------------------------------
# 重点计划盘中降级闸（2026-09-03 用户定，白板③：0-8<6 / 0-12<7.3 / 0-17<8.5）
# ---------------------------------------------------------------------------

def _key_low_roi_targets(
    report: dict, threshold: float
) -> tuple[dict[str, list[dict]], list[dict]]:
    """重点计划盘中破线名单（纯函数）：在投 + key_plan + 今日累计 ROI < 阈值。

    有消耗但 ROI 无数据按破线计（与潜力闸同口径）；零消耗无数据不动（无证据）。
    人工白名单经 monitor 分类为 manual_review，天然不在 key_plan 内。
    """
    by_dept: dict[str, list[dict]] = {}
    scan_errors: list[dict] = []
    for department, entries in (report.get("plans") or {}).items():
        for entry in entries:
            if entry.get("error"):
                scan_errors.append({"department": department, "error": entry["error"]})
                continue
            if entry.get("category") != "key_plan":
                continue
            if "投放中" not in (entry.get("status") or ""):
                continue
            spent = float(entry.get("spent") or 0)
            roi = entry.get("roi")
            if roi is None:
                if spent <= 0:
                    continue
                reason = "ROI无数据"
            elif float(roi) < threshold:
                reason = f"ROI={float(roi):g}<{threshold:g}"
            else:
                continue
            by_dept.setdefault(department, []).append({
                **entry, "department": department, "circuit_reason": reason,
            })
    return by_dept, scan_errors


async def demote_key_low_roi(threshold: float, dry_run: bool = False,
                             scan_slot: str | None = None) -> dict:
    """重点计划盘中降级闸：破线 → 降潜力（ROI 7.3、日预算 300）并继续投放。

    不暂停、不入恢复队列；23:00 按全天数据终分类，数据够格会重新升回重点。
    标签同槽落库为 potential_plan（UPSERT 覆盖同槽旧标签 + scanned_at 最新优先），
    盘中保持机制随之转向潜力，不会再把它播回重点。
    """
    slot = scan_slot or _label_slot_now()
    report = await monitor(dry_run=True, scan_slot=slot, final_classification=False)
    by_dept, scan_errors = _key_low_roi_targets(report, threshold)
    summary = {
        "action": "demote-key-low-roi",
        "threshold": threshold,
        "scan_slot": slot,
        "dry_run": dry_run,
        "scan_errors": scan_errors,
        "targets": [item for items in by_dept.values() for item in items],
        "demoted": [],
        "failed": [],
    }
    if dry_run or not by_dept:
        return summary

    from .old_plan_pool import apply_old_pool

    rewrite = await apply_old_pool(
        [{"plan_name": item.get("plan_name"), "plan_id": item.get("plan_id"),
          "department": item["department"], "status": item.get("status")}
         for items in by_dept.values() for item in items],
        roi=POTENTIAL_PLAN_ROI_TARGET, budget=POTENTIAL_PLAN_DAILY_BUDGET)
    summary["demoted"] = rewrite.get("changed") or []
    summary["failed"] = rewrite.get("failed") or []

    changed_ids = {str(plan_id) for plan_id in summary["demoted"] if plan_id}
    if changed_ids:
        try:
            from webui.db import record_plan_daily_labels

            labels: dict[str, list[dict]] = {}
            for department, items in by_dept.items():
                for item in items:
                    if str(item.get("plan_id") or "") not in changed_ids:
                        continue
                    labels.setdefault(department, []).append({
                        **item,
                        "status": "投放中",
                        "category": "potential_plan",
                        "reason": (
                            f"{slot} 重点盘中破线：{item.get('circuit_reason')}，"
                            "降为潜力计划(7.3/300)继续投放，23:00按全天数据终分类"
                        ),
                        "roi_target": POTENTIAL_PLAN_ROI_TARGET,
                        "daily_budget": POTENTIAL_PLAN_DAILY_BUDGET,
                    })
            if labels:
                record_plan_daily_labels(labels, scan_slot=slot)
        except Exception as exc:
            log.error("重点降级标签落库失败: %s", str(exc)[:200])

    log.info("%s 重点降级转潜力(7.3/300 继续投放)：目标%d，改写成功%d，失败%d",
             slot, len(summary["targets"]), len(summary["demoted"]),
             len(summary["failed"]))
    return summary

async def pause_one(plan_name: str, plan_id: str | None, department: str,
                    dry_run: bool = False, resume_date: str | None = None,
                    roi: float | None = None, label: str = "") -> dict:
    """暂停单条刚发布的新计划并挂 08:00 恢复（2026-08-23 策略：夜间建好但暂停到 8:00）。

    由轮询在建计划成功后立刻调用（--from-poll）：此时调用方就是轮询轮次本身，
    main() 侧跳过等轮询空闲与忙标，避免自等。
    roi：该计划的目标 ROI（如旧素材通道 6.5，2026-10-09 用户规则）；缺省按
    新计划标准 4.5。写入恢复队列与每日标签，08:00 归一按票据值保持，不打回 4.5。
    label：通道标签（如「旧素材补投」），随队列条目与看板落库，便于追踪。
    """
    roi_target = float(roi) if roi else NEW_PLAN_ROI_TARGET
    summary = {"action": "pause-one", "plan_name": plan_name, "plan_id": plan_id,
               "department": department, "dry_run": dry_run,
               "result": None, "queued": False}
    if dry_run:
        summary["result"] = "dry_run"
        return summary
    _ensure_cdp()
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        # 2026-10-06 实证：复用存量总览页会拿到假死旧标签（DOM 不渲染搜索框，
        # pause-one 连续 RuntimeError: Search input not found，建成的新计划
        # 烧预算没人停——rlmLqE 小洁食光记靠手工开新页才救回）。改为自己开
        # 全新页、用完即关；新开的页不是第一页，关闭安全。
        page = await open_fresh_page(browser.contexts[0])
        try:
            page = await ensure_qianchuan_page(page, department, timeout=20.0)
            ok, detail = await pause_plan(page, plan_name, plan_id, department)
        finally:
            try:
                await page.close()
            except Exception:
                pass
    summary["result"] = detail
    if ok and detail == "ok":
        _queue_old_pool_resume([{
            "plan_name": plan_name, "plan_id": plan_id, "department": department,
            "category": "new_plan", "status": "已暂停", "roi_target": roi_target,
            **({"label": label} if label else {}),
        }], resume_date=resume_date)
        summary["queued"] = True
        log.info("[%s] 新计划建成即暂停，挂 08:00 恢复: %s", department, plan_name)
    elif ok:
        # already：已是暂停态，视为成功（可能刚被收口停掉）
        summary["queued"] = True
        _queue_old_pool_resume([{
            "plan_name": plan_name, "plan_id": plan_id, "department": department,
            "category": "new_plan", "status": "已暂停", "roi_target": roi_target,
            **({"label": label} if label else {}),
        }], resume_date=resume_date)
        log.info("[%s] 新计划已是暂停态，直接挂 08:00 恢复: %s", department, plan_name)
    if summary["queued"]:
        # 夜间新建计划落每日标签：写到「恢复执行日」的看板 08:00 列（2026-08-24）。
        # 不经过 monitor 扫描的新计划也能在次日看板第一时间可见。
        try:
            from webui.db import record_plan_daily_labels

            record_plan_daily_labels(
                {department: [{
                    "department": department, "plan_id": plan_id or "",
                    "plan_name": plan_name, "status": "已暂停",
                    "category": "new_plan",
                    "reason": (f"{label}：夜间新建即暂停，挂 08:00 恢复" if label
                               else "夜间新建即暂停，挂 08:00 恢复"),
                    "roi_target": roi_target, "daily_budget": 300,
                }]},
                scan_slot="08:00",
                label_date=_default_resume_date(),
            )
        except Exception as exc:
            log.error("夜间新建标签落库失败: %s", str(exc)[:200])
    return summary


# ---------------------------------------------------------------------------
# 08:00 批量开启
# ---------------------------------------------------------------------------

def _needs_8am_delivery_date_stamp(item: dict) -> bool:
    """Only night-created new plans get relabeled when their 08:00 run starts."""
    return (
        item.get("category") == "new_plan"
        and (item.get("resume_time") or _resume_time_for(item)) == "08:00"
    )


async def _stamp_8am_new_plan_delivery_date(page, item: dict) -> dict:
    """Rename an enabled 08:00 new plan to its queued delivery date.

    The plan is searched by ID where possible, then its edit form is saved once
    after the name change.  The queue's resume_date is authoritative: retries
    later in the day must retain the original delivery date, not use a new one.
    """
    original = str(item.get("plan_name") or "")
    unchanged = {"changed": False, "before": original, "after": original}
    if not _needs_8am_delivery_date_stamp(item):
        return unchanged

    department = str(item.get("department") or "")
    plan_id = str(item.get("plan_id") or "")
    keyword = plan_id or original
    if not department or not keyword:
        raise RuntimeError("缺少部门、计划 ID 或计划名称，无法更新投放日期")

    rows = await _search_plans(page, keyword, department, by_creator=False, time_range=None)
    target_row = None
    for row in rows:
        metrics = await _parse_plan_row(row)
        if metrics is None:  # 畸形行已留 WARNING，跳过
            continue
        if (plan_id and metrics.plan_id == plan_id) or (not plan_id and metrics.plan_name == original):
            target_row = row
            break
    if target_row is None:
        raise RuntimeError("恢复后未找到计划，无法更新投放日期")

    try:
        await _open_edit_page(page, target_row)
        await _wait_stable(page, 1.5)
        try:
            delivery_date = datetime.date.fromisoformat(str(item.get("resume_date") or _today()))
        except ValueError:
            delivery_date = datetime.date.today()
        change = await update_plan_name_delivery_date(page, delivery_date)
        if change["changed"] and not await _save_edit_page(page):
            raise RuntimeError("计划名称已修改但未找到保存按钮")
        return change
    finally:
        # The following queue item must always start from the overview page.
        try:
            await _open_overall(page, department)
        except Exception as exc:
            log.warning("[%s] 改名后回到总览页失败: %s", department, str(exc)[:120])


async def _enable_one(page, target: dict) -> str:
    """开启单条计划。返回 enabled/already/not_found/failed:*。"""
    dept = target["department"]
    name = target.get("plan_name") or ""
    pid = target.get("plan_id")
    if is_manual_whitelisted(department=dept, plan_id=pid or "", plan_name=name) and not target.get("manual_override"):
        return "manual_whitelist"
    # 计划名称可能在夜间收口/早八改名，恢复时必须优先按稳定的计划 ID 搜索。
    # 否则旧标签里的名称会导致“计划存在但未找到”，漏掉应恢复计划。
    keyword = pid or name
    if not keyword:
        return "failed:no_keyword"

    async def _find_row(time_range: str):
        rows = await _search_plans(
            page, keyword, dept, by_creator=False, time_range=time_range,
        )
        for tr in rows:
            try:
                metrics = await _parse_plan_row(tr)
            except Exception:
                continue
            if metrics is None:  # 畸形行已留 WARNING，跳过
                continue
            if pid and metrics.plan_id == pid:
                return tr
            if name and metrics.plan_name == name:
                return tr
        return None

    target_row = await _find_row(target.get("time_range") or "今天")
    if target_row is None and not target.get("time_range"):
        # 2026-10-07 实证：老池恢复队列条目不带 time_range，默认「今天」窗口
        # 会把数周无数据的暂停计划过滤掉（08:00 槽 retry=not_found）；
        # 且 _search_plans 无结果时会兜底返回可见残留行（非空），所以必须按
        # 「未匹配到目标行」判定而不是按「rows 为空」判定。
        log.info("[%s] %s 「今天」窗口未命中，放宽近180天重搜", dept, keyword)
        target_row = await _find_row("近180天")
    if target_row is None:
        return "not_found"
    result = await _set_plan_power(page, target_row, turn_on=True, verify_word="投放中")
    if result == "ok":
        return "enabled"
    if result == "already":
        return "already"
    return result


async def _legacy_enable_batch(dry_run: bool = False) -> dict:
    state = load_state()
    done_ids = set(state.get("enabled") or [])
    pending: dict[str, list[dict]] = {}
    known = set(done_ids)

    def add_target(item: dict) -> bool:
        key = item.get("plan_id") or item.get("plan_name")
        if not key or key in known:
            return False
        if is_manual_whitelisted(
            department=item.get("department") or "",
            plan_id=item.get("plan_id") or "",
            plan_name=item.get("plan_name") or "",
        ):
            log.info("[%s] 跳过人工白名单计划: %s", item.get("department"), key)
            return False
        pending.setdefault(item["department"], []).append(item)
        known.add(key)
        return True

    # 00:00-08:00 自动创建的计划：由 plan_create --pause-after 精确记账。
    for item in state.get("night_created") or []:
        add_target({**item, "source": "night_created", "time_range": "今天"})

    # 昨日实际 ROI 合格的计划：必须显式切到「昨天」，不使用今天的实时数据。
    previous_day_targets = 0
    for dept in DEPARTMENTS:
        try:
            plans = await list_all_plans(dept, time_range="昨天")
        except Exception as exc:
            log.error("[%s] 昨日 ROI 扫描失败: %s", dept, exc)
            continue
        for p in plans:
            if p.roi is None or p.roi <= PREVIOUS_DAY_ROI_THRESHOLD:
                continue
            if add_target({
                "department": dept,
                "plan_id": p.plan_id,
                "plan_name": p.plan_name,
                "previous_day_roi": p.roi,
                "source": "previous_day_roi",
                "time_range": "昨天",
            }):
                previous_day_targets += 1

    total = sum(len(v) for v in pending.values())
    night_created_targets = sum(
        1 for items in pending.values() for item in items
        if item.get("source") == "night_created"
    )
    log.info("待开启 %d 条（昨日报 ROI>%.1f: %d，夜间新建: %d；%s）",
             total, PREVIOUS_DAY_ROI_THRESHOLD, previous_day_targets,
             night_created_targets,
             "、".join(f"{d}:{len(v)}" for d, v in pending.items()) or "无")
    if dry_run:
        return {
            "action": "enable-batch",
            "dry_run": True,
            "previous_day_roi_threshold": PREVIOUS_DAY_ROI_THRESHOLD,
            "pending": {
                d: [{
                    "plan_name": i.get("plan_name"),
                    "source": i.get("source"),
                    "previous_day_roi": i.get("previous_day_roi"),
                } for i in v]
                for d, v in pending.items()
            },
        }
    if not total:
        return {"action": "enable-batch", "enabled": 0}

    summary = {"enabled": 0, "already": 0, "not_found": 0, "failed": 0}
    _ensure_cdp()
    for dept, items in pending.items():
        try:
            async with async_playwright() as pw:
                browser = await connect_browser(pw, timeout=8000)
                context = browser.contexts[0]
                page = await open_fresh_page(context)
                for item in items:
                    try:
                        result = await _enable_one(page, item)
                    except Exception as exc:
                        result = f"failed:{str(exc)[:120]}"
                    key = item.get("plan_id") or item.get("plan_name")
                    if result in ("enabled", "already"):
                        state = load_state()
                        state.setdefault("enabled", [])
                        if key not in state["enabled"]:
                            state["enabled"].append(key)
                        save_state(state)
                        summary[result] += 1
                        log.info("[%s] %s → %s", dept, item.get("plan_name"),
                                 "已开启" if result == "enabled" else "本就在投放中")
                    elif result == "not_found":
                        summary["not_found"] += 1
                        log.warning("[%s] 未找到计划: %s（%s）",
                                    dept, item.get("plan_name"), item.get("plan_id"))
                    else:
                        summary["failed"] += 1
                        log.error("[%s] 开启失败: %s → %s",
                                  dept, item.get("plan_name"), result)
        except Exception as exc:
            log.error("[%s] 批量开启会话失败: %s", dept, exc)
            summary["failed"] += len(items)
    return {"action": "enable-batch", **summary}


async def enable_batch(dry_run: bool = False) -> dict:
    """旧 API 兼容入口：早八恢复规则已废止，始终不操作计划。"""
    return {
        "action": "enable-batch",
        "skipped": True,
        "dry_run": dry_run,
        "message": "早八批量开启已废止；新计划在 00:00-07:59 创建后直接投放",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# 时间点动作结束后跑一次对账报告（先报告后执行，见 reconcile.py）
_RECONCILE_ACTIONS = {"close-non-key", "close-old-pool", "pause-all",
                      "resume-old-pool", "pause-old-pool", "pause-new-low-roi",
                      "pause-potential-low-roi", "demote-key-low-roi",
                      "afternoon-follow-up", "pause-one"}


# ---------------------------------------------------------------------------
# 对账差异执行器（2026-09-14 用户拍板，9/15-16 开发，★9/18 灰度收口）
# ---------------------------------------------------------------------------

# off=只报告；shadow=记「将执行」不动浏览器（灰度观察）；apply=真执行。
RECONCILE_APPLY_MODE = "shadow"


async def apply_reconcile_diffs(diffs: list[dict], *, dry_run: bool = True) -> dict:
    """对账差异执行：rewrite/pause 自动，enable 谨慎（差异已在计算侧豁免过滤）。

    rewrite → _set_budget_absolute 写回档位（ROI/预算）；pause → pause_plan
    纯暂停（不挂恢复票——对账 pause 的是窗口外异常在投，恢复交给正常生命周期，
    挂票会造出重复票）；enable → _enable_one。逐条隔离失败，不因一条崩全批。
    shadow（dry_run=True）只产 planned 清单，浏览器都不连。
    """
    planned: list[dict] = []
    for d in diffs:
        kind = str(d.get("diff") or "")
        if kind == "rewrite":
            planned.append({**d, "action": "rewrite",
                            "want_roi": d.get("roi_target_want"),
                            "want_budget": d.get("budget_want")})
        elif kind in ("pause", "enable"):
            planned.append({**d, "action": kind})
    summary: dict = {"dry_run": dry_run, "planned": planned,
                     "applied": [], "failed": []}
    if dry_run or not planned:
        return summary

    _ensure_cdp()
    by_dept: dict[str, list[dict]] = {}
    for item in planned:
        dept = str(item.get("department") or "")
        if dept:
            by_dept.setdefault(dept, []).append(item)
        else:
            summary["failed"].append({**item, "detail": "缺部门，无法定位页面"})
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        for department, items in by_dept.items():
            try:
                page = await _department_page(browser.contexts[0], department)
            except Exception as exc:
                summary["failed"].extend(
                    {**item, "detail": f"部门页打开失败: {str(exc)[:150]}"}
                    for item in items)
                continue
            for item in items:
                try:
                    if item["action"] == "rewrite":
                        out = await _set_budget_absolute(
                            page, item, item.get("want_budget"),
                            roi=item.get("want_roi"))
                        summary["applied"].append({**item, "detail": f"rewrite ok: {out}"})
                    elif item["action"] == "pause":
                        ok, detail = await pause_plan(
                            page, str(item.get("plan_name") or ""),
                            str(item.get("plan_id") or "") or None, department)
                        (summary["applied"] if ok else summary["failed"]).append(
                            {**item, "detail": f"pause: {detail}"})
                    elif item["action"] == "enable":
                        detail = await _enable_one(page, item)
                        (summary["applied"] if detail in ("enabled", "already")
                         else summary["failed"]).append(
                            {**item, "detail": f"enable: {detail}"})
                except Exception as exc:
                    summary["failed"].append({**item, "detail": str(exc)[:200]})
                    log.error("对账执行失败: %s %s (%s)", department,
                              item.get("plan_name"), str(exc)[:150])
    return summary


def _post_action_scan_slot(action: str, requested_slot: str | None = None) -> str:
    """Map an action to the daily label column it must refresh."""
    if action in {"close-old-pool", "close-non-key", "pause-all"}:
        return "23:00"
    if action in {"pause-old-pool", "pause-new-low-roi"}:
        return "12:00" if action == "pause-new-low-roi" else "17:00"
    if action == "pause-potential-low-roi":
        return requested_slot if requested_slot in {"12:00", "17:00"} else _label_slot_now()
    if action == "demote-key-low-roi":
        return requested_slot if requested_slot in {"08:00", "12:00", "17:00"} else _label_slot_now()
    if action == "afternoon-follow-up":
        return requested_slot if requested_slot in {"14:00", "16:00"} else _label_slot_now()
    if action in {"resume-old-pool", "pause-one"}:
        return "08:00" if action == "pause-one" else "12:00"
    return _label_slot_now()


def _post_action_reconcile(action: str, requested_slot: str | None = None) -> None:
    """Rescan after actions, sync DB, then write one post-action reconciliation.

    Platform status/ROI/budget changes are eventually consistent. A single scan
    immediately after a batch action can still show the pre-action state and
    leave plan_catalog wrong (for example, ``投放中`` after a successful pause).
    We therefore perform up to three read-only verification scans; every scan
    refreshes plan_catalog and plan_daily_labels, and only the final scan is
    used for the report.
    """
    try:
        from .reconcile import append_reconcile_report
        from .commission_gate import load_commission_blocked_ids

        slot = _post_action_scan_slot(action, requested_slot)
        final = action in {"close-old-pool", "close-non-key", "pause-all"}
        # enable 差异豁免（2026-09-14 拍板「enable 谨慎」）：佣金阻断名单
        # （人工解禁前不该开）∪ 恢复票在途（暂停是设计，12:00/08:00 链会拉起）
        # ∪ ignored 名单（平台系统暂停+用户要求不再处理——09-14 shadow 首报
        # 实测误报蓉蓉儿/铭妈美食记两条 ignored 计划，当日补入豁免）。
        enable_exempt = set(load_commission_blocked_ids())
        enable_exempt.update(_ignored_resume_keys())
        enable_exempt.update(
            str(item.get("plan_id") or "")
            for item in _load_old_pool_resume_queue().get("pending", [])
            if item.get("plan_id")
        )
        report = None
        for attempt in range(1, 4):
            report = asyncio.run(monitor(
                dry_run=True, scan_slot=slot, final_classification=final
            ))
            counts = report.get("counts") or {}
            log.info(
                "动作后数据库复核 %d/3: action=%s slot=%s counts=%s",
                attempt, action, slot, json.dumps(counts, ensure_ascii=False),
            )
            if attempt < 3:
                time.sleep(3)
        body = append_reconcile_report(
            report or {}, source_action=action,
            stale_zero_order_ids=_verified_stale_zero_order_ids(),
            enable_exempt_ids=enable_exempt,
        )
        log.info(
            "对账报告已写入: action=%s 差异 %d 条（数据库已按动作后复核刷新）",
            action, body["diff_count"],
        )
        # 对账 apply（2026-09-14 用户拍板：rewrite+pause 自动、enable 谨慎）。
        # 灰度期 shadow 只记「将执行」不动浏览器，★9/18 收口后切 apply。
        if body.get("diffs") and RECONCILE_APPLY_MODE != "off":
            from .reconcile import append_apply_report

            apply_result = asyncio.run(apply_reconcile_diffs(
                body["diffs"], dry_run=(RECONCILE_APPLY_MODE == "shadow")))
            append_apply_report(apply_result, source_action=action,
                                mode=RECONCILE_APPLY_MODE)
    except Exception:
        log.exception("动作后数据库复核失败（不影响动作本身）: action=%s", action)


def _main_impl() -> int:
    ap = argparse.ArgumentParser(description="计划生命周期监控、23:00 收口与 12:00 老计划池恢复")
    ap.add_argument("action", choices=[
        "monitor", "close-old-pool", "close-non-key", "pause-all", "capture-old-pool", "restore-pause-exceptions", "resume-old-pool", "pause-old-pool", "pause-new-low-roi", "pause-potential-low-roi", "demote-key-low-roi", "afternoon-follow-up", "pause-one", "enable-batch", "status", "commission-gate"
    ])
    ap.add_argument("--dry-run", action="store_true", help="只扫描记账/列清单，不点开关")
    ap.add_argument("--force", action="store_true", help="忽略自动化总开关，手动执行启停")
    ap.add_argument("--threshold", type=float, default=None,
                    help="pause-new-low-roi：ROI 低于该值的当日新计划停投转老池")
    ap.add_argument("--scan-slot", default=None,
                    help="每日标签落库的扫描档位（如 12:00/17:00/23:00），默认按动作推断")
    ap.add_argument("--name", default="", help="pause-one：计划名称")
    ap.add_argument("--plan-id", default=None, help="pause-one：计划 ID（可选）")
    ap.add_argument("--department", default="", help="pause-one：业务部；commission-gate：只跑该部门（试点）")
    ap.add_argument("--from-poll", action="store_true",
                    help="pause-one 由轮询轮次内调用：跳过等轮询空闲与忙标，避免自等")
    ap.add_argument("--from-report", action="store_true",
                    help="由日报进程内调用（14:00/16:00 下午跟进）：忙标就是父进程自己举的，跳过等播报空闲，避免自等 15 分钟（2026-09-03 16pm 档因此迟推半小时）")
    ap.add_argument("--resume-date", default=None,
                    help="pause-one：预约投放日期 YYYY-MM-DD，默认按当前夜间窗口顺延")
    ap.add_argument("--roi", type=float, default=None,
                    help="pause-one：该计划的目标 ROI（旧素材通道 6.5 等，默认按新计划 4.5）；"
                         "写入恢复队列与每日标签，08:00 归一按此值保持，不打回 4.5")
    ap.add_argument("--label", default="",
                    help="pause-one：通道标签（如 旧素材补投），随恢复队列与看板落库")
    args = ap.parse_args()

    busy = args.action in _BROWSER_ACTIONS and not args.dry_run and not args.from_poll
    lock_fd, lock_held = None, False
    # pythonw 下 print 全丢，启动和异常都必须落日志文件（2026-08-21 23:00 收口
    # 曾静默 exit 1：无任何日志、忙标未挂，事后无法定位）。
    log.info("plan_power 启动: action=%s dry_run=%s force=%s",
             args.action, args.dry_run, args.force)
    if args.action in _BROWSER_ACTIONS and not args.dry_run:
        if not args.from_report:
            _wait_report_idle()  # 大节点播报扫描优先：扫描在跑就等它结束
        if not args.from_poll:
            _wait_poll_round_idle()
            _busy_mark(args.action)
        lock_fd, lock_held = _acquire_action_lock()
    try:
        if args.action != "status" and not args.dry_run and not args.force and not automation_enabled():
            print(json.dumps({
                "action": args.action,
                "skipped": True,
                "message": "自动化总开关已关闭，未操作计划（手动执行请加 --force）",
            }, ensure_ascii=False))
            return 0
        if args.action == "status":
            state = load_state()
            out = {
                "key_kept_at_close": len(state.get("key_kept") or []),
                "closed_non_key": len(state.get("closed_non_key") or []),
                "old_pool_paused": len(state.get("old_pool_paused") or []),
                "old_pool_pending_resume": len(_load_old_pool_resume_queue().get("pending") or []),
                "old_pool_enabled": len(state.get("old_pool_enabled") or []),
                "legacy_midnight_paused": len(state.get("midnight_paused") or []),
                "legacy_night_created": len(state.get("night_created") or []),
                "legacy_enabled": len(state.get("enabled") or []),
                "updated_at": state.get("updated_at"),
            }
        elif args.action == "close-old-pool":
            out = asyncio.run(close_old_pool(args.dry_run))
        elif args.action in ("close-non-key", "pause-all"):
            # pause-all 仅保留为旧任务的安全兼容入口；不再执行全量暂停。
            out = asyncio.run(close_non_key(args.dry_run))
            if args.action == "pause-all":
                out["legacy_alias"] = "pause-all 已改为 close-non-key"
        elif args.action == "resume-old-pool":
            out = asyncio.run(resume_old_pool(args.dry_run))
        elif args.action == "pause-old-pool":
            out = asyncio.run(pause_old_pool(args.dry_run))
        elif args.action == "afternoon-follow-up":
            if args.scan_slot not in {"14:00", "16:00"}:
                print(json.dumps({"status": "failed", "message": "afternoon-follow-up 需要 --scan-slot 14:00 或 16:00"}, ensure_ascii=False))
                return 1
            out = asyncio.run(afternoon_follow_up(args.scan_slot, args.dry_run))
        elif args.action == "pause-new-low-roi":
            if args.threshold is None:
                print(json.dumps({"status": "failed",
                                  "message": "pause-new-low-roi 需要 --threshold"},
                                 ensure_ascii=False))
                return 1
            out = asyncio.run(pause_new_low_roi(args.threshold, args.dry_run,
                                                scan_slot=args.scan_slot))
        elif args.action == "pause-potential-low-roi":
            if args.threshold is None:
                print(json.dumps({"status": "failed",
                                  "message": "pause-potential-low-roi 需要 --threshold"},
                                 ensure_ascii=False))
                return 1
            out = asyncio.run(pause_potential_low_roi(args.threshold, args.dry_run,
                                                      scan_slot=args.scan_slot))
        elif args.action == "demote-key-low-roi":
            if args.threshold is None:
                print(json.dumps({"status": "failed",
                                  "message": "demote-key-low-roi 需要 --threshold"},
                                 ensure_ascii=False))
                return 1
            out = asyncio.run(demote_key_low_roi(args.threshold, args.dry_run,
                                                 scan_slot=args.scan_slot))
        elif args.action == "commission-gate":
            # 佣金闸（2026-09-07）：>commission_max_pct 的存量在投计划批量停。
            # 函数内 import：commission_gate 顶部只依赖标准库，反过来 plan_power
            # 若在顶部 import 它会经 plan_sweep→plan_create 形成循环。
            from .commission_gate import commission_gate_run
            deps = [args.department] if args.department else None
            out = asyncio.run(commission_gate_run(args.dry_run, departments=deps))
        elif args.action == "pause-one":
            if not args.name or not args.department:
                print(json.dumps({"status": "failed",
                                  "message": "pause-one 需要 --name 和 --department"},
                                 ensure_ascii=False))
                return 1
            out = asyncio.run(pause_one(args.name, args.plan_id, args.department,
                                        args.dry_run, args.resume_date, roi=args.roi,
                                        label=args.label))
        elif args.action == "restore-pause-exceptions":
            out = asyncio.run(restore_pause_exceptions(args.dry_run))
        elif args.action == "capture-old-pool":
            out = capture_old_pool_resume(args.dry_run)
        elif args.action == "enable-batch":
            out = {
                "action": "enable-batch", "skipped": True,
                "message": "早八批量开启已废止；新计划在 00:00-07:59 创建后直接投放",
            }
        else:
            out = asyncio.run(monitor(args.dry_run))
        if args.action in _RECONCILE_ACTIONS and not args.dry_run:
            # 轮询内 pause-one 跳过三遍复核：乘方改版后单次全量扫描约 8min，
            # 三遍超过轮询单步 1200s 上限——动作本身已成功却被判超时失败空转重试
            # （2026-09-26 rNS81Z 实证）；08:00 恢复槽与 23:00 收口自带复核兜底。
            if not (args.action == "pause-one" and args.from_poll):
                _post_action_reconcile(args.action, args.scan_slot)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    except Exception as exc:
        log.exception("plan_power 失败: action=%s", args.action)
        print(json.dumps({"status": "failed", "message": str(exc)[:300]},
                         ensure_ascii=False))
        return 1
    finally:
        if lock_fd is not None:
            _release_action_lock(lock_fd, lock_held)
        if busy:
            _busy_clear()


def main() -> int:
    """最外层兜底：进入 try 之前（忙标/等空闲/动作锁）的异常也要落盘。

    2026-10-09 事故：_busy_mark 里 NameError（漏 import os）发生在主 try 之外，
    pythonw 下 stderr 全丢，23:00/08:00 两槽「无声死亡」——分类闸没写整夜零建计划。
    """
    try:
        return _main_impl()
    except SystemExit:
        raise
    except Exception:
        log.exception("plan_power 致命异常（发生在主流程 try 之外，pythonw 下 stderr 不可见）")
        return 2


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
