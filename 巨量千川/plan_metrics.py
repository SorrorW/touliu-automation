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
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from .session_prepare import ACCOUNT_BY_DEPARTMENT, ensure_qianchuan_page

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CDP_URL = "http://127.0.0.1:9222"
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
    budget: float
    delivery_type: str = ""
    creator_name: str | None = None
    plan_id: str | None = None
    message: str = ""
    orders: int | None = None
    impression_count: int = 0
    click_count: int = 0
    net_deal_amount: float | None = None  # 净成交金额，仅供人工核对，不参与达标判定


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


async def _column_labels(tr, cell_count: int) -> list[str] | None:
    """Return header labels aligned with the row's cell order, or None.

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


def _ensure_cdp() -> None:
    """Quick-check that the QQBrowser CDP port is responsive."""
    from urllib.request import urlopen

    try:
        with urlopen(f"{CDP_URL}/json/version", timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl", "")
            if not ws.startswith("ws://127.0.0.1:9222/"):
                raise ConnectionError("CDP WebSocket URL is not local")
    except Exception as exc:
        raise RuntimeError(
            "QQBrowser CDP is not available. Run `python session_prepare.py` first."
        ) from exc


# ---------------------------------------------------------------------------
# Task-level: read chase task metrics from task center
# ---------------------------------------------------------------------------


async def _open_overall(page, department: str = "业务3部") -> None:
    """Ensure page is on the overall plan page with search input available."""
    await ensure_qianchuan_page(page, department, timeout=20.0)


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
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

        scroll_areas = page.locator(".qc-table.task-center-table .ovui-table__body-wrapper")
        if await scroll_areas.count() == 0 or not await scroll_areas.first.is_visible():
            # Not on task center page — navigate through overall
            await _open_overall(page, department)
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
    # The filter select is a dropdown beside the search input
    selector = page.locator(".oc-filter-select-input-select .ovui-select__input").first
    if not await selector.count():
        return  # no mode switch needed

    # Check if already in the correct mode
    current_ph = page.locator('input[placeholder*="抖音号"]')
    if target == "抖音号" and await current_ph.count():
        return  # already in 抖音号 mode
    current_ph = page.locator('input[placeholder*="计划名称"]')
    if target == "计划名称" and await current_ph.count():
        return  # already in plan name mode

    # Click the filter selector to open dropdown
    await selector.click()
    await page.wait_for_timeout(500)

    # Click the target option
    option = page.get_by_text(target, exact=True).first
    if await option.count():
        await option.click()
        await page.wait_for_timeout(500)


async def _set_time_range(page, mode: str) -> None:
    """Switch the overall page date/time range preset.

    千川总览页日期快捷选项结构：
        .oc-range-picker-shortcut > .oc-range-picker-shortcut-item

    Args:
        mode: "today" — 今天（页面默认）; "last_7_days" — 近7天。
    """
    if mode == "today":
        return  # 页面默认就是今天，无需操作

    target = "近7天" if mode == "last_7_days" else mode

    # 定位 .oc-range-picker-shortcut-item，类名含 ovui-shortcut
    items = page.locator(".oc-range-picker-shortcut-item")
    count = await items.count()
    if count == 0:
        print(f"WARNING: 未找到日期快捷选项容器，跳过日期切换 ({target})")
        return

    # 找目标 item
    found = None
    for i in range(count):
        try:
            text = (await items.nth(i).inner_text()).strip()
            if target in text:
                found = items.nth(i)
                break
        except Exception:
            continue

    if not found:
        print(f"WARNING: 未找到日期选项 '{target}'，保持当前日期范围")
        return

    # 记录切换前的表格内容，用于检测 reload 完成
    try:
        prev_text = await page.locator("table tbody").first.inner_text()
    except Exception:
        prev_text = ""

    # 用 JS click 避免被遮挡
    try:
        await found.evaluate("el => el.click()")
    except Exception as exc:
        print(f"WARNING: 点击 '{target}' 失败: {exc}")
        return

    await _wait_table_settled(page, prev_text)


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
    await _open_overall(page, department)

    # Switch time range first (before search)
    if time_range:
        await _set_time_range(page, time_range)

    # Switch search mode if needed
    mode = "抖音号" if by_creator else "计划名称"
    await _switch_search_mode(page, mode)

    # Find the search input
    search_sel = 'input[placeholder*="抖音号"]' if by_creator else 'input[placeholder*="计划名称"]'
    search = page.locator(search_sel).first
    if not await search.count() or not await search.is_visible():
        raise RuntimeError(f"Search input not found (mode={mode})")

    # 日期切换后表格异步刷新可能吞掉搜索请求，0 行时重试一次
    for attempt in range(2 if time_range else 1):
        await search.fill(keyword)
        await search.press("Enter")
        await page.wait_for_timeout(800)

        rows = []
        for _ in range(20):
            trs = page.locator("table tbody tr")
            count = await trs.count()
            if count > 0:
                for i in range(count):
                    tr = trs.nth(i)
                    if await tr.is_visible():
                        rows.append(tr)
            if rows:
                break
            await page.wait_for_timeout(500)
        if rows:
            return rows
        if attempt == 0 and time_range:
            print("WARNING: 切换日期后搜索无结果，重试一次", file=sys.stderr)
    return rows


async def _parse_plan_row(tr, labels: list[str] | None = None) -> PlanMetrics:
    """Extract metrics from a single plan table row.

    Metric columns (ROI / 消耗 / 订单数 / 净成交金额) are located by header
    text, not fixed indices — 千川 reshuffles its columns over time (e.g. the
    综合营销ROI column was added after the old hard-coded layout went stale,
    which made roi read 净成交金额 instead). Fixed indices remain only as a
    last-resort fallback when the header cannot be read.

    Args:
        tr: Playwright locator for the <tr> element.
        labels: Pre-computed column header labels (from _column_labels).
                When provided, avoids redundant per-row CDP round-trips.
    """

    def _first_value(text: str) -> str:
        """Take the first numeric value before any '基础' or '|' separator."""
        return text.split("基础")[0].split("|")[0].strip()

    cells = tr.locator("td, th")
    cell_count = await cells.count()

    texts = []
    for i in range(cell_count):
        texts.append((await cells.nth(i).inner_text()).strip())

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
        r"^\d{2,4}[-_]\d{1,2}[-_]\s*(.+?)\s*[-_]",  # "07-18_示例达人丙_张"
        r"\d{2,4}[-_]\s*(.+?)\s*[-_]",   # "0716-示例作者-张" / "0727_示例达人_王"
        r"^[\d.]+[-_]?\s*(.+?)\s*[-_]",  # "0415示例达人丁-王" / "6.9-示例达人戊-商品B-李"
        r"[\d.]+[-_\s]*(.+?)\s*[（(]",   # "3.25宁静（小厨娘）" → "宁静"
    ]:
        m = re.search(pattern, plan_name)
        if m:
            creator_name = m.group(1).strip()
            break

    # --- Status (cell[2]) — take only the first line ---
    status = (texts[2].split("\n")[0] if cell_count > 2 else "").strip()

    # --- Budget from settings cell[3] ---
    budget = 0.0
    if cell_count > 3:
        budget_match = re.search(r"(\d+[\d.]*)\s*元", texts[3])
        if budget_match:
            budget = float(budget_match.group(1))

    # --- Metric columns located by header text (layout shifts over time) ---
    if labels is None:
        labels = await _column_labels(tr, cell_count)
    if labels is None:
        print(
            "WARNING: plan table header unavailable, falling back to legacy column indices",
            file=sys.stderr,
        )

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
    orders_idx = _col_index(labels, "净成交订单数", fallback=14)
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

    # --- Impressions / Clicks ---
    impression_count = 0
    click_count = 0

    return PlanMetrics(
        plan_name=plan_name,
        status=status,
        spent=spent or 0.0,
        roi=roi,
        budget=budget or 0.0,
        delivery_type=delivery_type,
        creator_name=creator_name,
        plan_id=plan_id,
        orders=orders,
        impression_count=impression_count,
        click_count=click_count,
        net_deal_amount=net_deal_amount,
    )


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
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

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
            results.append(metrics)

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


async def list_all_plans(
    department: str = DEFAULT_DEPARTMENT,
    progress_cb=None,  # Callable[[int, int], None] — (已读计划数, 分页总条数)，每页上报
) -> list[PlanMetrics]:
    """List every plan on the overall page (no keyword filter), across all pages.

    Clears any residual search keyword, reads the pagination total (共 N 条记录),
    clicks through pages until all rows are collected, and deduplicates by plan_id.
    """
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

        await _open_overall(page, department)

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
            for _ in range(20):
                trs = page.locator("table tbody tr")
                visible = []
                for i in range(await trs.count()):
                    tr = trs.nth(i)
                    if await tr.is_visible():
                        visible.append(tr)
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
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

        # Search by 抖音号
        rows = await _search_plans(page, creator_name, department, by_creator=True)
        if not rows:
            return {"plan": None, "tasks": []}

        plan_metrics = await _parse_plan_row(rows[0])

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
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

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
