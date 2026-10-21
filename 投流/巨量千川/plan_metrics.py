"""Extract ROI and spent metrics from Qianchuan plans via CDP.

Two query modes:
  1.  Plan-level  — search the overall plan table by 抖音号（达人名称）and read ROI/spent.
  2.  Task-level  — read the task center for a specific chase task's metrics (portable from
                   the chase-workflow project).

Dependency: pip install "playwright>=1.60,<2"
Usage:
    python plan_metrics.py plan --creator 达人名称             # plan-level, by creator name
    python plan_metrics.py plan --plan-name "计划名称"         # plan-level, by exact plan name
    python plan_metrics.py task --main-plan-id <id> --task-id <id>  # task-level
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

from .session_prepare import (
    ACCOUNT_BY_DEPARTMENT,
    _account_id,
    connect_browser,
    ensure_qianchuan_page,
    force_plans_tab,
    open_fresh_page,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CDP_URL = "http://127.0.0.1:9223"
OVERALL_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
DEFAULT_DEPARTMENT = "业务3部"
TOTAL_TIMEOUT = 20.0

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PlanMetrics:
    """Metrics for a single main plan on the overall plan page."""

    plan_name: str
    status: str
    spent: float
    roi: float | None
    roi_target: float | None
    budget: float
    delivery_type: str = ""
    creator_name: str | None = None
    plan_id: str | None = None
    message: str = ""
    orders: int | None = None
    # None means the platform did not return this metric. It must never be
    # rendered as a real zero in a management report.
    impression_count: int | None = None
    click_count: int | None = None
    net_deal_amount: float | None = None  # 净成交金额，仅供人工核对，不参与达标判定
    pause_reason: str | None = None
    pause_category: str | None = None


@dataclass
class TaskMetrics:
    """Metrics for a single material-chase task."""

    task_id: str
    main_plan_id: str
    plan_name: str
    status: str
    budget: float
    duration_hours: float
    spent: float
    roi: float | None
    control_cost: float | None
    impressions: int = 0
    clicks: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _optional_number(text: str) -> float | None:
    """Parse a number from table cell text; return None for empty/dash entries.

    Handles multi-line values like "24.0\\n预计 07-17 14:49 结束" → 24.0.
    """
    # Take the first line only (before newline)
    first_line = text.split("\n")[0].strip().replace(",", "")
    if first_line in {"", "-", "--", "暂无数据"}:
        return None
    try:
        return float(first_line.replace("%", ""))
    except ValueError:
        return None


def _pause_details(status_text: str) -> tuple[str | None, str | None]:
    """Preserve why a paused plan stopped instead of treating all pauses alike."""
    text = status_text or ""
    if "暂停" not in text:
        return None, None
    if any(token in text for token in ("预算不足", "预算耗尽", "预算已用完")):
        return "预算不足", "budget_exhausted"
    if "手动暂停" in text:
        return "手动暂停", "manual_pause"
    if "系统暂停" in text:
        return "系统暂停", "system_pause"
    return "暂停原因未返回", "unknown_pause"


def _norm_label(text: str) -> str:
    """Collapse whitespace so header labels compare reliably."""
    return re.sub(r"\s+", "", text or "")


def _find_col(labels: list[str], *keywords: str) -> int | None:
    """Find a column index by header text; prefix match first, then contains."""
    for kw in keywords:
        for i, lab in enumerate(labels):
            if lab.startswith(kw):
                return i
    for kw in keywords:
        for i, lab in enumerate(labels):
            if kw in lab:
                return i
    return None


def _col_index(labels: list[str] | None, *keywords: str, fallback: int) -> int:
    """Resolve a column index from header labels, else use the legacy index."""
    if labels:
        idx = _find_col(labels, *keywords)
        if idx is not None:
            return idx
    return fallback


def _labels_usable(labels: list[str] | None) -> bool:
    """表头可用性：非空且含指标列（消耗/ROI）。对齐成功但读错表格的表头
    （如固定列子表头）不含指标列，同样视为不可用以便重试。"""
    if not labels:
        return False
    return any(("消耗" in lab or "ROI" in lab) for lab in labels)


async def _read_column_labels_once(tr, cell_count: int) -> list[str] | None:
    """单次读取表头标签（原 _column_labels 逻辑）。

    Reads the thead of the row's own table first; ovui tables with fixed
    columns may split the header into several tables, so fall back to merging
    every visible header cell on the page ordered by x position (deduping the
    fixed-column duplicates that share the same x).
    """
    ths = tr.locator("xpath=ancestor::table[1]").locator("thead th")
    labels = [_norm_label(await ths.nth(i).inner_text()) for i in range(await ths.count())]
    if len(labels) == cell_count and any(labels):
        return labels

    all_ths = tr.page.locator("thead th")
    th_count = await all_ths.count()
    # 新版千川固定列会重复表头，元素过多时直接降级到 legacy 索引
    if th_count > 100:
        return None
    items = []
    for i in range(th_count):
        th = all_ths.nth(i)
        try:
            if not await th.is_visible():
                continue
            box = await th.bounding_box()
        except Exception:
            continue
        if box:
            items.append((box["x"], _norm_label(await th.inner_text())))
    items.sort(key=lambda it: it[0])
    merged: list[str] = []
    last_x: float | None = None
    for x, label in items:
        if last_x is not None and abs(x - last_x) < 2:
            continue  # fixed-column duplicate at the same position
        merged.append(label)
        last_x = x
    if len(merged) == cell_count and any(merged):
        return merged
    return None


async def _column_labels(tr, cell_count: int, retries: int = 2) -> list[str] | None:
    """Return header labels aligned with the row's cell order, or None.

    2026-09-01：表格半渲染（虚拟滚动/固定列重排）时表头短暂读不到是常态，
    读不到或读出的表头不含指标列时等待重试；重试耗尽才返回 None，
    由行级校验（_malformed_reason）兜底拒收脏行。
    """
    for attempt in range(retries + 1):
        labels = await _read_column_labels_once(tr, cell_count)
        if _labels_usable(labels):
            return labels
        if attempt < retries:
            await tr.page.wait_for_timeout(700)
    return None


def _ensure_cdp() -> None:
    """确保 Edge CDP 可用：未起/慢启动时等待并拉起。

    原来是 1s 探活、失败直接报错 —— 浏览器刚重启恢复会话时探活必挂，
    批量误伤（2026-08-17）。改走 session_prepare 的确保逻辑（探活 → 杀拉起 → 等就绪）。
    """
    import time as _time

    from .session_prepare import _ensure_cdp as _sp_ensure_cdp

    _sp_ensure_cdp(_time.monotonic() + 45.0)


# ---------------------------------------------------------------------------
# Task-level: read chase task metrics from task center
# ---------------------------------------------------------------------------


async def _open_overall(page, department: str = "业务3部"):
    """Ensure page is on the overall plan page with search input available.
    返回实际可用的 Page（僵尸标签可能被换新）。"""
    # 83+ 条计划的总览页实时刷新，20s 加载窗口实测不够（2026-08-17）
    page = await ensure_qianchuan_page(page, department, timeout=45.0)
    await force_plans_tab(page)
    return page


async def _locate_task_row(page, main_plan_id: str, task_id: str):
    """Open task center and scroll to expose all columns, return the task row."""
    # Find search input
    search = None
    for sel in ['input[placeholder*="计划名称"]', 'input[placeholder*="抖音号"]']:
        el = page.locator(sel)
        if await el.count() and await el.first.is_visible():
            search = el.first
            break
    if not search:
        raise RuntimeError("No search input found for locating main plan")
    await search.fill(main_plan_id)
    await search.press("Enter")

    # Wait for the row to appear
    row = None
    for _ in range(30):
        rows = page.locator("tr", has_text=main_plan_id)
        for i in range(await rows.count()):
            if await rows.nth(i).is_visible():
                row = rows.nth(i)
                break
        if row:
            break
        await page.wait_for_timeout(500)
    if not row:
        raise RuntimeError(f"Main plan not found: {main_plan_id}")

    # Click the "素材追投" data icon to open the task center
    task_btn = row.locator('iconpark-icon[name="oc-chart-histogram"]')
    for _ in range(20):
        if await task_btn.count() and await task_btn.first.is_visible():
            await task_btn.first.evaluate("el => el.click()")
            break
        await row.hover()
        await page.wait_for_timeout(300)
    else:
        raise RuntimeError(f"Task-center data icon not found for plan {main_plan_id}")

    # Wait for task center
    task_table = page.locator(".qc-table.task-center-table .ovui-table__body-wrapper")
    for _ in range(40):
        if "uni_task_center" in page.url and await task_table.count():
            return await _find_task_row(page, task_id, task_table)
        await page.wait_for_timeout(500)
    raise RuntimeError("Task center did not open")


async def _find_task_row(page, task_id: str, scroll_area):
    """Scroll right to expose all columns, then find the task row."""
    await scroll_area.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    await page.wait_for_timeout(300)

    for _ in range(40):
        rows = page.locator("tr", has_text=f"ID: {task_id}")
        for i in range(await rows.count()):
            if await rows.nth(i).is_visible():
                return rows.nth(i)
        await page.wait_for_timeout(500)
    raise RuntimeError(f"Task row not found: {task_id}")


async def read_task_metrics(main_plan_id: str, task_id: str, department: str = DEFAULT_DEPARTMENT) -> TaskMetrics:
    """Connect to the current CDP session and read chase task metrics.

    Metric columns (control_cost / roi / spent) are located by header text;
    only the structural status/budget columns use fixed indices.
    """
    _ensure_cdp()
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 单条读取优先复用现有千川页，避免轮询和计划操作不断积累标签。仅在没有
        # 千川页时才新开临时页，并且只关闭自己创建的那一页。
        page = next((p for p in context.pages if "qianchuan.jinritemai.com" in p.url), None)
        owned_page = page is None
        if page is None:
            page = await open_fresh_page(context)

        scroll_areas = page.locator(".qc-table.task-center-table .ovui-table__body-wrapper")
        if await scroll_areas.count() == 0 or not await scroll_areas.first.is_visible():
            # Not on task center page — navigate through overall
            page = await _open_overall(page, department)
            await _locate_task_row(page, main_plan_id, task_id)
        else:
            # Already on task center — find visible scroll area and task row
            sa = None
            for i in range(await scroll_areas.count()):
                if await scroll_areas.nth(i).is_visible():
                    sa = scroll_areas.nth(i)
                    break
            if not sa:
                raise RuntimeError("No visible task table scroll area")
            await _find_task_row(page, task_id, sa)

        # Re-read the row now that we're positioned
        return await _parse_task_row(page, task_id)


async def _parse_task_row(page, task_id: str) -> TaskMetrics:
    """Parse the task row cells into a TaskMetrics dataclass.

    Metric columns (综合成本 / ROI / 消耗) are located by header text — the
    task-center table layout shifts over time just like the plan table.
    Structural columns (status / budget) keep fixed indices.
    """
    row = await _find_task_row(
        page, task_id,
        page.locator(".qc-table.task-center-table .ovui-table__body-wrapper").first,
    )
    cells = row.locator("td")
    cc = await cells.count()
    if cc < 18:
        raise RuntimeError(f"Task row has {cc} cells, expected >= 18")

    texts = [(await cells.nth(j).inner_text()).strip() for j in range(cc)]
    labels = await _column_labels(row, cc)
    if labels is None:
        print(
            "WARNING: task table header unavailable, falling back to legacy column indices",
            file=sys.stderr,
        )

    cost_idx = _col_index(labels, "综合成本", fallback=6)
    roi_idx = _col_index(labels, "综合营销ROI", "综合ROI", fallback=7)
    spent_idx = _col_index(labels, "整体消耗", "消耗", fallback=15)

    return TaskMetrics(
        task_id=task_id,
        main_plan_id="",
        status=texts[2],
        budget=_optional_number(texts[3]) or 0.0,
        control_cost=_optional_number(texts[cost_idx]),
        roi=_optional_number(texts[roi_idx]),
        spent=_optional_number(texts[spent_idx]) or 0.0,
    )


# ---------------------------------------------------------------------------
# Plan-level: read metrics from the overall plan table
# ---------------------------------------------------------------------------


async def _switch_search_mode(page, target: str) -> None:
    """Switch the overall page search filter to match target type.

    Args:
        target: "抖音号" to search by douyin account, "计划名称" for plan name search.
    """
    # 已是目标模式直接返回（按搜索框 placeholder 判定，新旧组件通用）
    ph = "抖音号" if target == "抖音号" else "计划名称"
    if await page.locator(f'input[placeholder*="{ph}"]').count():
        return

    # 旧版 oc 组件（2026-10 前）
    legacy = page.locator(".oc-filter-select-input-select .ovui-select__input").first
    if await legacy.count():
        await legacy.click()
        await page.wait_for_timeout(500)
        option = page.get_by_text(target, exact=True).first
        if await option.count():
            await option.click()
            await page.wait_for_timeout(500)
        return

    # 2026-10-06 实证：乘方总览迁至 aurora 组件，旧选择器静默失效导致
    # 「按抖音号搜索」永远找不到输入框（plan_sweep 匹配 0 条→误判没投过→
    # 新建撞平台重复计划拦截）。新选择器：搜索框左侧模式选择器 +
    # body 级下拉里点 exact-text 选项叶子，按 placeholder 校验切成功。
    # 2026-10-12：冷页面（浏览器重启/新开页）wrap 渲染晚于点击——
    # `if not wrap.count(): return` 静默跳过=模式没切（肉肉妈妈 sweep 实证），
    # 先等 wrap 出现再点。
    wrap = page.locator(
        '[class*="promotion-more-filter-select-input-wrap"] [class*="aurora-qc-select"]'
    ).first
    for _ in range(16):  # 最多等 8s 渲染
        try:
            if await wrap.count() and await wrap.is_visible():
                break
        except Exception:
            pass
        await page.wait_for_timeout(500)
    if not await wrap.count():
        return
    for _ in range(2):
        try:
            await wrap.click()
        except Exception:
            pass
        await page.wait_for_timeout(600)
        try:
            await page.evaluate(
                """(target) => {
                    const cands = Array.from(document.querySelectorAll(
                        '[class*="select-item-option-content"], [class*="select-item"] *'))
                        .filter(e => e.children.length === 0
                            && (e.innerText || '').trim() === target
                            && e.offsetParent !== null);
                    if (cands.length) { cands[0].click(); return true; }
                    return false;
                }""",
                target,
            )
        except Exception:
            pass
        await page.wait_for_timeout(600)
        if await page.locator(f'input[placeholder*="{ph}"]').count():
            return


def _range_matches(target: str, current: tuple[str, str] | None, today: datetime.date | None = None) -> bool | None:
    """校验读回的 (start, end) 是否符合目标快捷选项；无法预判的选项返回 None。

    千川语义（2026-08-31 实测）:「近N天/最近N天」不含今天，为 today-N ~ today-1;
    「今天」= today~today;「昨天」= today-1~today-1。
    对 N 天做容差匹配（端点允许含今天的旧定义），核心目的是抓"页面卡在错误范围"。
    """
    if current is None:
        return None
    today = today or datetime.date.today()
    fmt = "%Y-%m-%d"
    if target == "近180天含今天":
        return current == _target_dates(target, today)
    if target == "今天":
        return current == (today.strftime(fmt), today.strftime(fmt))
    if target == "昨天":
        d = (today - datetime.timedelta(days=1)).strftime(fmt)
        return current == (d, d)
    m = re.search(r"(\d+)\s*天", target)
    if m:
        n = int(m.group(1))
        valid_starts = {
            (today - datetime.timedelta(days=n)).strftime(fmt),      # 不含今天
            (today - datetime.timedelta(days=n - 1)).strftime(fmt),  # 含今天(旧定义兜底)
        }
        valid_ends = {
            (today - datetime.timedelta(days=1)).strftime(fmt),
            today.strftime(fmt),
        }
        return current[0] in valid_starts and current[1] in valid_ends
    return None


def _target_dates(target: str, today: datetime.date | None = None) -> tuple[str, str] | None:
    """目标快捷选项 → (起, 止) 日期；无法解析返回 None。语义同 _range_matches。"""
    today = today or datetime.date.today()
    fmt = "%Y-%m-%d"
    if target == "近180天含今天":
        return ((today - datetime.timedelta(days=179)).strftime(fmt), today.strftime(fmt))
    if target == "今天":
        d = today.strftime(fmt)
        return (d, d)
    if target == "昨天":
        d = (today - datetime.timedelta(days=1)).strftime(fmt)
        return (d, d)
    m = re.search(r"(\d+)\s*天", target)
    if m:
        n = int(m.group(1))
        return ((today - datetime.timedelta(days=n)).strftime(fmt),
                (today - datetime.timedelta(days=1)).strftime(fmt))
    return None


def _daterange_from_url(url: str) -> tuple[str, str] | None:
    """从总览页 URL 读 dateRange 参数（2026-09 改版后日期范围由 URL 驱动）。"""
    m = re.search(r"[?&]dateRange=(\d{4}-\d{2}-\d{2})(?:%2C|,)(\d{4}-\d{2}-\d{2})", url)
    return (m.group(1), m.group(2)) if m else None


def _with_daterange(url: str, start: str, end: str) -> str:
    """替换/追加 URL 的 dateRange 参数，其余查询参数原样保留。"""
    dr = f"dateRange={start}%2C{end}"
    if re.search(r"[?&]dateRange=\d{4}-\d{2}-\d{2}(?:%2C|,)\d{4}-\d{2}-\d{2}", url):
        return re.sub(r"dateRange=\d{4}-\d{2}-\d{2}(?:%2C|,)\d{4}-\d{2}-\d{2}", dr, url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{dr}"


async def _read_range_inputs(page) -> tuple[str, str] | None:
    """读回日期选择器输入框的起止日期；读不到时回退读 URL 的 dateRange 参数。

    2026-09-22 千川改版：旧组件 .oc-range-picker 被移除，新组件为
    .aurora-qc-picker-range；两套选择器都试，最后以 URL 参数兜底。
    """
    for selector in (".aurora-qc-picker-range input", ".oc-range-picker input"):
        try:
            vals = await page.evaluate(
                "(sel) => Array.from(document.querySelectorAll(sel))"
                ".map(e => e.value || '')", selector)
        except Exception:
            vals = None
        vals = [v for v in (vals or []) if v]
        if len(vals) >= 2:
            return (vals[0], vals[1])
    return _daterange_from_url(page.url)


async def _set_time_range(page, mode: str, retries: int = 4) -> None:
    """Switch the overall page date/time range preset.

    千川总览页日期快捷选项结构：
        .oc-range-picker-shortcut > .oc-range-picker-shortcut-item

    Args:
        mode: "today" — 今天（页面默认）; "last_7_days" — 近7天。

    2026-08-31 加固：容器未加载/点击失败/读回日期不符均重试，最终失败抛
    RuntimeError。此前只 print WARNING 就放行，页面停留在上次范围（如近7天），
    日报会把 7 天累计当当天数据推送（08-31 14pm/16pm 两档播报虚高即此导致）。
    注意：千川「近N天」实测不含今天（today-N ~ today-1），校验按此语义。
    """
    # 不依赖页面上一次残留的日期范围；即使页面当前停留在“近7天”，
    # 也必须显式切到“今天”，避免日报误读历史累计数据。
    target = "近7天" if mode == "last_7_days" else ("今天" if mode == "today" else mode)
    dates = _target_dates(target)
    if dates is None:
        raise RuntimeError(f"无法解析目标日期范围: {target}")
    last_note = "未知原因"

    # 2026-09-22 千川改版：总览页迁到 /overall-prom，日期快捷选项组件
    # （.oc-range-picker-shortcut-item）被移除，日期范围改由 URL 参数
    # dateRange=起,止 驱动——直接改 URL 导航，比摸新组件 DOM 稳定；
    # 读回校验不变（_read_range_inputs 已兼容新旧组件 + URL 兜底）。
    for _attempt in range(retries):
        # URL 已是目标范围且读回不矛盾：无需重复导航，避免无谓 reload
        if _daterange_from_url(page.url) == dates:
            if _range_matches(target, await _read_range_inputs(page)) is not False:
                await force_plans_tab(page)
                return

        # 记录切换前的表格内容，用于检测 reload 完成
        try:
            prev_text = await page.locator("table tbody").first.inner_text()
        except Exception:
            prev_text = ""

        try:
            await page.goto(_with_daterange(page.url, *dates),
                            wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            last_note = f"导航失败: {exc}"
            await page.wait_for_timeout(1500)
            continue

        # 读回校验：URL 参数与输入框起止日期均须符合目标范围语义
        await page.wait_for_timeout(1500)
        # Date navigation restores the platform's persisted managed-store tab.
        # Re-enter the plan tab before reading/searching rows; URL dates alone
        # do not prove the plan table is visible.
        await force_plans_tab(page)
        current = await _read_range_inputs(page)
        match = _range_matches(target, current)
        if match is not False and _daterange_from_url(page.url) == dates:
            await _wait_table_settled(page, prev_text)
            return
        last_note = f"读回校验不符: 目标「{target}」{dates}, 实际 {current}"
        await page.wait_for_timeout(1200)

    raise RuntimeError(f"日期范围切换失败（目标「{target}」）: {last_note}")


async def _wait_table_settled(page, prev_text: str, timeout: float = 10.0) -> None:
    """Wait for the plan table to finish reloading after a date-range switch.

    Two phases: first wait until the tbody content differs from ``prev_text``
    (reload started), then until it stays unchanged for two consecutive polls
    with non-empty content (reload finished). Falls through on timeout — the
    search retry in ``_search_plans`` covers the residual flakiness.
    """
    tbody = page.locator("table tbody").first
    deadline = time.monotonic() + timeout
    changed = False
    last = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            text = await tbody.inner_text()
        except Exception:
            text = ""
        if not changed:
            if text != prev_text:
                changed = True
        elif text == last and text.strip():
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = text
        await page.wait_for_timeout(300)


async def _search_plans(page, keyword: str, department: str = DEFAULT_DEPARTMENT, by_creator: bool = False, time_range: str | None = None) -> list:
    """Search plans on the overall page and return visible rows."""
    page = await _open_overall(page, department)
    # 2026-10-14 用户口径「记得要切换到商品」：乘方总览数据区有「直播/商品」
    # 维度切换，账号可能默认落在「直播」——不点到「商品」，计划列表/搜索框
    # 都不是我们要的商品计划。
    # 2026-10-15 实证坑：计划行的行内操作列也有「商品」链接——必须只在
    # 「直播/商品」兄弟对的容器里点，否则点到行内链接会导航进计划详情页。
    try:
        await page.evaluate("""() => {
          const vis = e => e && e.offsetParent !== null;
          const isActive = (el) => {
            let n = el;
            for (let i = 0; i < 3 && n; i++, n = n.parentElement) {
              const c = (n.className || '').toString();
              if (/active|selected|checked/.test(c)) return true;
              if (n.getAttribute && n.getAttribute('aria-selected') === 'true') return true;
            }
            return false;
          };
          const lives = [...document.querySelectorAll('*')].filter(e =>
            e.children.length === 0 && (e.innerText || '').trim() === '直播' && vis(e));
          for (const live of lives) {
            const parent = live.parentElement;
            if (!parent) continue;
            const prod = [...parent.querySelectorAll('*')].find(e =>
              e.children.length === 0 && (e.innerText || '').trim() === '商品' && vis(e));
            if (!prod) continue;
            if (isActive(prod)) return;
            prod.click();
            return;
          }
        }""")
        await page.wait_for_timeout(1200)
    except Exception:
        pass
    # 2026-10-12 一部账号实证：总览落地默认乘方推广页（无搜索框、无计划行），
    # 点「商品自选」tab 才渲染计划列表+筛选条。搜索框缺失时先补点 tab（幂等）。
    if not await page.locator('input[placeholder*="计划名称"]').count():
        try:
            tab = page.locator('[class*="tab"]:has-text("商品自选")').first
            if await tab.count() and await tab.is_visible():
                await tab.click(timeout=3000)
                await page.wait_for_timeout(3000)
        except Exception:
            pass

    # Switch time range first (before search)
    if time_range:
        await _set_time_range(page, time_range)

    # Switch search mode if needed
    mode = "抖音号" if by_creator else "计划名称"
    await _switch_search_mode(page, mode)

    # Find the search input
    search_sel = 'input[placeholder*="抖音号"]' if by_creator else 'input[placeholder*="计划名称"]'
    search = page.locator(search_sel).first
    # 2026-10-07：日期切换/URL 导航后搜索框偶发渲染延迟，立即判定会误报
    # Search input not found（pause-one 与 resume 探针多次踩到）——最多等 10s。
    # 2026-10-12：冷页面筛选条整体不渲染（实测同 URL 两次导航一次有一次无），
    # 等 20s 还不够就整页刷新重试一次（时间范围/搜索模式需重建）。
    found_input = False
    for reload_attempt in range(2):
        for _ in range(40):
            try:
                if await search.count() and await search.is_visible():
                    found_input = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
        if found_input:
            break
        if reload_attempt == 0:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            if time_range:
                await _set_time_range(page, time_range)
            await _switch_search_mode(page, mode)
            search = page.locator(search_sel).first
    if not found_input:
        raise RuntimeError(f"Search input not found (mode={mode})")

    # 日期切换后表格异步刷新可能吞掉搜索请求，0 行时重试一次
    for attempt in range(2 if time_range else 1):
        await search.fill(keyword)
        await search.press("Enter")
        await page.wait_for_timeout(800)

        rows = []
        fallback_rows = []
        for _ in range(20):
            trs = page.locator("table tbody tr")
            count = await trs.count()
            if count > 0:
                visible = []
                for i in range(count):
                    tr = trs.nth(i)
                    if await tr.is_visible():
                        visible.append(tr)
                if visible:
                    # 表格刷新期间可能还显示上一屏的残留行（别的达人/旧筛选），
                    # 只接受文本里真的包含关键词的行，否则继续等
                    fallback_rows = visible
                    rows = []
                    for tr in visible:
                        # 虚拟滚动+实时刷新下行随时被回收：inner_text 会死等
                        # 30s（2026-08-17 多次卡死），改 evaluate 立即取、被回收就跳过
                        try:
                            txt = await tr.evaluate("el => el.innerText || ''")
                        except Exception:
                            continue
                        if keyword in txt:
                            rows.append(tr)
                    if rows:
                        break
            await page.wait_for_timeout(500)
        if rows:
            return rows
        if attempt == 0 and time_range:
            print("WARNING: 切换日期后搜索无结果，重试一次", file=sys.stderr)
    if fallback_rows:
        # 按 ID 搜索时行文本可能只显示昵称不含 ID，兜底返回可见行
        print("WARNING: 未匹配到含关键词的行，返回全部可见行", file=sys.stderr)
        return fallback_rows
    return []


# 已知状态词：状态列首行包含其一才算正常，否则视为列错位/半渲染。
# 放得比较宽（投放/暂停/结束/审核/学习/不足/完成/删除/待），只拦真正的错位。
_KNOWN_STATUS_TOKENS = ("投放", "暂停", "结束", "审核", "学习", "不足", "完成", "删除", "待")


async def _row_cell_texts(tr) -> list[str]:
    """一次 evaluate 取全部单元格文本；行被虚拟滚动回收时返回空列表。

    单元格逐个 inner_text 在虚拟滚动表格上会死等（行被回收即卡 30s），
    故用一次 JS 调用取齐；返回空表示行句柄失效，由调用方重读或跳过。
    """
    try:
        return await tr.evaluate(
            "el => Array.from(el.querySelectorAll('td, th'))"
            ".map(c => (c.innerText || '').trim())")
    except Exception:
        return []


def _extract_row(texts: list[str], labels: list[str] | None) -> PlanMetrics:
    """纯函数：单元格文本 + 表头标签 → PlanMetrics。不做任何 IO，便于单测。

    Metric columns (ROI / 消耗 / 订单数 / 净成交金额) are located by header
    text, not fixed indices — 千川 reshuffles its columns over time (e.g. the
    综合营销ROI column was added after the old hard-coded layout went stale,
    which made roi read 净成交金额 instead). Fixed indices remain only as a
    last-resort fallback when the header cannot be read.

    Args:
        texts: 行内全部单元格文本（_row_cell_texts 的输出）。
        labels: Pre-computed column header labels (from _column_labels)。
                None 时指标列用 legacy 固定索引兜底。
    """

    def _first_value(text: str) -> str:
        """Take the first numeric value before any '基础' or '|' separator."""
        return text.split("基础")[0].split("|")[0].strip()

    cell_count = len(texts)

    # --- Plan name (cell[1]) — the line right before the "ID：" line ---
    # A badge line (e.g. "扶持中") may precede the actual plan name.
    plan_name_raw = texts[1] if cell_count > 1 else ""
    name_lines = [ln.strip() for ln in plan_name_raw.split("\n") if ln.strip()]
    plan_name = name_lines[0] if name_lines else ""
    for idx, line in enumerate(name_lines):
        if re.match(r"ID[：:]", line) and idx > 0:
            plan_name = name_lines[idx - 1]
            break

    # --- Creator name from plan name (multiple naming conventions) ---
    creator_name = None
    for pattern in [
        r"^\d{2,4}[-_]\d{1,2}[-_]\s*(.+?)\s*[-_]",  # "07-18_洋琪妈妈的日记_张"
        r"\d{2,4}[-_]\s*(.+?)\s*[-_]",   # "0716-我家厨房-赵" / "0727_肉肉妈妈_郑"
        r"^[\d.]+[-_]?\s*(.+?)\s*[-_]",  # "0415不点的生活良方-郑" / "6.9-青草吖-牛仔骨-丽伟"
        r"[\d.]+[-_\s]*(.+?)\s*[（(]",   # "3.25宁静（小厨娘）" → "宁静"
    ]:
        m = re.search(pattern, plan_name)
        if m:
            creator_name = m.group(1).strip()
            break

    # --- Status (cell[2]) — take only the first line ---
    raw_status = texts[2] if cell_count > 2 else ""
    status = (raw_status.split("\n")[0] if raw_status else "").strip()
    pause_reason, pause_category = _pause_details(raw_status)

    # --- Budget from settings cell[3] ---
    budget = 0.0
    roi_target = None
    if cell_count > 3:
        budget_match = re.search(r"(\d+[\d.]*)\s*元", texts[3])
        if budget_match:
            budget = float(budget_match.group(1))
    roi_target_match = re.search(r"综合营销ROI目标\s*([0-9.]+)", "\n".join(texts))
    if roi_target_match:
        roi_target = float(roi_target_match.group(1))

    # --- Metric columns located by header text (layout shifts over time) ---
    # （表头读取/重试已上移到 _parse_plan_row；本纯函数只消费传入的 labels）

    # --- Delivery type (cell[7]) ---
    delivery_type = _first_value(texts[7]) if cell_count > 7 else ""

    # --- ROI (综合营销ROI) ---
    roi_idx = _col_index(labels, "综合营销ROI", "综合ROI", fallback=9)
    roi = _optional_number(_first_value(texts[roi_idx])) if cell_count > roi_idx else None

    # --- Spent / 整体消耗 ---
    spent_idx = _col_index(labels, "整体消耗", "消耗", fallback=13)
    spent = _optional_number(_first_value(texts[spent_idx])) if cell_count > spent_idx else 0.0

    # --- Plan ID from plan info cell ---
    plan_id = None
    id_match = re.search(r"ID[：:]?\s*(\d+)", texts[1] if cell_count > 1 else "")
    if id_match:
        plan_id = id_match.group(1)

    # --- Orders（净成交订单数）---
    orders_idx = _col_index(labels, "整体成交订单数", "净成交订单数", fallback=14)
    orders: int | None = None
    if cell_count > orders_idx:
        orders = int(_optional_number(texts[orders_idx]) or 0) if texts[orders_idx].strip() not in {"", "-", "--"} else None

    # --- 净成交金额（仅供人工核对）---
    net_idx = _find_col(labels, "净成交金额") if labels else None
    net_deal_amount = (
        _optional_number(_first_value(texts[net_idx]))
        if net_idx is not None and cell_count > net_idx
        else None
    )

    # --- Exposure / Clicks ---
    # 重点计划与潜力计划的播报需要曝光、点击和转化，不再把两项固定写成 0。
    impression_idx = _col_index(labels, "展示次数", "曝光次数", "曝光", fallback=-1)
    click_idx = _col_index(labels, "点击次数", "点击", fallback=-1)
    impression_count = (
        int(_optional_number(_first_value(texts[impression_idx])) or 0)
        if 0 <= impression_idx < cell_count else None
    )
    click_count = (
        int(_optional_number(_first_value(texts[click_idx])) or 0)
        if 0 <= click_idx < cell_count else None
    )

    return PlanMetrics(
        plan_name=plan_name,
        status=status,
        spent=spent or 0.0,
        roi=roi,
        roi_target=roi_target,
        budget=budget or 0.0,
        delivery_type=delivery_type,
        creator_name=creator_name,
        plan_id=plan_id,
        orders=orders,
        impression_count=impression_count,
        click_count=click_count,
        net_deal_amount=net_deal_amount,
        pause_reason=pause_reason,
        pause_category=pause_category,
    )


def _malformed_reason(texts: list[str], metrics: PlanMetrics) -> str | None:
    """畸形行判定（2026-09-01 吞列事故根治）；命中由 _parse_plan_row 重读/跳过。

    千川虚拟滚动 + 固定列重渲染时，行会短暂处于半渲染态：名称单元格丢失换行
    （「名称 ID：xxx1商品1抖音号素材」挤成一行）、ID 数字粘连后续字段（16 位
    真 ID 尾部粘上「1商品」的 1）、甚至整行被回收。此时产出的行数据必脏，
    绝不能进快照/标签库（08-31 夜间档被幻影计划虚增 1,088.75 元即此因）。
    宁可漏一行（下游用同日早前快照补齐），不可错一行。
    """
    if len(texts) < 8:
        return f"单元格仅 {len(texts)} 个（行被回收或落入固定列子表）"
    if not metrics.plan_name:
        return "计划名为空"
    if re.search(r"ID[：:]\s*\d{4,}", metrics.plan_name):
        return "计划名吞并 ID 列"
    if not metrics.plan_id:
        return "未解析到计划 ID"
    if not re.fullmatch(r"\d{10,20}", metrics.plan_id):
        return f"计划 ID 异常[{metrics.plan_id}]"
    # ID 所在行数字后紧跟其他字段（商品/抖音号/素材或更多数字）= 列粘连。
    # 只认这批已知粘连特征，避免误伤 ID 行合法的尾随内容。
    # 注意不能用 \\d+([^\\n]+) 一个正则搞定：\\d+ 回溯会让出末位数字给
    # 捕获组，干净行也会被误判。先取整行再锚定替换掉「ID：数字」前缀。
    id_line = re.search(r"ID[：:][^\n]*", texts[1] if len(texts) > 1 else "")
    if id_line:
        tail = re.sub(r"^ID[：:]\s*\d+", "", id_line.group(0)).strip()
        if tail and re.match(r"(\d|商品|抖音|素材)", tail):
            return "ID 列粘连后续字段"
    if not metrics.status or not any(
            tok in metrics.status for tok in _KNOWN_STATUS_TOKENS):
        return f"状态列异常[{metrics.status[:12]}]"
    return None


async def _parse_plan_row(
    tr, labels: list[str] | None = None, *, retries: int = 2
) -> PlanMetrics | None:
    """Extract metrics from a single plan table row; 返回 None = 畸形行已跳过。

    Args:
        tr: Playwright locator for the <tr> element.
        labels: Pre-computed column header labels (from _column_labels).
                When provided, avoids redundant per-row CDP round-trips.
                为 None 时本函数自行读取（含重试），仍读不到才走 legacy
                固定列索引，并在 stderr 留 WARNING。
        retries: 行半渲染/读取失败时的重读次数（半渲染通常 1s 内自愈）。
    """
    last_reason = "行单元格读取失败（虚拟滚动回收）"
    snippet = ""
    for attempt in range(retries + 1):
        texts = await _row_cell_texts(tr)
        if texts:
            if labels is None:
                labels = await _column_labels(tr, len(texts))
            metrics = _extract_row(texts, labels)
            last_reason = _malformed_reason(texts, metrics)
            snippet = (texts[1] if len(texts) > 1 else "|".join(texts))[:40]
            if last_reason is None:
                if labels is None:
                    print(
                        "WARNING: plan table header unavailable, falling back "
                        "to legacy column indices",
                        file=sys.stderr,
                    )
                return metrics
        if attempt < retries:
            await tr.page.wait_for_timeout(600 + 300 * attempt)
    print(f"WARNING: 跳过畸形计划行（{last_reason}）: {snippet}", file=sys.stderr)
    return None


async def search_plan_metrics(
    creator_name: str | None = None,
    plan_name: str | None = None,
    department: str = DEFAULT_DEPARTMENT,
    time_range: str | None = None,
) -> list[PlanMetrics]:
    """Search plans by creator name or exact plan name on the overall page.

    Args:
        creator_name: 抖音号达人名称, matched against plan name pattern "MMDD-达人名称-赵"
        plan_name: Exact plan name or ID to search
        time_range: 日期范围预设 — None(保持页面默认=今天), "last_7_days"(近7天), "today"

    Returns:
        List of matching plan metrics (empty list if none found).
    """
    _ensure_cdp()

    keyword = plan_name or creator_name or ""
    if not keyword:
        raise ValueError("Either creator_name or plan_name is required")

    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 单条读取优先复用现有千川页，避免轮询和计划操作不断积累标签。仅在没有
        # 千川页时才新开临时页，并且只关闭自己创建的那一页。
        page = next((p for p in context.pages if "qianchuan.jinritemai.com" in p.url), None)
        owned_page = page is None
        if page is None:
            page = await open_fresh_page(context)

        rows = await _search_plans(page, keyword, department, by_creator=(creator_name is not None), time_range=time_range)

        # 预计算列标签（同一搜索结果页所有行共享表头）
        labels = None
        if rows:
            try:
                labels = await _column_labels(rows[0], await rows[0].locator("td, th").count())
            except Exception:
                pass

        results = []
        for tr in rows:
            metrics = await _parse_plan_row(tr, labels)
            if metrics is None:  # 畸形行（半渲染/吞列）已留 WARNING，跳过
                continue
            results.append(metrics)

        if owned_page:
            try:
                await page.close()
            except Exception:
                pass
        return results


# ---------------------------------------------------------------------------
# Plan-level: list all plans on the overall page (no keyword filter)
# ---------------------------------------------------------------------------


async def _read_pagination_total(page) -> Optional[int]:
    """Read '共 N 条记录' from the table pagination; None if not found."""
    pager = page.locator(".oc-table-pagination").first
    if not await pager.count():
        return None
    text = await pager.inner_text()
    match = re.search(r"共\s*(\d+)\s*条", text or "")
    return int(match.group(1)) if match else None


async def _goto_next_page(page) -> bool:
    """Click the pagination next button; False when absent or disabled."""
    btn = page.locator(".ovui-pagination-next, li[title='下一页']").first
    if not await btn.count():
        return False
    cls = await btn.get_attribute("class") or ""
    if "disabled" in cls:
        return False
    await btn.click()
    await page.wait_for_timeout(800)
    return True


def _department_overview_page(pages, account_id: str):
    """返回同账号的新版/旧版总览页，避免每次扫描都另开标签。

    千川 2026-09 从 /uni-prom/overall 迁到 /overall-prom；两种路径并存，
    所以复用判断必须兼容，否则每个部门每轮都泄露一个自动化标签。
    """
    return next(
        (p for p in pages
         if ("/uni-prom/overall" in p.url or "/overall-prom" in p.url)
         and f"aavid={account_id}" in p.url),
        None,
    )


async def _wait_plan_table_ready(page, timeout_s: float = 15.0) -> bool:
    """等总览页计划表格渲染完成：可见数据行 / 空态「暂无数据」/ 分页器共0条 任一出现。

    2026-09-08 业务1部-1 静默空扫根因：ensure_qianchuan_page 的 aavid 兜底只保证
    「落在正确账号上下文」（09-02 为零计划账号授权流设计），不保证总览表格已渲染
    ——矩阵账号导航后可能停在乘方营销首页，无表格无分页器，旧逻辑 30s 等不到行
    就静默返回 []，17:10/23:00 暂停链因此漏掉整个部门（无任何日志）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            trs = page.locator("table tbody tr")
            for i in range(await trs.count()):
                if await trs.nth(i).is_visible():
                    return True
            empties = page.locator('[class*="empty"]')
            for i in range(await empties.count()):
                el = empties.nth(i)
                if await el.is_visible():
                    text = await el.inner_text() or ""
                    if "暂无" in text or "没有" in text:
                        return True
            if await _read_pagination_total(page) == 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False


async def _visible_plan_rows(page):
    """在浏览器内筛选可见行，避免每行一次 is_visible 往返。"""
    rows = page.locator("table tbody tr:visible")
    return [rows.nth(i) for i in range(await rows.count())]


async def list_all_plans(
    department: str = DEFAULT_DEPARTMENT,
    progress_cb=None,  # Callable[[int, int], None] — (已读计划数, 分页总条数)，每页上报
    time_range: str | None = None,  # None=今天(页面默认)；快捷选项原文，如 "近7天"/"近30天"
) -> list[PlanMetrics]:
    """List every plan on the overall page (no keyword filter), across all pages.

    Clears any residual search keyword, reads the pagination total (共 N 条记录),
    clicks through pages until all rows are collected, and deduplicates by plan_id.
    """
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 日常扫描复用每个业务部已有的总览页，避免轮询不断堆积标签。
        account_id = _account_id(ACCOUNT_BY_DEPARTMENT[department])
        page = _department_overview_page(context.pages, account_id) or await open_fresh_page(context)

        page = await _open_overall(page, department)

        # 2026-09-08：aavid 兜底不代表表格就绪（营销首页无表）。等到表格行/空态/
        # 分页器出现；不就绪整页重载一次；仍不就绪抛错——由调用方记「扫描失败」，
        # 绝不静默返回 []（业务1部-1 连停两天 17:10/23:00 都因此漏停）。
        if not await _wait_plan_table_ready(page):
            log.warning("[%s] 总览表格未渲染，整页重载重试", department)
            try:
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                # 乘方账号整页重载后 SPA 又把子 Tab 拨回素材Tab——重载后必须重新软切
                await force_plans_tab(page)
            except Exception:
                pass
            if not await _wait_plan_table_ready(page):
                raise RuntimeError("总览表格未渲染（可能落在营销首页/账号未切换）")

        if time_range:
            await _set_time_range(page, time_range)
            await page.wait_for_timeout(1500)

        # Clear any residual search keyword so the full table is listed
        for sel in ('input[placeholder*="计划名称"]', 'input[placeholder*="抖音号"]'):
            search = page.locator(sel).first
            if await search.count() and await search.is_visible():
                if (await search.input_value()).strip():
                    await search.fill("")
                    await search.press("Enter")
                    await page.wait_for_timeout(800)
                break

        results: list[PlanMetrics] = []
        seen: set[str] = set()

        for _ in range(50):  # page-turn safety bound
            # Wait until the table has at least one visible row (or give up empty)
            rows = []
            # 2026-08-24：连续扫描时后扫部门（90+ 条计划）表格渲染常超 10s，
            # 原 20×500ms 等待会把「仍在加载」误判成「空部门」→ 放宽到 30s。
            for _ in range(60):
                visible = await _visible_plan_rows(page)
                if visible:
                    rows = visible
                    break
                await page.wait_for_timeout(500)

            if not rows:
                total = await _read_pagination_total(page)
                if total is None or len(seen) >= total:
                    break
                if not await _goto_next_page(page):
                    break
                continue

            # 预计算列标签（同一页所有行共享表头，避免每行重复 CDP 往返）
            labels = None
            try:
                labels = await _column_labels(rows[0], await rows[0].locator("td, th").count())
            except Exception:
                pass

            for tr in rows:
                try:
                    metrics = await _parse_plan_row(tr, labels)
                except Exception:
                    continue
                if metrics is None:  # 畸形行已留 WARNING，跳过
                    continue
                key = metrics.plan_id or metrics.plan_name
                if key and key not in seen:
                    seen.add(key)
                    results.append(metrics)

            total = await _read_pagination_total(page)
            if progress_cb and total:
                try:
                    progress_cb(len(results), total)
                except Exception:
                    pass
            if total is None or len(seen) >= total or not rows:
                break
            if not await _goto_next_page(page):
                break

        return results


# ---------------------------------------------------------------------------
# Chase task-level: open task center for a plan row and read task metrics
# ---------------------------------------------------------------------------


async def _open_chase_task_center(page, plan_row) -> None:
    """Open the chase task center for a given plan row by clicking the chart icon."""
    # 先把行/列滚进可视区域，避免 hover 因 sticky 列被挤出视口而超时
    await plan_row.evaluate(
        """(row) => {
            row.scrollIntoView({ block: 'center', inline: 'center' });
            const table = row.closest('table');
            const wrap = table && table.closest('.ovui-table__body-wrapper');
            if (wrap) wrap.scrollLeft = wrap.scrollWidth;
        }"""
    )
    await page.wait_for_timeout(800)

    # 用 JS 直接点击「素材追投」格子里面的图表图标
    clicked = await plan_row.evaluate(
        """(row) => {
            const tasks = row.querySelectorAll('.assist-task');
            for (const t of tasks) {
                if (t.innerText.includes('素材追投')) {
                    const icon = t.querySelector('iconpark-icon[name="oc-chart-histogram"]');
                    if (icon) { icon.click(); return 'ok'; }
                    return 'no_chart_icon';
                }
            }
            return 'no_cell';
        }"""
    )
    if clicked != "ok":
        chart_icon = plan_row.locator('iconpark-icon[name="oc-chart-histogram"]')
        if await chart_icon.count():
            await chart_icon.first.evaluate("el => el.click()")
        else:
            raise RuntimeError("Task-center data icon not found for plan row")
    await page.wait_for_timeout(1200)

    # Click "查看数据" popover if it appears
    view_data = page.get_by_text("查看数据", exact=True).first
    if await view_data.count() and await view_data.is_visible():
        await view_data.click(force=True)
        await page.wait_for_timeout(2000)


async def _read_chase_tasks(page) -> list[TaskMetrics]:
    """Read chase task rows from the task center table.

    Metric columns are located by header text (layout shifts over time);
    structural columns keep fixed indices:
        [0]=task-name(ID), [1]=material, [2]=status, [3]=budget, [4]=duration
    """
    task_table = page.locator(".qc-table.task-center-table")
    if await task_table.count() == 0:
        return []

    # Scroll right to expose all columns
    scroll_area = page.locator(".qc-table.task-center-table .ovui-table__body-wrapper").first
    if await scroll_area.count():
        await scroll_area.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
        await page.wait_for_timeout(500)

    task_rows = task_table.locator("tbody tr")
    count = await task_rows.count()
    results = []
    labels_cache: dict[int, list[str] | None] = {}

    for i in range(count):
        row = task_rows.nth(i)
        cells = row.locator("td")
        cc = await cells.count()
        if cc < 17:
            continue

        if cc not in labels_cache:
            labels_cache[cc] = await _column_labels(row, cc)
        labels = labels_cache[cc]

        texts = []
        for j in range(cc):
            texts.append((await cells.nth(j).inner_text()).strip())

        cost_idx = _col_index(labels, "综合成本", fallback=6)
        roi_idx = _col_index(labels, "综合营销ROI", "综合ROI", fallback=7)
        spent_idx = _col_index(labels, "整体消耗", "消耗", fallback=15)
        imp_idx = _col_index(labels, "展示次数", fallback=11)
        click_idx = _col_index(labels, "点击次数", fallback=12)

        # Parse task name and ID from cell[0]
        task_name_full = texts[0]
        task_id = ""
        id_match = re.search(r"ID[：:]?\s*(\d+)", task_name_full)
        if id_match:
            task_id = id_match.group(1)

        # Parse main plan ID from URL
        from urllib.parse import parse_qs, urlparse
        url_params = parse_qs(urlparse(page.url).query)
        main_plan_id = url_params.get("adId", [""])[0]

        results.append(TaskMetrics(
            task_id=task_id,
            main_plan_id=main_plan_id,
            plan_name=task_name_full.split("\n")[0].strip(),
            status=texts[2] if cc > 2 else "",
            budget=_optional_number(texts[3]) or 0.0,
            duration_hours=_optional_number(texts[4]) or 0.0,
            control_cost=_optional_number(texts[cost_idx]) if cc > cost_idx else None,
            roi=_optional_number(texts[roi_idx]) if cc > roi_idx else None,
            spent=_optional_number(texts[spent_idx]) if cc > spent_idx else 0.0,
            impressions=int(_optional_number(texts[imp_idx]) or 0) if cc > imp_idx else 0,
            clicks=int(_optional_number(texts[click_idx]) or 0) if cc > click_idx else 0,
        ))

    return results


async def search_plan_with_tasks(
    creator_name: str,
    department: str = DEFAULT_DEPARTMENT,
) -> dict:
    """Search plans by 抖音号 and read both plan-level and chase task metrics.

    Returns:
        Dict with "plan" (PlanMetrics) and "tasks" (list of TaskMetrics).
    """
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 复用旧标签可能撞僵尸页（渲染半死时 count/evaluate 永不返回、进程僵死），一律开新页
        page = await open_fresh_page(context)

        # Search by 抖音号
        rows = await _search_plans(page, creator_name, department, by_creator=True)
        if not rows:
            return {"plan": None, "tasks": []}

        plan_metrics = await _parse_plan_row(rows[0])
        # 畸形行时 plan_metrics 为 None（已留 WARNING），计划指标留空但追投任务照读

        # Open chase task center for the first plan
        await _open_chase_task_center(page, rows[0])
        tasks = await _read_chase_tasks(page)

async def read_chase_by_creator(
    creator_name: str,
    department: str = DEFAULT_DEPARTMENT,
) -> list[TaskMetrics]:
    """Search 抖音号, open chase task center, and return chase task metrics only."""
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 复用旧标签可能撞僵尸页（渲染半死时 count/evaluate 永不返回、进程僵死），一律开新页
        page = await open_fresh_page(context)

        rows = await _search_plans(page, creator_name, department, by_creator=True)
        if not rows:
            return []

        await _open_chase_task_center(page, rows[0])
        return await _read_chase_tasks(page)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract ROI/spent metrics from Qianchuan.")
    sub = parser.add_subparsers(dest="mode", required=True)

    # plan mode
    plan_p = sub.add_parser("plan", help="Search plans by creator/name and read metrics")
    plan_p.add_argument("--creator", help="抖音号达人名称")
    plan_p.add_argument("--plan-name", help="Exact plan name to search")
    plan_p.add_argument("--department", default=DEFAULT_DEPARTMENT, choices=tuple(ACCOUNT_BY_DEPARTMENT))

    # chase mode — 只监控追投的消耗和ROI
    chase_p = sub.add_parser("chase", help="按抖音号查素材追投任务：消耗 + 综合ROI")
    chase_p.add_argument("--creator", required=True, help="抖音号达人名称")
    chase_p.add_argument("--department", default=DEFAULT_DEPARTMENT, choices=tuple(ACCOUNT_BY_DEPARTMENT))

    # task mode
    task_p = sub.add_parser("task", help="Read chase task metrics from task center")
    task_p.add_argument("--main-plan-id", required=True)
    task_p.add_argument("--task-id", required=True)
    task_p.add_argument("--department", default=DEFAULT_DEPARTMENT, choices=tuple(ACCOUNT_BY_DEPARTMENT))

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    started = time.monotonic()

    try:
        if args.mode == "plan":
            if not args.creator and not args.plan_name:
                print(json.dumps({"status": "failed", "message": "Provide --creator or --plan-name"}, ensure_ascii=False))
                return 1

            results = asyncio.run(search_plan_metrics(args.creator, args.plan_name, args.department))
            elapsed = round(time.monotonic() - started, 2)
            output = {
                "status": "ok",
                "mode": "plan",
                "count": len(results),
                "plans": [asdict(r) for r in results],
                "elapsed_seconds": elapsed,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0

        elif args.mode == "chase":
            tasks = asyncio.run(read_chase_by_creator(args.creator, args.department))
            elapsed = round(time.monotonic() - started, 2)
            output = {
                "status": "ok",
                "mode": "chase",
                "creator": args.creator,
                "count": len(tasks),
                "tasks": [asdict(t) for t in tasks],
                "elapsed_seconds": elapsed,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0

        elif args.mode == "task":
            result = asyncio.run(read_task_metrics(args.main_plan_id, args.task_id, args.department))
            elapsed = round(time.monotonic() - started, 2)
            output = {"status": "ok", "mode": "task", **asdict(result), "elapsed_seconds": elapsed}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0

        return 0

    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        print(json.dumps({"status": "failed", "message": str(exc), "elapsed_seconds": elapsed}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
