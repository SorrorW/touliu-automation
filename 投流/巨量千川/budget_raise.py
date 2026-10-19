"""ROI>10 且订单>10 的计划预算上调（2026-08-17 新规）。

规则（用户定）：新计划统一 ROI 4.5、日预算 300；每天扫描一次，
当天综合 ROI > 10 且净成交订单数 > 10 的计划把日预算改为「不限」（平台无不限选项，
操作上等价设置为 99999 元/天，相对业务量即不限）。

用法：
  - webui 全部门扫描完成后每天自动执行一次（当天已调过的计划跳过）；
  - CLI 手动：python -m 巨量千川.budget_raise           （只列出，不动计划）
             python -m 巨量千川.budget_raise --apply  （实际改预算）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright

from .daily_high_performer_monitor import scan_all_plans
from .manual_whitelist import is_manual_whitelisted
from .old_creator_retarget import _open_edit_page, _wait_stable
from .plan_metrics import _ensure_cdp, _search_plans
from .plan_sweep import _save_edit_page
from .session_prepare import connect_browser, open_fresh_page

ROI_RAISE_THRESHOLD = 10.0       # 当天综合 ROI > 10 才上调（严格大于，用户定）
ORDERS_RAISE_THRESHOLD = 10       # 当天净成交订单数 > 10 才上调（严格大于，用户定）
RAISE_BUDGET_TO = 99999          # 「预算不限」的等价设置（平台无不限选项）


def find_raise_targets(report: dict) -> list[dict]:
    """从扫描报告挑出 ROI>10、订单>10 且投放中的计划。"""
    targets: list[dict] = []
    for dep, rec in (report or {}).items():
        for p in (rec or {}).get("plans") or []:
            if is_manual_whitelisted(
                creator=p.get("creator_name") or "",
                department=dep,
                plan_id=p.get("plan_id") or "",
                plan_name=p.get("plan_name") or "",
            ):
                continue
            roi = p.get("roi")
            if roi is None or roi <= ROI_RAISE_THRESHOLD:
                continue
            orders = p.get("orders")
            if orders is None or orders <= ORDERS_RAISE_THRESHOLD:
                continue
            if "投放中" not in (p.get("status") or ""):
                continue
            if not p.get("plan_id"):
                continue
            targets.append({
                "department": dep,
                "plan_id": p["plan_id"],
                "plan_name": p.get("plan_name") or "",
                "roi": roi,
                "orders": orders,
                "spent": p.get("spent"),
            })
    return targets


async def _read_budget(page) -> float:
    """Read the current daily-budget value from an open plan edit form.

    Table-level budget parsing can be unavailable in the overview.  A 12:00
    promotion therefore reads the authoritative edit-form input before it
    calculates how much budget must be topped up.
    """
    inputs = page.locator('input[placeholder="请输入"]')
    if await inputs.count() >= 2:
        raw = (await inputs.nth(1).input_value()).strip().replace(",", "")
    else:
        raw = await page.evaluate("""() => {
            const labels = Array.from(document.querySelectorAll('div, span, label'));
            for (const el of labels) {
                const t = (el.innerText || '').trim();
                if (t === '日预算' || t === '预算') {
                    const box = el.closest('.ovui-form-item, .form-item, div');
                    const input = box && box.querySelector('input');
                    if (input) return input.value || '';
                }
            }
            return '';
        }""")
        raw = str(raw or '').strip().replace(",", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"编辑页日预算无法解析: {raw!r}")
    if value < 0:
        raise RuntimeError(f"编辑页日预算异常: {value}")
    return value


async def _set_budget(page, value: int) -> None:
    """编辑页填日预算。优先沿用创建页约定（placeholder=请输入 的第 2 个输入框），
    兜底按「日预算」标签就近找输入框。"""
    inputs = page.locator('input[placeholder="请输入"]')
    if await inputs.count() >= 2:
        await inputs.nth(1).fill(str(value))
        return
    ok = await page.evaluate("""(val) => {
        const labels = Array.from(document.querySelectorAll('div, span, label'));
        for (const el of labels) {
            const t = (el.innerText || '').trim();
            if (t === '日预算' || t === '预算') {
                const box = el.closest('.ovui-form-item, .form-item, div');
                const input = box && box.querySelector('input');
                if (input) {
                    input.value = val;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
        }
        return false;
    }""", str(value))
    if not ok:
        raise RuntimeError("编辑页未找到日预算输入框")


async def raise_budgets(report: dict, already_raised: set[str] | None = None) -> dict:
    """对报告里 ROI>10 且订单>10 的计划逐个改预算为不限。"""
    done_before = set(already_raised or set())
    targets = [t for t in find_raise_targets(report) if t["plan_id"] not in done_before]
    raised: list[str] = []
    failed: list[str] = []

    if targets:
        _ensure_cdp()
        async with async_playwright() as pw:
            browser = await connect_browser(pw, timeout=8000)
            page = await open_fresh_page(browser.contexts[0])
            try:
                for t in targets:
                    label = (
                        f"{t['plan_name']}（{t['department']}，ROI {t['roi']:g}，"
                        f"订单 {t['orders']}）"
                    )
                    try:
                        rows = await _search_plans(
                            page, t["plan_id"], t["department"], by_creator=False)
                        if not rows:
                            failed.append(f"{label}: 计划行未找到")
                            continue
                        await _open_edit_page(page, rows[0])
                        await _wait_stable(page, 2.0)
                        await _set_budget(page, RAISE_BUDGET_TO)
                        saved = await _save_edit_page(page)
                        if not saved:
                            failed.append(f"{label}: 未找到保存按钮")
                            continue
                        raised.append(t["plan_id"])
                    except Exception as exc:
                        failed.append(f"{label}: {str(exc)[:120]}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    return {
        "success": not failed,
        "threshold": ROI_RAISE_THRESHOLD,
        "orders_threshold": ORDERS_RAISE_THRESHOLD,
        "budget_to": RAISE_BUDGET_TO,
        "targets": len(targets),
        "raised": raised,
        "failed": failed,
        "message": (
            f"ROI>{ROI_RAISE_THRESHOLD:g}、订单>{ORDERS_RAISE_THRESHOLD} "
            f"计划 {len(targets)} 条待调："
            f"已改不限 {len(raised)}、失败 {len(failed)}"
            + (f"（当天已调过 {len(done_before)} 条跳过）" if done_before else "")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ROI>10 且订单>10 的计划预算改不限")
    parser.add_argument("--apply", action="store_true", help="实际改预算；默认只列出")
    args = parser.parse_args()

    async def _run() -> dict:
        report = await scan_all_plans(department="all")
        if not args.apply:
            return {"success": True, "dry_run": True,
                    "targets": find_raise_targets(report)}
        return await raise_budgets(report)

    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
