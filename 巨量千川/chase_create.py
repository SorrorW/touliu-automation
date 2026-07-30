"""为主计划创建素材追投任务。

流程：搜抖音号 → 点"素材追投"的"+"号 → 选视频 → 填预算/时长 → 提交

Usage:
    python chase_create.py create --creator 12345678abc --budget 100 --hours 24
    python chase_create.py create --plan-id <id> --budget 100 --hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime

from playwright.async_api import async_playwright

from .plan_metrics import (
    CDP_URL,
    DEFAULT_DEPARTMENT,
    _ensure_cdp,
    _search_plans,
    _open_chase_task_center,
)
from .session_prepare import ACCOUNT_BY_DEPARTMENT


class ChaseCreateError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Helpers
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
        raise ChaseCreateError(f"Expected one visible {label}; found {len(result)}")
    return result[0]


async def _dismiss_banners(page) -> None:
    """最佳努力关闭顶部公告/通知横幅（遮挡操作区会导致自动化点击失效）。"""
    try:
        await page.evaluate(
            """() => {
                let n = 0;
                // 1) 「我知道了」类确认钮
                for (const el of document.querySelectorAll('a, button, span')) {
                    if (el.innerText && el.innerText.trim() === '我知道了' && el.offsetParent) {
                        el.click(); n++;
                    }
                }
                // 2) 公告/通知容器里的关闭钮
                const closeBtns = document.querySelectorAll(
                    'iconpark-icon[name*="close"], [class*="close-btn"], [class*="close-icon"], .ovui-message__close'
                );
                for (const btn of closeBtns) {
                    if (!btn.offsetParent) continue;
                    let p = btn, text = '';
                    for (let i = 0; i < 6 && p; i++) {
                        p = p.parentElement;
                        if (p && p.innerText && p.innerText.length > 20) { text = p.innerText; break; }
                    }
                    if (/公告|通知|升级预告/.test(text)) { btn.click(); n++; }
                }
                return n;
            }"""
        )
    except Exception:
        pass  # 点不到就当没有


# 任务中心各 tab（核实新任务时逐个排查）
_CHASE_TABS = ("素材追投", "一键起量", "搜索抢首屏")


async def _find_new_task_id(page, task_name: str, submit_at: datetime) -> str:
    """在任务中心各 tab 找刚创建的任务 ID。

    先按任务名精确匹配（若抓到可靠 task_name）；
    否则按「创建时间在提交时刻 ±5 分钟内」兜底（任务中心是按计划隔离的，
    时间窗内的新行就是刚创建的那条）。
    """
    time_fallback = ""
    for tab in _CHASE_TABS:
        tab_el = page.get_by_text(tab, exact=True).first
        try:
            if await tab_el.count() and await tab_el.is_visible():
                await tab_el.evaluate("el => el.click()")
                await page.wait_for_timeout(600)
        except Exception:
            continue
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            rows = page.locator(".qc-table.task-center-table tbody tr")
            for i in range(await rows.count()):
                row = rows.nth(i)
                try:
                    if not await row.is_visible():
                        continue
                    text = await row.inner_text()
                except Exception:
                    continue
                if task_name and task_name in text:
                    m = re.search(r"ID[：:]?\s*(\d+)", text)
                    if m:
                        return m.group(1)
                if time_fallback:
                    continue  # 已有一个时间窗候选，继续找精确名
                m_time = re.search(r"(20\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", text)
                if m_time:
                    try:
                        created = datetime.strptime(
                            f"{m_time.group(1)} {m_time.group(2)}", "%Y-%m-%d %H:%M:%S"
                        )
                    except ValueError:
                        continue
                    if abs((created - submit_at).total_seconds()) <= 300:
                        m = re.search(r"ID[：:]?\s*(\d+)", text)
                        if m:
                            time_fallback = m.group(1)
            if task_name and not time_fallback:
                await page.wait_for_timeout(300)  # 等表格加载/刷新
            else:
                break
        if time_fallback and not task_name:
            break  # 没有名字可对，时间窗候选即可信
    return time_fallback


# ---------------------------------------------------------------------------
# Core: create chase task
# ---------------------------------------------------------------------------


async def create_chase(
    creator_name: str | None = None,
    main_plan_id: str | None = None,
    department: str = DEFAULT_DEPARTMENT,
    budget: int = 100,
    hours: float = 24.0,
) -> dict:
    """创建素材追投任务。返回 task_id 等结果。"""
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        context = browser.contexts[0]
        pages = [p for p in context.pages if "qianchuan" in p.url]
        page = pages[0] if pages else context.pages[0]

        # 1. 定位计划行
        if creator_name:
            rows = await _search_plans(page, creator_name, department, by_creator=True)
            if not rows:
                raise ChaseCreateError(f"No plans found for 抖音号: {creator_name}")
        elif main_plan_id:
            rows = await _search_plans(page, main_plan_id, department, by_creator=False)
            if not rows:
                raise ChaseCreateError(f"No plan found: {main_plan_id}")
        else:
            raise ChaseCreateError("Provide --creator or --plan-id")
        tr = rows[0]
        plan_name_text = await tr.locator("td").nth(1).inner_text()
        plan_id_match = re.search(r"ID[：:]?\s*(\d+)", plan_name_text)
        resolved_plan_id = plan_id_match.group(1) if plan_id_match else ""

        # 按 plan_id 搜索时验证行 ID 精确匹配（搜纯数字会模糊匹配到其他计划）
        if main_plan_id and resolved_plan_id != main_plan_id:
            raise ChaseCreateError(
                f"搜索 plan_id={main_plan_id} 匹配到的行 ID={resolved_plan_id}，"
                "目标计划不精确匹配，请用 creator 搜索或确认 plan_id 正确"
            )

        # 关掉遮挡操作区的公告/通知横幅
        await _dismiss_banners(page)

        # 2. 点 "+" 号打开追投创建表单
        # 先把行/列滚进可视区域（素材追投列在右侧，可能被 sticky 列挤出视口）
        await tr.evaluate(
            """(row) => {
                row.scrollIntoView({ block: 'center', inline: 'center' });
                const table = row.closest('table');
                const wrap = table && table.closest('.ovui-table__body-wrapper');
                if (wrap) wrap.scrollLeft = wrap.scrollWidth;
            }"""
        )
        await page.wait_for_timeout(800)

        # 用 JS 直接点击「素材追投」格子里面的 plus 图标（避免 hover 触发问题）
        clicked = await tr.evaluate(
            """(row) => {
                const tasks = row.querySelectorAll('.assist-task');
                for (const t of tasks) {
                    if (t.innerText.includes('素材追投')) {
                        const icon = t.querySelector('iconpark-icon[name="oc-icon-plus"]');
                        if (icon) { icon.click(); return 'ok'; }
                        return 'no_plus_icon';
                    }
                }
                return 'no_cell';
            }"""
        )
        if clicked != "ok":
            # 兜底：回到原来的 locator 方式
            chase_span = tr.get_by_text("素材追投", exact=True).first
            chase_div = chase_span.locator("..")
            plus = chase_div.locator('iconpark-icon[name="oc-icon-plus"]')
            if not await plus.count():
                await _open_chase_task_center(page, tr)
                raise ChaseCreateError(
                    "该计划已有追投任务，请用 chase_ops.py 管理，"
                    "或确认当前计划需要新建追投"
                )
            await plus.evaluate("el => el.click()")
        await page.wait_for_timeout(1000)

        # 3. 点"添加视频"打开视频选择器
        add_video = page.get_by_role("button", name="添加视频", exact=True)
        await add_video.wait_for(state="visible", timeout=8000)
        await add_video.evaluate("el => el.click()")
        await page.wait_for_timeout(1500)

        # 4. 选第一个可用视频
        # 视频选择器弹窗里的表格（特征是：在 drawer 容器内、有 checkbox）
        video_table = page.locator('[class*="drawer"] table.ovui-table, [class*="overlay"] table.ovui-table').first
        if not await video_table.count():
            # fallback: 找最后一个可见且有 checkbox 的表格
            all_tables = page.locator("table.ovui-table")
            for i in range(await all_tables.count(), 0, -1):
                t = all_tables.nth(i - 1)
                if await t.is_visible() and await t.locator("input[type='checkbox']").count():
                    video_table = t
                    break
        if not await video_table.count():
            raise ChaseCreateError("No video picker table found")

        first_row = video_table.locator("tbody tr").first
        if not await first_row.count():
            raise ChaseCreateError("Video picker table is empty")

        # 点第一行第一个单元格触发选中
        first_cell = first_row.locator("td").first
        box = await first_cell.bounding_box()
        if not box:
            raise ChaseCreateError("Video cell has no bounding box")
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(1000)

        # 5. 点"确定"确认视频选择
        await page.wait_for_timeout(500)
        confirms = await _visible(page.get_by_role("button", name="确定", exact=True))
        if not confirms:
            raise ChaseCreateError("Video picker confirm button not found")
        await confirms[-1].evaluate("el => el.click()")
        await page.wait_for_timeout(1500)

        # 6. 先获取自动生成的任务名（限定在表单弹层内，避免抓到任务中心的搜索框）
        task_name_input = page.locator(
            '[class*="drawer"] input[placeholder*="任务名称"], '
            '[class*="modal"] input[placeholder*="任务名称"], '
            '[class*="overlay"] input[placeholder*="任务名称"]'
        ).first
        task_name = ""
        if await task_name_input.count():
            task_name = (await task_name_input.input_value()).strip()

        # 7. 填预算 + 时长
        budget_inputs = page.locator('input[placeholder="请输入"]')
        fields = await _visible(budget_inputs)
        if len(fields) < 2:
            raise ChaseCreateError(f"Expected budget and duration inputs; found {len(fields)}")
        await fields[0].fill(str(budget))
        await fields[1].fill(str(hours))
        await page.wait_for_timeout(300)

        # 8. 提交
        await fields[1].press("Tab")
        await page.wait_for_timeout(500)
        submit = page.get_by_role("button", name="提交", exact=True)
        await submit.wait_for(state="visible", timeout=5000)
        if not await submit.is_enabled():
            raise ChaseCreateError("Submit button is disabled")
        # 用 JS 点击避免底部按钮被弹层/滚动遮挡
        await submit.evaluate("el => { el.scrollIntoView({ block: 'center', inline: 'center' }); el.click(); }")

        # 9. 等表单关闭（最多 20 秒）
        submit_closed = False
        for _ in range(40):
            await page.wait_for_timeout(500)
            if not await submit.is_visible():
                submit_closed = True
                break
        if not submit_closed:
            # 检查是否有确认弹窗（二次确认）
            confirm = page.get_by_role("button", name="确定", exact=True)
            if await confirm.count() and await confirm.first.is_visible():
                await confirm.first.evaluate("el => el.click()")
                await page.wait_for_timeout(2000)
                if not await submit.is_visible():
                    submit_closed = True
        if not submit_closed:
            # 检查是否有错误提示
            error_selectors = [
                ".ovui-message--error",
                ".ovui-message--warning",
                '[class*="message"][class*="error"]',
                ".ovui-form-item__error",
            ]
            errors = []
            for sel in error_selectors:
                el = page.locator(sel)
                for i in range(await el.count()):
                    try:
                        if await el.nth(i).is_visible():
                            errors.append((await el.nth(i).inner_text())[:200])
                    except Exception:
                        pass
            error_text = "; ".join(errors) if errors else "表单提交后未关闭，无错误提示"
            raise ChaseCreateError(f"追投提交失败: {error_text}")

        # 10. 打开任务中心，核实新任务已创建并回读 task_id
        # 不依赖 task_name 精确匹配（任务中心有多个 tab，且任务名可能抓到残留值）：
        # 先按名找，找不到按「提交前后 5 分钟内创建」兜底；3 轮重试再放弃
        submit_at = datetime.now()
        task_id = ""
        open_error = None
        for _attempt in range(3):
            await _dismiss_banners(page)
            try:
                await _open_chase_task_center(page, rows[0])
                open_error = None
            except Exception as exc:
                open_error = exc
                await page.wait_for_timeout(1000)
                continue
            task_id = await _find_new_task_id(page, task_name, submit_at)
            if task_id:
                break
            await page.wait_for_timeout(1000)

        if not task_id:
            detail = f"；最后一次打开任务中心失败: {open_error}" if open_error else ""
            raise ChaseCreateError(
                f"任务中心未找到新追投任务（task_name={task_name or '未取到'}），"
                f"创建结果未确认，请在千川页面手动核实{detail}"
            )

        return {
            "success": True,
            "task_id": task_id or None,
            "plan_id": resolved_plan_id,
            "message": f"追投任务创建成功" + (f"，task_id={task_id}" if task_id else "，但未能提取到 ID"),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="创建素材追投任务")
    parser.add_argument("action", choices=["create"], help="操作")
    parser.add_argument("--creator", help="抖音号（与 --plan-id 二选一）")
    parser.add_argument("--plan-id", help="主计划 ID（与 --creator 二选一）")
    parser.add_argument("--budget", type=int, default=100, help="追投预算")
    parser.add_argument("--hours", type=float, default=24.0, help="追投时长（小时）")
    parser.add_argument("--department", default=DEFAULT_DEPARTMENT, choices=tuple(ACCOUNT_BY_DEPARTMENT))
    args = parser.parse_args()

    if not args.creator and not args.plan_id:
        print(json.dumps({"status": "failed", "message": "Provide --creator or --plan-id"}, ensure_ascii=False))
        return 1

    started = time.monotonic()
    try:
        result = asyncio.run(create_chase(args.creator, args.plan_id, args.department, args.budget, args.hours))
        elapsed = round(time.monotonic() - started, 2)
        output = {"status": "ok" if result["success"] else "failed", **result, "elapsed_seconds": elapsed}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if result["success"] else 1
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        print(json.dumps({"status": "failed", "message": str(exc), "elapsed_seconds": elapsed}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
