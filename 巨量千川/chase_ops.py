"""追投任务操作：暂停、恢复、预算调整、删除。

依赖 plan_metrics.py 的查询能力定位任务，然后执行操作。

Usage:
    python chase_ops.py pause --creator dou12345678 --task-id <id>
    python chase_ops.py resume --creator dou12345678 --task-id <id>
    python chase_ops.py budget --creator dou12345678 --task-id <id> --amount 200
    python chase_ops.py delete --creator dou12345678 --task-id <id>

    也支持直接指定主计划 ID（免去按抖音号搜索的步骤）：
    python chase_ops.py pause --main-plan-id <id> --task-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

from playwright.async_api import async_playwright

from .plan_metrics import (
    CDP_URL,
    DEFAULT_DEPARTMENT,
    _ensure_cdp,
    _open_overall,
    _search_plans,
    _open_chase_task_center,
    _optional_number,
)
from .session_prepare import ACCOUNT_BY_DEPARTMENT


class ChaseOpsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Helpers (ported from qianchuan_chase.py)
# ---------------------------------------------------------------------------


async def _visible(locator) -> list:
    result = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await item.is_visible():
            result.append(item)
    return result


async def _one_visible(locator, label: str):
    result = await _visible(locator)
    if len(result) != 1:
        raise ChaseOpsError(f"Expected one visible {label}; found {len(result)}")
    return result[0]


async def _locate_task(page, task_id: str):
    """Find the task row in the currently open task center."""
    # Scroll right to ensure the row is fully rendered
    scroll_area = page.locator(".qc-table.task-center-table .ovui-table__body-wrapper").first
    if await scroll_area.count():
        await scroll_area.evaluate("el => { el.scrollLeft = 0; }")
        await page.wait_for_timeout(300)

    for _ in range(40):
        rows = page.locator("tr", has_text=f"ID: {task_id}")
        for i in range(await rows.count()):
            if await rows.nth(i).is_visible():
                return rows.nth(i)
        await page.wait_for_timeout(500)
    raise ChaseOpsError(f"Task row not found: {task_id}")


# ---------------------------------------------------------------------------
# Connect + navigate to task center, then execute an action
# ---------------------------------------------------------------------------


async def _with_task_center(
    action,
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    department: str = DEFAULT_DEPARTMENT,
):
    """Connect to CDP, navigate to task center, run ``action(browser, page)``."""
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

        if creator_name:
            rows = await _search_plans(page, creator_name, department, by_creator=True)
            if not rows:
                raise ChaseOpsError(f"No plans found for 抖音号: {creator_name}")
            await _open_chase_task_center(page, rows[0])
        elif main_plan_id:
            await _open_overall(page, department)
            rows = await _search_plans(page, main_plan_id, department, by_creator=False)
            if not rows:
                raise ChaseOpsError(f"No plan found: {main_plan_id}")
            await _open_chase_task_center(page, rows[0])
        else:
            raise ChaseOpsError("Provide --creator or --main-plan-id")

        return await action(browser, page)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def pause_task(
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    task_id: str = "",
    department: str = DEFAULT_DEPARTMENT,
) -> str:
    """Pause a chase task. Returns the status after pause."""
    async def _do(browser, page):
        row = await _locate_task(page, task_id)
        current = (await row.locator("td").nth(2).inner_text()).strip()
        if "已暂停" in current:
            return current

        await row.hover()
        pause = await _one_visible(row.locator('iconpark-icon[name="iconPause"]'), "pause icon")
        await pause.evaluate("el => el.click()")
        await page.wait_for_timeout(500)

        confirm = await _one_visible(page.get_by_role("button", name="确定", exact=True), "pause confirm")
        await confirm.evaluate("el => el.click()")

        for _ in range(40):
            await page.wait_for_timeout(500)
            status = (await row.locator("td").nth(2).inner_text()).strip()
            if "已暂停" in status:
                return status
        raise ChaseOpsError(f"Task {task_id} did not enter paused status")

    return await _with_task_center(_do, creator_name, main_plan_id, department)


async def resume_task(
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    task_id: str = "",
    department: str = DEFAULT_DEPARTMENT,
) -> str:
    """Resume a paused chase task. Returns the status after resume."""
    async def _do(browser, page):
        row = await _locate_task(page, task_id)
        await row.hover()
        resume = await _one_visible(row.locator('iconpark-icon[name="iconPlay"]'), "resume icon")
        await resume.evaluate("el => el.click()")
        await page.wait_for_timeout(500)

        confirm = await _one_visible(page.get_by_role("button", name="确定", exact=True), "resume confirm")
        await confirm.evaluate("el => el.click()")

        for _ in range(30):
            await page.wait_for_timeout(500)
            status = (await row.locator("td").nth(2).inner_text()).strip()
            if "调控中" in status:
                return status
        raise ChaseOpsError(f"Task {task_id} did not resume to active status")

    return await _with_task_center(_do, creator_name, main_plan_id, department)


async def set_task_budget(
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    task_id: str = "",
    budget: int = 100,
    department: str = DEFAULT_DEPARTMENT,
) -> float:
    """Update a chase task's budget. Returns the new budget value."""
    async def _do(browser, page):
        row = await _locate_task(page, task_id)
        budget_cell = row.locator("td").nth(3)
        await budget_cell.hover()
        edit = await _one_visible(
            budget_cell.locator('iconpark-icon[name="oc-icon-light-edit"]'),
            "budget edit icon",
        )
        await edit.evaluate("el => el.click()")

        budget_inputs = page.locator('input[placeholder="请输入"]')
        fields = None
        for _ in range(20):
            fields = await _visible(budget_inputs)
            if len(fields) >= 1:
                break
            await page.wait_for_timeout(300)
        if not fields:
            raise ChaseOpsError("Budget input did not appear")

        field = fields[0]
        await field.fill(str(budget))
        await field.press("Tab")

        confirm = await _one_visible(page.get_by_role("button", name="确定", exact=True), "budget confirm")
        await confirm.evaluate("el => el.click()")

        for _ in range(20):
            await page.wait_for_timeout(500)
            current = _optional_number(await row.locator("td").nth(3).inner_text())
            if current == float(budget):
                return current
        raise ChaseOpsError(f"Task {task_id} budget did not update to {budget}")

    return await _with_task_center(_do, creator_name, main_plan_id, department)


async def delete_task(
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    task_id: str = "",
    department: str = DEFAULT_DEPARTMENT,
) -> None:
    """Delete a chase task."""
    async def _do(browser, page):
        row = await _locate_task(page, task_id)
        await row.hover()
        delete = await _one_visible(row.locator('iconpark-icon[name="oc-icon-delete"]'), "delete icon")
        await delete.evaluate("el => el.click()")
        await page.wait_for_timeout(500)

        confirm = await _one_visible(page.get_by_role("button", name="确定", exact=True), "delete confirm")
        await confirm.evaluate("el => el.click()")
        await page.wait_for_timeout(1000)

        remaining = page.locator("tr", has_text=f"ID: {task_id}")
        if await remaining.count():
            raise ChaseOpsError(f"Task {task_id} remained visible after deletion")

    return await _with_task_center(_do, creator_name, main_plan_id, department)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage chase tasks: pause/resume/budget/delete.")
    parser.add_argument("action", choices=["pause", "resume", "budget", "delete"], help="操作类型")
    parser.add_argument("--creator", help="抖音号达人名称（与 --main-plan-id 二选一）")
    parser.add_argument("--main-plan-id", help="主计划 ID（与 --creator 二选一）")
    parser.add_argument("--task-id", required=True, help="追投任务 ID")
    parser.add_argument("--amount", type=int, help="新预算金额（仅 budget 操作使用）")
    parser.add_argument("--department", default=DEFAULT_DEPARTMENT, choices=tuple(ACCOUNT_BY_DEPARTMENT))
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    started = time.monotonic()

    if not args.creator and not args.main_plan_id:
        print(json.dumps({"status": "failed", "message": "Provide --creator or --main-plan-id"}, ensure_ascii=False))
        return 1

    try:
        result = None
        if args.action == "pause":
            status = asyncio.run(pause_task(args.creator, args.main_plan_id, args.task_id, args.department))
            result = {"action": "pause", "task_id": args.task_id, "status": status}

        elif args.action == "resume":
            status = asyncio.run(resume_task(args.creator, args.main_plan_id, args.task_id, args.department))
            result = {"action": "resume", "task_id": args.task_id, "status": status}

        elif args.action == "budget":
            new_budget = asyncio.run(set_task_budget(args.creator, args.main_plan_id, args.task_id, args.amount, args.department))
            result = {"action": "budget", "task_id": args.task_id, "new_budget": new_budget}

        elif args.action == "delete":
            asyncio.run(delete_task(args.creator, args.main_plan_id, args.task_id, args.department))
            result = {"action": "delete", "task_id": args.task_id, "status": "deleted"}

        elapsed = round(time.monotonic() - started, 2)
        output = {"status": "ok", **result, "elapsed_seconds": elapsed}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        print(json.dumps({"status": "failed", "message": str(exc), "elapsed_seconds": elapsed}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
