"""Move verified low-performing plans into the old-plan pool.

The caller must provide targets selected from yesterday's read-only report.  This
module changes only ROI target and daily budget; it never changes plan power or
creates material follow-up tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .budget_raise import _set_budget
from .manual_whitelist import is_manual_whitelisted, manual_label
from .old_creator_retarget import _open_edit_page, _wait_stable
from .plan_metrics import _ensure_cdp, _parse_plan_row, _search_plans
from .plan_sweep import _save_edit_page
from .session_prepare import connect_browser, open_fresh_page

OLD_POOL_BUDGET = 300
OLD_POOL_ROI = 10.3


async def _wait_edit_inputs(page, timeout_seconds: float = 30.0) -> None:
    """Wait until the edit form renders its ROI and daily-budget inputs."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    inputs = page.locator('input[placeholder="请输入"]')
    while asyncio.get_running_loop().time() < deadline:
        if await inputs.count() >= 2:
            return
        await page.wait_for_timeout(500)
    raise RuntimeError("编辑页表单加载超时，未找到 ROI 和日预算输入框")


async def _set_roi(page, value: float) -> None:
    """Set the first standard input in the plan edit form, which is the ROI target."""
    inputs = page.locator('input[placeholder="请输入"]')
    if await inputs.count() >= 2:
        await inputs.nth(0).fill(str(value))
        return
    ok = await page.evaluate("""(val) => {
        const visible = el => el && el.offsetParent !== null && !el.disabled;
        const setValue = input => {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(input, val);
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new Event('blur', {bubbles: true}));
        };
        for (const input of Array.from(document.querySelectorAll('input')).filter(visible)) {
            let node = input;
            let context = '';
            for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
                context += ' ' + (node.innerText || '');
            }
            if (/ROI|投产比/i.test(context)) {
                setValue(input);
                return true;
            }
        }
        return false;
    }""", str(value))
    if not ok:
        raise RuntimeError("编辑页未找到 ROI 目标输入框")


def _write_progress(
    progress_file: Path | None,
    total: int,
    changed: list[str],
    failed: list[dict[str, str]],
    skipped: list[dict[str, str]],
    roi: float = OLD_POOL_ROI,
    budget: int = OLD_POOL_BUDGET,
) -> None:
    if progress_file is None:
        return
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(
        json.dumps(
            {
                "targets": total,
                "completed": len(changed) + len(failed) + len(skipped),
                "changed": changed,
                "failed": failed,
                "skipped": skipped,
                "budget": budget,
                "roi": roi,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def apply_old_pool(
    targets: list[dict[str, Any]],
    progress_file: Path | None = None,
    reuse_page: bool = False,
    roi: float = OLD_POOL_ROI,
    budget: int = OLD_POOL_BUDGET,
    allow_paused: bool = False,
) -> dict[str, Any]:
    """Apply ROI target + daily budget to exact plan IDs and verify only the save action.

    默认老计划池参数（10.3/300）；23:00 收口也用本函数给潜力计划写 7.3/300，
    通过 roi/budget 参数传入。函数只改 ROI 目标和日预算，不动计划开关。
    暂停计划默认跳过（防止覆盖人工暂停的意图）；allow_paused=True 时
    连暂停计划也改——仅限用户明确指令的迁移/对账纠偏场景（2026-08-24）。
    """
    changed: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    _ensure_cdp()
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        owned_page = True
        page = None
        if reuse_page:
            page = next((p for p in context.pages if "qianchuan.jinritemai.com" in p.url), None)
            owned_page = page is None
        if page is None:
            page = await open_fresh_page(context)
        try:
            for target in targets:
                plan_id = str(target.get("plan_id") or "").strip()
                department = str(target.get("department") or "").strip()
                plan_name = str(target.get("plan_name") or plan_id)
                if is_manual_whitelisted(
                    creator=str(target.get("creator_name") or ""),
                    department=department,
                    plan_id=plan_id,
                    plan_name=plan_name,
                ):
                    skipped.append({
                        "plan": plan_name,
                        "plan_id": plan_id,
                        "reason": f"{manual_label(department=department, plan_id=plan_id, plan_name=plan_name)}人工白名单",
                    })
                    _write_progress(progress_file, len(targets), changed, failed, skipped, roi, budget)
                    continue
                plan_status = str(target.get("plan_status") or target.get("status") or "")
                if not plan_id or not department:
                    failed.append({"plan": plan_name, "error": "缺少计划 ID 或部门"})
                    _write_progress(progress_file, len(targets), changed, failed, skipped, roi, budget)
                    continue
                if not allow_paused and any(status in plan_status for status in ("系统暂停", "已暂停")):
                    skipped.append({"plan": plan_name, "plan_id": plan_id, "reason": "暂停状态，不修改"})
                    _write_progress(progress_file, len(targets), changed, failed, skipped, roi, budget)
                    continue
                try:
                    rows = await _search_plans(page, plan_id, department, by_creator=False)
                    row = None
                    metrics = None
                    for candidate in rows:
                        metrics = await _parse_plan_row(candidate)
                        if metrics is None:  # 畸形行已留 WARNING，跳过
                            continue
                        if metrics.plan_id == plan_id:
                            row = candidate
                            break
                    if row is None:
                        raise RuntimeError("按计划 ID 未找到唯一计划行")
                    if not allow_paused and metrics and any(status in str(metrics.status or "") for status in ("系统暂停", "已暂停")):
                        skipped.append({"plan": plan_name, "plan_id": plan_id, "reason": "页面显示暂停状态，不修改"})
                        _write_progress(progress_file, len(targets), changed, failed, skipped, roi, budget)
                        continue
                    await _open_edit_page(page, row)
                    await _wait_edit_inputs(page)
                    await _set_roi(page, roi)
                    await _set_budget(page, budget)
                    if not await _save_edit_page(page):
                        raise RuntimeError("未找到保存按钮")
                    changed.append(plan_id)
                except Exception as exc:
                    failed.append({"plan": plan_name, "plan_id": plan_id, "error": str(exc)[:200]})
                _write_progress(progress_file, len(targets), changed, failed, skipped, roi, budget)
        finally:
            if owned_page:
                try:
                    await page.close()
                except Exception:
                    pass
    return {
        "success": not failed,
        "targets": len(targets),
        "changed": changed,
        "failed": failed,
        "skipped": skipped,
        "budget": budget,
        "roi": roi,
    }


async def verify_old_pool(target: dict[str, Any]) -> dict[str, Any]:
    """Read the saved ROI target and daily budget from an exact plan edit page."""
    plan_id = str(target.get("plan_id") or "").strip()
    department = str(target.get("department") or "").strip()
    if not plan_id or not department:
        raise RuntimeError("缺少计划 ID 或部门")
    _ensure_cdp()
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        page = await open_fresh_page(browser.contexts[0])
        try:
            rows = await _search_plans(page, plan_id, department, by_creator=False)
            row = None
            for candidate in rows:
                metrics = await _parse_plan_row(candidate)
                if metrics is None:  # 畸形行已留 WARNING，跳过
                    continue
                if metrics.plan_id == plan_id:
                    row = candidate
                    break
            if row is None:
                raise RuntimeError("按计划 ID 未找到唯一计划行")
            await _open_edit_page(page, row)
            await _wait_edit_inputs(page)
            values = await page.evaluate("""() => {
                const visible = el => el && el.offsetParent !== null;
                return Array.from(document.querySelectorAll('input')).filter(visible).map(input => {
                    let node = input;
                    let context = '';
                    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
                        context += ' ' + (node.innerText || '');
                    }
                    return {value: input.value, context};
                });
            }""")
            roi = next((item["value"] for item in values if "ROI" in item["context"] or "投产比" in item["context"]), None)
            budget = next((item["value"] for item in values if "日预算" in item["context"] or "预算" in item["context"]), None)
            return {"plan_id": plan_id, "roi_target": roi, "daily_budget": budget}
        finally:
            try:
                await page.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="将昨日低效计划调整为老计划池参数")
    parser.add_argument("--report", required=True, help="昨日扫描候选 JSON 文件")
    parser.add_argument("--apply", action="store_true", help="实际保存 ROI 10.3、预算 300")
    parser.add_argument("--verify", action="store_true", help="只读回查第一个候选计划的 ROI 和日预算")
    parser.add_argument("--progress", help="每条计划完成后写入的进度 JSON 文件")
    parser.add_argument("--reuse-page", action="store_true", help="复用现有千川页面，不新开标签页")
    args = parser.parse_args()
    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    targets = list(payload.get("targets") or [])
    if args.verify:
        if len(targets) != 1:
            raise SystemExit("回查需要且只能有一条候选计划")
        print(json.dumps(asyncio.run(verify_old_pool(targets[0])), ensure_ascii=False))
        return 0
    if not args.apply:
        print(json.dumps({"targets": targets, "budget": OLD_POOL_BUDGET, "roi": OLD_POOL_ROI}, ensure_ascii=False))
        return 0
    result = asyncio.run(
        apply_old_pool(
            targets,
            Path(args.progress) if args.progress else None,
            args.reuse_page,
        )
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
