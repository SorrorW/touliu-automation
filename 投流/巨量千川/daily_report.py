"""每日投放数据汇总（企微群播报用）。

扫描三部门当天总览，汇总消耗/成交/计划数，并按生命周期区分：
今日新计划、重点计划、潜力计划、老计划池。stdout 输出 JSON：
{date, departments: {dep: {spent, orders, plans, roi}}, totals, markdown, markdown_by_dept}

markdown 为原有汇总播报（推汇总群）；markdown_by_dept 为按计划分类的部门播报。
ROI 为消耗加权综合 ROI（Σ(计划ROI×消耗)/Σ消耗，只统计有消耗的条目）。
所有时段均隐藏无消耗计划；08:00—17:00 的分母是扫描时当前在投数，
22:30 的分母是当天全量去重计划数，但明细仍只展开有消耗计划。

CLI: python -m 巨量千川.daily_report
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import re
import sys

from .daily_high_performer_monitor import scan_all_plans
from .lifecycle import classify_plan_lifecycle


CATEGORY_META = (
    ("key_plan", "重点计划"),
    ("potential_plan", "潜力计划"),
    ("new_plan", "今日新计划 / 老计划新素材（ROI目标 4.5）"),
    ("old_pool", "老计划池"),
)
# 日间阶段播报只看当前仍在投放的计划；17:00/22:30 才纳入当天早前
# 开启后又暂停/熔断的计划，作为全量汇总。
CURRENT_ONLY_REPORT_SLOTS = frozenset({"08:00", "10:00", "12:00", "14:00", "16:00"})
FULL_REPORT_SLOTS = frozenset({"17:00", "22:30"})
# 企微 Markdown 上限为 4096 字节；预留四个分类标题和“其余计划”统计空间。
MAX_MARKDOWN_BYTES = 3000


def _plan_date_from_name(plan_name: str) -> datetime.date | None:
    """Parse the leading delivery date used by current/historic plan names."""
    name = (plan_name or "").strip()
    match = re.match(r"^(\d{1,2})\s*[._/-]\s*(\d{1,2})(?:\D|$)", name)
    if not match:
        match = re.match(r"^(\d{2})(\d{2})(?:[_-]|\D)", name)
    if not match:
        match = re.match(r"^(\d)(\d{2})(?:[_-]|\D)", name)
    if not match:
        return None
    try:
        return datetime.date(datetime.date.today().year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _plan_category(plan: dict) -> str:
    """Classify report plans while preserving the daytime new-plan stage.

    ROI target 4.5 marks a fresh test plan (including an old plan with new
    material).  At 12:00, a passing plan is promoted to target 7.3 but remains
    a ``new_plan`` until the 23:00 close classifies it finally.  The date prefix
    distinguishes that daytime 7.3 stage from an older potential plan.
    """
    roi_target = plan.get("roi_target")
    try:
        target = float(roi_target) if roi_target is not None else None
    except (TypeError, ValueError):
        target = None
    if target is not None and abs(target - 4.5) < 0.001:
        return "new_plan"
    if (
        target is not None
        and abs(target - 7.3) < 0.001
        and _plan_date_from_name(str(plan.get("plan_name") or "")) == datetime.date.today()
    ):
        return "new_plan"
    decision = classify_plan_lifecycle(
        roi=plan.get("roi"),
        orders=plan.get("orders"),
        is_today_new=False,
    )
    return decision.category


def _prepare_plan(plan: dict, department: str) -> dict:
    item = dict(plan)
    item["department"] = department
    item["category"] = _plan_category(item)
    item["spent"] = round(float(item.get("spent") or 0), 2)
    item["orders"] = int(item.get("orders") or 0)
    return item


def _has_orders(plan: dict) -> bool:
    try:
        return int(plan.get("orders") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_reportable(plan: dict, *, include_started_today: bool = False) -> bool:
    """日报过滤；当天扫描过的计划不能因后续状态变化而消失。"""
    if include_started_today and (
        plan.get("started_today") or plan.get("scanned_today")
    ):
        return True
    if "暂停" not in str(plan.get("status") or ""):
        return True
    # 2026-09-03 用户定：有出单的被暂停计划也要播报（含手动暂停）。
    if _has_orders(plan):
        return True
    return plan.get("pause_category") == "budget_exhausted"


def _report_history_slot(title_suffix: str, scan_slot: str | None) -> str:
    """Normalize a report run to the daily-label slot used by SQLite history."""
    if scan_slot:
        return scan_slot
    for slot in ("08:00", "10:00", "12:00", "14:00", "16:00", "17:00", "22:30"):
        if slot in (title_suffix or ""):
            return slot
    return "23:00"


def _snapshot_report_for_history(report: dict, *, scan_slot: str, label_date: str) -> None:
    """Persist the current report as evidence of today's active plans."""
    try:
        from webui.db import record_plan_daily_labels

        labeled: dict[str, list[dict]] = {}
        for department, payload in (report or {}).items():
            if not isinstance(payload, dict) or payload.get("error"):
                continue
            items = []
            for plan in payload.get("plans") or []:
                item = dict(plan)
                item["department"] = department
                item["category"] = _plan_category(item)
                items.append(item)
            labeled[department] = items
        record_plan_daily_labels(labeled, scan_slot=scan_slot, label_date=label_date)
    except Exception:
        # History must never make a read-only report fail; caller still has the
        # live scan and the error is intentionally not surfaced to WeCom users.
        return


def _merge_live_metrics_from_history(report: dict, *, label_date: str) -> dict:
    """防止同日后续扫描的异常快照把累计指标冲低。

    千川总览偶尔会返回不完整/较小的当日快照；消耗和成交是单调累计值，
    对仍出现在本次扫描中的计划取历史最大有效值。这样 17:00/22:30
    不会把 12:00 已确认的数据覆盖掉，也不影响状态和计划集合口径。
    """
    try:
        from webui.db import list_plan_daily_labels
        history = list_plan_daily_labels(label_date, active_only=False)
    except Exception:
        return report
    latest: dict[str, dict] = {}
    for row in history:
        plan_id = str(row.get("plan_id") or "").strip()
        if plan_id and plan_id not in latest:
            latest[plan_id] = row
    for payload in (report or {}).values():
        if not isinstance(payload, dict):
            continue
        for plan in payload.get("plans") or []:
            old = latest.get(str(plan.get("plan_id") or "").strip())
            if not old:
                continue
            try:
                old_spent = float(old.get("spent") or 0)
                new_spent = float(plan.get("spent") or 0)
            except (TypeError, ValueError):
                continue
            if old_spent > new_spent:
                plan["spent"] = old_spent
                if old.get("roi") is not None:
                    plan["roi"] = old["roi"]
            try:
                old_orders = int(old.get("orders") or 0)
                new_orders = int(plan.get("orders") or 0)
            except (TypeError, ValueError):
                continue
            if old_orders > new_orders:
                plan["orders"] = old_orders
    return report


def _merge_started_today_history(
    report: dict, *, label_date: str
) -> tuple[dict, int]:
    """Merge plans scanned at an earlier slot into the current report.

    Live rows remain authoritative for latest spend/ROI/status. Historical rows
    only fill missing plans and mark live rows as scanned today.
    """
    try:
        from webui.db import list_plan_daily_labels
        # 任何时间档位扫描到的计划都属于当日投放范围。不能只查当时
        # 「投放中」的行，否则12:00/17:00熔断或已暂停计划会被日报漏掉。
        history = list_plan_daily_labels(label_date, active_only=False)
    except Exception:
        return report, 0

    latest: dict[str, dict] = {}
    for row in history:
        plan_id = str(row.get("plan_id") or "").strip()
        if plan_id and plan_id not in latest:
            latest[plan_id] = row

    merged = {department: dict(payload) for department, payload in (report or {}).items()}
    current_by_id: dict[str, dict] = {}
    for department, payload in merged.items():
        if not isinstance(payload, dict):
            continue
        plans = [dict(item) for item in (payload.get("plans") or [])]
        payload["plans"] = plans
        # 2026-09-01 用户定：22:30 档分母改用当次扫描数（平台当前视图列出的计划数），
        # 不再用合并历史后的当天全量。在追加历史行之前记下当次扫描规模。
        fresh_ids = {str(item.get("plan_id") or "").strip() for item in plans}
        fresh_ids.discard("")
        payload["fresh_scan_total"] = len(fresh_ids) or len(plans)
        for item in plans:
            pid = str(item.get("plan_id") or "").strip()
            if pid:
                current_by_id[pid] = item

    added = 0
    for plan_id, row in latest.items():
        current = current_by_id.get(plan_id)
        if current is not None:
            current["started_today"] = True
            current["scanned_today"] = True
            current["first_seen_active_slot"] = row.get("scan_slot")
            if "暂停" in str(current.get("status") or ""):
                current["today_last_active_status"] = row.get("plan_status")
            continue

        department = str(row.get("department") or "")
        if not department:
            continue
        item = {
            "plan_id": plan_id,
            "plan_name": row.get("plan_name") or "",
            "status": "已暂停（今日曾投放）",
            "today_last_active_status": row.get("plan_status") or "投放中",
            "started_today": True,
            "scanned_today": True,
            "first_seen_active_slot": row.get("scan_slot"),
            "creator_name": None,
            "spent": row.get("spent") or 0,
            "budget": row.get("daily_budget"),
            "roi": row.get("roi"),
            "roi_target": row.get("roi_target"),
            "orders": row.get("orders") or 0,
            "net_deal_amount": None,
            "pause_reason": None,
            "pause_category": None,
        }
        merged.setdefault(department, {"total_plans": 0, "qualified_count": 0, "plans": []})
        merged[department].setdefault("plans", []).append(item)
        added += 1
    return merged, added


def _category_summary(plans: list[dict]) -> dict:
    spent = round(sum(p.get("spent") or 0 for p in plans), 2)
    orders = sum(p.get("orders") or 0 for p in plans)
    weighted = sum(
        (p.get("roi") or 0) * (p.get("spent") or 0)
        for p in plans if p.get("roi") is not None and p.get("spent")
    )
    return {
        "count": len(plans),
        "spent": spent,
        "orders": orders,
        "roi": round(weighted / spent, 2) if spent else None,
    }


def _is_active_at_scan(plan: dict) -> bool:
    """判断扫描截止时是否仍为开启/投放中的计划。"""
    return "暂停" not in str(plan.get("status") or "")


def aggregate(report: dict, *, include_started_today: bool = False,
             scan_slot: str | None = None) -> tuple[dict, dict]:
    """扫描报告 → 各部门汇总；夜间可包含当天曾投放后暂停的计划。

    ``plans``/``spent_plans`` 是本次播报实际展示的有消耗计划数；
    ``active_plans`` 是扫描截止时当前在投数，``total_plans`` 是扫描/合并后的计划总数，
    ``plan_denominator`` 按时段选择当前在投或 22:30 当次扫描数口径。
    """
    deps: dict[str, dict] = {}
    # 22:30 的分子是当天全量有消耗计划；分母是当次扫描数（2026-09-01 用户改，
    # 原为当天全量口径）。其余对外播报只统计扫描截止时仍在投的计划。
    # 17:00 即使会读取历史，也不能把早前已暂停的计划算进当前在投分子。
    full_spent_scope = scan_slot == "22:30" or (scan_slot is None and include_started_today)
    for dep, rec in (report or {}).items():
        raw_plans = list((rec or {}).get("plans") or [])
        active_plans = sum(1 for plan in raw_plans if _is_active_at_scan(plan))
        ids = {str(plan.get("plan_id") or "").strip() for plan in raw_plans}
        ids.discard("")
        scanned_total = max(
            len(ids) or len(raw_plans),
            int((rec or {}).get("total_plans") or 0),
        )
        reportable = [
            plan for plan in raw_plans
            if _is_reportable(plan, include_started_today=include_started_today)
        ]
        # 所有档位隐藏无消耗计划；17:00 也只播当前仍在投且有消耗的计划，
        # 只有 22:30 把当天曾投放后暂停的有消耗计划纳入明细。
        # 2026-09-03 用户定：有出单的被暂停计划任何档位都播（含手动暂停）。
        plans = [
            plan for plan in reportable
            if float(plan.get("spent") or 0) > 0
            and (full_spent_scope or _is_active_at_scan(plan) or _has_orders(plan))
        ]
        spent_plans = len(plans)
        # 22:30 全量档分母 = 当次扫描数（2026-09-01 用户定）：平台「今天」视图
        # 当前列出的计划数。零消耗的已停计划晚间不再列出，故比当天全量小。
        # 有消耗计划始终带数据、必然在当次扫描内，分子不会大于分母。
        fresh_total = int((rec or {}).get("fresh_scan_total") or 0)
        spent = sum(p.get("spent") or 0 for p in plans)
        orders = sum(p.get("orders") or 0 for p in plans)
        weighted = sum(
            (p.get("roi") or 0) * (p.get("spent") or 0)
            for p in plans if p.get("roi") and p.get("spent")
        )
        categorized = [_prepare_plan(plan, dep) for plan in plans]
        categories = {
            key: [plan for plan in categorized if plan["category"] == key]
            for key, _ in CATEGORY_META
        }
        deps[dep] = {
            "spent": round(spent, 2),
            "orders": orders,
            "plans": len(plans),
            "spent_plans": spent_plans,
            "active_plans": active_plans,
            "total_plans": scanned_total,
            "plan_denominator": (fresh_total or scanned_total) if full_spent_scope else active_plans,
            "roi": round(weighted / spent, 2) if spent > 0 else None,
            "categories": categories,
            "category_totals": {
                key: _category_summary(items) for key, items in categories.items()
            },
        }
    totals = {
        "spent": round(sum(d["spent"] for d in deps.values()), 2),
        "orders": sum(d["orders"] for d in deps.values()),
        "plans": sum(d["plans"] for d in deps.values()),
        "spent_plans": sum(d["spent_plans"] for d in deps.values()),
        "active_plans": sum(d["active_plans"] for d in deps.values()),
        "total_plans": sum(d["total_plans"] for d in deps.values()),
        "plan_denominator": sum(d["plan_denominator"] for d in deps.values()),
    }
    num = sum((d["roi"] or 0) * d["spent"] for d in deps.values() if d["roi"])
    totals["roi"] = round(num / totals["spent"], 2) if totals["spent"] > 0 else None
    return deps, totals


def _format_detail(value: object, *, decimals: int = 0) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if decimals:
        return f"{number:,.{decimals}f}"
    return f"{number:,.0f}"


def _plan_line(plan: dict, include_department: bool, *, include_details: bool = True) -> str:
    name = plan.get("plan_name") or plan.get("creator_name") or "未识别计划"
    prefix = f"{plan['department']}｜" if include_department else ""
    roi = f"，ROI **{plan['roi']:g}**" if plan.get("roi") is not None else "，ROI 待观察"
    target = f"，ROI目标 {plan['roi_target']:g}" if plan.get("roi_target") is not None else ""
    budget = f"，预算 {plan['budget']:,.0f} 元" if plan.get("budget") else ""
    status_note = ""
    if "暂停" in str(plan.get("status") or ""):
        status_note = ("，当前已暂停（今日曾投放）" if plan.get("started_today")
                       else "，当前已暂停")
    line = (
        f"> {prefix}{name}：消耗 {plan.get('spent', 0):,.2f} 元，"
        f"成交 **{plan.get('orders', 0)}** 单{roi}{target}{budget}{status_note}"
    )
    if not include_details:
        return line

    # 全域（ROI2）产品不提供曝光/点击漏斗指标：管理表列目录与数据V2报表
    # 模块均无此字段（2026-08-21 逐目录核实）；此前展示的「曝光/点击/CTR」
    # 实为助推子集数据（恒为个位数），误导已移除，只保留净成交额。
    net_deal = _format_detail(plan.get("net_deal_amount"), decimals=2)
    return f"{line}\n> 净成交额 {net_deal} 元"


def _append_category_sections(lines: list[str], categories: dict[str, list[dict]], *,
                              include_department: bool) -> None:
    """追加四类计划，容量不足时保留分类统计并汇总未展开的计划。"""
    for category_index, (key, title) in enumerate(CATEGORY_META):
        if category_index:
            lines.append("")
        # 出单计划必须优先展示，避免被高消耗但零单的计划挤进“其余”汇总。
        plans = sorted(
            categories.get(key) or [],
            key=lambda item: (-(item.get("orders") or 0), -(item.get("spent") or 0),
                              item.get("plan_name") or ""),
        )
        summary = _category_summary(plans)
        roi = f"，ROI **{summary['roi']:g}**" if summary.get("roi") is not None else ""
        lines.append(
            f"**{title}（{summary['count']} 条）**"
            f"：消耗 {summary['spent']:,.2f} 元，成交 **{summary['orders']}** 单{roi}"
        )
        shown = 0
        for plan in plans:
            line = _plan_line(
                plan,
                include_department,
                include_details=key != "old_pool",
            )
            if len("\n".join(lines + [line]).encode("utf-8")) > MAX_MARKDOWN_BYTES:
                break
            lines.append(line)
            shown += 1
        if shown < len(plans):
            rest = plans[shown:]
            rest_summary = _category_summary(rest)
            lines.append(
                f"> …其余 {rest_summary['count']} 条：消耗 {rest_summary['spent']:,.2f} 元，"
                f"成交 **{rest_summary['orders']}** 单"
            )


def _merge_matrix_department(report: dict) -> dict:
    """业务1部-1（矩阵号）数据并入业务1部展示（2026-09-03 用户定「放到业务一部里」）。

    只动播报展示层，在快照/历史合并之后调用：标签库与历史留痕仍按真实账号
    归属保存（部门级浏览器路由依赖它），这里只把矩阵计划折进业务1部的
    汇总行和分类清单。子部门扫描异常时不并入，错误照常上浮。
    """
    child = (report or {}).get("业务1部-1")
    if not isinstance(child, dict) or child.get("error"):
        return report
    parent = report.get("业务1部")
    if not isinstance(parent, dict) or parent.get("error"):
        return report
    parent["plans"] = list(parent.get("plans") or []) + list(child.get("plans") or [])
    for key in ("total_plans", "fresh_scan_total"):
        parent[key] = int(parent.get(key) or 0) + int(child.get(key) or 0)
    del report["业务1部-1"]
    return report


def build_dept_markdown(date_str: str, dep: str, d: dict) -> str:
    """单部门播报：部门合计 + 四类计划清单。"""
    roi = f"，ROI **{d['roi']:g}**" if d.get("roi") else ""
    lines = [
        f"**【投放日报】{date_str}**",
        f"> {dep}：消耗 {d['spent']:,.2f} 元，成交 **{d['orders']}** 单{roi}"
        f"（{d.get('spent_plans', d['plans'])}/{d.get('plan_denominator', d['plans'])}条计划）",
    ]
    _append_category_sections(lines, d.get("categories") or {}, include_department=False)
    return "\n".join(lines)


def build_markdown(date_str: str, deps: dict, totals: dict) -> str:
    """汇总播报保持原样：三部门合计 + 总计。"""
    lines = [f"**【投放日报】{date_str}**"]
    for dep, d in deps.items():
        roi = f"，ROI **{d['roi']:g}**" if d.get("roi") else ""
        lines.append(
            f"> {dep}：消耗 {d['spent']:,.2f} 元，成交 **{d['orders']}** 单{roi}"
            f"（{d.get('spent_plans', d['plans'])}/{d.get('plan_denominator', d['plans'])}条计划）"
        )
    total_roi = f"，综合 ROI {totals['roi']:g}" if totals.get("roi") else ""
    lines.append(
        f"**合计：消耗 {totals['spent']:,.2f} 元，成交 {totals['orders']} 单{total_roi}**"
    )
    return "\n".join(lines)


async def make_report(time_range: str | None = None, title_suffix: str = "",
                     scan_slot: str | None = None,
                     include_started_today: bool | None = None) -> dict:
    """扫描汇总；当天各档位均保留本日扫描过的计划。"""
    report = await scan_all_plans(department="all", time_range=time_range)
    # 千川总览偶发单部门列表未加载完成（返回空数组或直接超时报错）。对异常部门
    # 单独复扫一次，避免把临时异常直接当作零数据推送或整报失败（2026-08-24 起
    # 复扫范围从「仅空部门」扩到「报错部门也复扫」）。
    has_healthy_department = any(
        isinstance(rec, dict) and not rec.get("error")
        for rec in report.values()
    )
    if has_healthy_department:
        for dep, rec in list(report.items()):
            if not isinstance(rec, dict):
                continue
            needs_retry = bool(rec.get("error")) or not rec.get("plans")
            if not needs_retry:
                continue
            retry_report = await scan_all_plans(department=dep, time_range=time_range)
            retry_rec = retry_report.get(dep)
            if isinstance(retry_rec, dict) and not retry_rec.get("error"):
                report[dep] = retry_rec
    failed = [dep for dep, rec in report.items() if isinstance(rec, dict) and rec.get("error")]
    if failed:
        raise RuntimeError(f"扫描失败部门: {'/'.join(failed)}")
    day = datetime.date.today()
    if time_range == "昨天":
        day -= datetime.timedelta(days=1)
    normalized_slot = _report_history_slot(title_suffix, scan_slot)
    # 先做指标回退保护；随后写入已校正的快照，供后续档位继续使用。
    # Every scheduled report becomes historical evidence for a later night run.
    _snapshot_report_for_history(report, scan_slot=normalized_slot, label_date=day.isoformat())
    if include_started_today is None:
        # 08:00—17:00 明细只播当前仍处于投放且有消耗的计划；17:00 不把历史暂停计划
        # 算进分子。22:30 才合并本日早前扫描过、后来暂停/熔断/关闭的计划，作为全量分母。
        # 23:00不是对外日报，但保留全量口径供收口和历史重建使用。
        if time_range == "昨天":
            include_started_today = False
        elif normalized_slot in CURRENT_ONLY_REPORT_SLOTS:
            include_started_today = False
        else:
            include_started_today = True
    history_added = 0
    if include_started_today and time_range != "昨天":
        report, history_added = _merge_started_today_history(
            report, label_date=day.isoformat()
        )
    # 矩阵号（业务1部-1）播报并入业务1部（2026-09-03 用户定）；标签库/历史
    # 留痕已在上面按真实账号归属写好，这里只折展示层。
    report = _merge_matrix_department(report)
    deps, totals = aggregate(
        report, include_started_today=bool(include_started_today), scan_slot=normalized_slot
    )
    # 动态时刻（2026-08-31 用户拍板）：标题「截至 HH:MM」标实际扫描完成时刻，
    # 扫描重试/延误时名实相符。注意必须在上方 _report_history_slot 之后改写，
    # 否则历史归槽会按实际时刻匹配不到固定档位。
    display_suffix = re.sub(
        r"截至\s*\d{1,2}:\d{2}",
        f"截至 {datetime.datetime.now().strftime('%H:%M')}",
        title_suffix or "",
    )
    date_str = day.strftime("%m月%d日") + display_suffix
    # 助推富化已停用：全域产品没有曝光/点击漏斗指标（管理表与数据V2报表
    # 目录均核实不含），助推子集数据无业务含义，且富化要占用浏览器与
    # 轮询/启停任务争抢。engagement_metrics 模块保留供历史报告重建使用。
    engagement = {"skipped": "全域产品无曝光/点击指标，助推富化已停用（2026-08-21）"}
    return {
        "success": True,
        "date": day.isoformat(),
        "time_range": time_range or "今天",
        "scan_slot": normalized_slot,
        "include_started_today": bool(include_started_today),
        "history_added": history_added,
        "departments": deps,
        "totals": totals,
        "engagement": engagement,
        "markdown": build_markdown(date_str, deps, totals),
        "markdown_by_dept": {
            dep: build_dept_markdown(date_str, dep, d) for dep, d in deps.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="每日投放数据汇总")
    ap.add_argument("--time-range", default=None,
                    help='总览页时间快捷选项原文，如 "昨天"；默认今天')
    ap.add_argument("--suffix", default="", help='标题日期后追加的文字，如 "（截至 12:00）"')
    ap.add_argument("--scan-slot", default=None, help="历史留痕档位，如 08:00/10:00/12:00/14:00/16:00/17:00/22:30")
    ap.add_argument("--include-started-today", action="store_true", help="纳入今日已扫描/投放后暂停的计划")
    args = ap.parse_args()
    try:
        result = asyncio.run(make_report(
            args.time_range, args.suffix, args.scan_slot,
            True if args.include_started_today else None,
        ))
        rc = 0
    except Exception as exc:
        result = {"success": False, "message": str(exc)[:300]}
        rc = 1
    print(json.dumps(result, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
