# -*- coding: utf-8 -*-
"""对账引擎（先报告后执行，2026-08-24 起）。

数据库（webui.db 的 plan_catalog）是事实源：每次 plan_power 扫描都会刷新。
每个时间点由 desired_state 按策略算出每个计划应有的开关与参数，
compute_diffs 对照实际状态产出差异清单。当前只写报告
（wecom-agent/logs/reconcile_YYYYMMDD.jsonl + 日志），不执行动作；
报告稳定 2-3 天、人工确认无误后再切自动执行。

策略窗口（2026-08-23 策略）：
- 重点计划：全天投放，10.3 / 不限额(99999)
- 潜力计划：08:00-23:00 投放，7.3 / 300
- 新计划：  08:00-23:00 投放；12:00 达标后仍属新计划，但参数升为 7.3，
  日预算仅补足到剩余 300 元
- 老计划池：12:00-17:10 投放，10.3 / 300（其余时间暂停）
- 人工白名单：AI 永不动
- 已核验 15 天 0 单僵尸计划：永不自动恢复
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from .lifecycle import (
    KEY_PLAN_DAILY_BUDGET,
    KEY_PLAN_ROI_TARGET,
    NEW_PLAN_DAILY_BUDGET,
    NEW_PLAN_ROI_TARGET,
    OLD_POOL_DAILY_BUDGET,
    OLD_POOL_ROI_TARGET,
    POTENTIAL_PLAN_DAILY_BUDGET,
    POTENTIAL_PLAN_ROI_TARGET,
)

log = logging.getLogger("reconcile")

_LOG_DIR = Path(__file__).resolve().parents[2] / "wecom-agent" / "logs"

# (start_hhmm, end_hhmm)，end 为开区间
_WINDOWS = {
    "key_plan": ("00:00", "24:00"),
    "potential_plan": ("08:00", "23:00"),
    "new_plan": ("08:00", "23:00"),
    "old_pool": ("12:00", "17:10"),
}
_PARAMS = {
    "key_plan": (KEY_PLAN_ROI_TARGET, KEY_PLAN_DAILY_BUDGET),
    "potential_plan": (POTENTIAL_PLAN_ROI_TARGET, POTENTIAL_PLAN_DAILY_BUDGET),
    "new_plan": (NEW_PLAN_ROI_TARGET, NEW_PLAN_DAILY_BUDGET),
    "old_pool": (OLD_POOL_ROI_TARGET, OLD_POOL_DAILY_BUDGET),
}


def _in_window(hhmm: str, window: tuple[str, str]) -> bool:
    return window[0] <= hhmm < window[1]


def desired_state(entry: dict, now: datetime.datetime,
                  stale_zero_order_ids: set[str] | None = None) -> dict:
    """算一个计划在此刻的期望状态。

    返回 {"category", "should_run", "window", "roi_target", "budget", "note"}。
    should_run 为 None 表示 AI 不该干预（白名单/未知分类），对账永不产生差异。
    """
    category = str(entry.get("category") or "")
    plan_id = str(entry.get("plan_id") or "")
    hhmm = now.strftime("%H:%M")
    if category == "manual_review" or category not in _WINDOWS:
        return {
            "category": category, "should_run": None, "window": None,
            "roi_target": None, "budget": None,
            "note": "人工白名单/未知分类，AI 不干预",
        }
    if stale_zero_order_ids and plan_id in stale_zero_order_ids:
        return {
            "category": category, "should_run": False,
            "window": _WINDOWS[category],
            "roi_target": _PARAMS[category][0], "budget": _PARAMS[category][1],
            "note": "已核验 15 天 0 单，永不自动恢复",
        }
    window = _WINDOWS[category]
    roi_target, budget = _PARAMS[category]
    # 12:00 已通过 4.5 检查的新计划仍然是 new_plan；对账应遵守其
    # 7.3 运行档和当时计算出的“剩余预算补足300元”日预算，不能报成 4.5/300 漂移。
    if category == "new_plan" and entry.get("new_plan_stage") == "roi_7_3":
        roi_target = POTENTIAL_PLAN_ROI_TARGET
        budget = entry.get("daily_budget") or entry.get("budget")
    # 23:00 正式标为潜力计划时，同一条 12:00 升档计划的预算也不得被
    # 对账误判为需要重写到固定 300。
    elif category == "potential_plan" and entry.get("preserve_promoted_budget"):
        roi_target = POTENTIAL_PLAN_ROI_TARGET
        budget = entry.get("daily_budget") or entry.get("budget")
    should_run = _in_window(hhmm, window)
    note = "" if should_run else "窗口外应暂停"
    # 17:00 ROI<6 的暂停是新计划当日的风险控制，不代表已经归老池。
    # 在23:00最终分类前，对账应认可该暂停，避免反复报“应开启”。
    if category == "new_plan" and entry.get("paused_at_17"):
        should_run = False
        note = "17:00 ROI<6 日内暂停，保持新计划，23:00再分类"
    return {
        "category": category,
        "should_run": should_run,
        "window": window,
        "roi_target": roi_target,
        "budget": budget,
        "note": note,
    }


def _close(a, b, eps: float = 0.001) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return False


def compute_diffs(entries: list[dict], now: datetime.datetime,
                  stale_zero_order_ids: set[str] | None = None,
                  enable_exempt_ids: set[str] | None = None) -> list[dict]:
    """对照期望状态与实际状态，产出差异清单（只描述差异，不执行）。

    enable_exempt_ids：enable 差异豁免名单（2026-09-14 用户拍板「enable 谨慎」）。
    暂停待发挂票（12:00 链会拉起）和佣金阻断（人工解禁前不该开）的计划，
    暂停是设计行为，不该报「应投放」——否则将来 apply 会把 12:00 链和
    佣金闸全短路（09-14 已实测误报暂停待发的 0914_呵呵好物_黄/安夏好物_黄）。
    """
    diffs: list[dict] = []
    for entry in entries:
        if entry.get("error"):
            continue
        desired = desired_state(entry, now, stale_zero_order_ids)
        if desired["should_run"] is None:
            continue
        status = str(entry.get("status") or "")
        running = "投放中" in status
        paused = "暂停" in status
        base = {
            "plan_id": str(entry.get("plan_id") or ""),
            "plan_name": str(entry.get("plan_name") or ""),
            "department": str(entry.get("department") or ""),
            "category": desired["category"],
            "status": status,
            "roi_target_now": entry.get("roi_target"),
            "budget_now": entry.get("budget"),
            "spent": entry.get("spent"),
            "roi": entry.get("roi"),
            "orders": entry.get("orders"),
        }
        if desired["should_run"] and not running:
            if paused or status:
                if enable_exempt_ids and base["plan_id"] in enable_exempt_ids:
                    continue
                diffs.append({
                    **base, "diff": "enable",
                    "detail": desired["note"] or f"{desired['window'][0]}-{desired['window'][1]} 应投放",
                })
            continue
        if not desired["should_run"] and running:
            diffs.append({
                **base, "diff": "pause",
                "detail": desired["note"],
            })
            continue
        if desired["should_run"] and running:
            want_roi, want_budget = desired["roi_target"], desired["budget"]
            param_mismatch = (
                not _close(entry.get("roi_target"), want_roi)
                or not _close(entry.get("budget"), want_budget, eps=0.5)
            )
            # roi_target 未入库（老数据）时不报参数漂移，避免误报
            if param_mismatch and entry.get("roi_target") is not None:
                diffs.append({
                    **base, "diff": "rewrite",
                    "detail": (
                        f"参数漂移：应为 {want_roi}/{want_budget}，"
                        f"实际 {entry.get('roi_target')}/{entry.get('budget')}"
                    ),
                    "roi_target_want": want_roi,
                    "budget_want": want_budget,
                })
    return diffs


def append_reconcile_report(report: dict, *, source_action: str,
                            now: datetime.datetime | None = None,
                            stale_zero_order_ids: set[str] | None = None,
                            enable_exempt_ids: set[str] | None = None) -> dict:
    """用一次 monitor 报告算差异并写 JSONL + 日志。返回报告体。"""
    now = now or datetime.datetime.now()
    entries: list[dict] = []
    for department, plans in (report.get("plans") or {}).items():
        for entry in plans or []:
            if isinstance(entry, dict):
                entry.setdefault("department", department)
                entries.append(entry)
    diffs = compute_diffs(entries, now, stale_zero_order_ids, enable_exempt_ids)
    body = {
        "ts": now.isoformat(timespec="seconds"),
        "source_action": source_action,
        "mode": "report-only",
        "counts": report.get("counts") or {},
        "diff_count": len(diffs),
        "diffs": diffs,
    }
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOG_DIR / f"reconcile_{now:%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(body, ensure_ascii=False) + "\n")
    if diffs:
        for item in diffs:
            log.info(
                "对账差异[%s] %s/%s（%s）：%s",
                item["diff"], item["department"], item["plan_name"],
                item["category"], item["detail"],
            )
    else:
        log.info("对账无差异（%s，扫描 %d 条）", source_action, len(entries))
    return body


def append_apply_report(result: dict, *, source_action: str, mode: str,
                        now: datetime.datetime | None = None) -> dict:
    """对账执行结果落同一本 JSONL（source_action 后缀 :apply 与差异报告区分）。

    shadow 模式下 planned=将执行清单、applied/failed 为空——灰度期（★9/18 收口前）
    靠这本账核对「自动执行到底会干什么」，确认无误再切 apply。
    """
    now = now or datetime.datetime.now()
    body = {
        "ts": now.isoformat(timespec="seconds"),
        "source_action": f"{source_action}:apply",
        "mode": mode,
        "planned_count": len(result.get("planned") or []),
        "planned": result.get("planned") or [],
        "applied": result.get("applied") or [],
        "failed": result.get("failed") or [],
    }
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOG_DIR / f"reconcile_{now:%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(body, ensure_ascii=False) + "\n")
    log.info("对账执行[%s]: 计划 %d 条，成功 %d，失败 %d（%s）",
             mode, body["planned_count"], len(body["applied"]),
             len(body["failed"]), source_action)
    return body
