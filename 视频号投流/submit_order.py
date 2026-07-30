"""视频号投流 — 选视频 + 填表 + 选模版 + 勾协议（不点提交，留人工确认）。

完整流程（一次搞定）：
  1. 打开「选择加热对象」弹窗（自动弹或点「添加视频」）→ 清空已选
     → 传 --keyword 则添加其他作者 → 搜视频号 → 点作者卡片
     → 传 --start-date 则设单日筛选 → 选第一条视频 → 点完成
  2. 优先提升目标 → 商品ROI
  3. 出价方式 → 控成本加热
  4. ROI → 7.3
  5. 订单预算 → 300（选商品ROI后页面自动带出，--no-budget 跳过手填）
  6. 加热时长 → 24小时
  7. 加热方案 → 自定义 → 使用模版 → 应用（--no-template 跳过）
  8. 勾选「我已阅读并同意」协议（不点提交）

日常用法（不搜作者，直接用默认作者视频）：
    python -m 视频号投流.submit_order --start-date 2026-07-01 --no-budget

全流程用法（搜作者）：
    python -m 视频号投流.submit_order --keyword 示例达人 --start-date 2026-07-13 --no-budget --template 非示例模板
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .session_prepare import (
    CDP_BASE_URL,
    TARGET_URL,
    SessionPrepareError,
    pick_page,
    prepare_session,
)

try:
    from touliu_config import load_touliu_config
except ImportError:  # 允许在包目录内直接运行本脚本
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from touliu_config import load_touliu_config


def _load_default_template() -> str | None:
    """人群定向模版默认值来自 config.local.json 的 defaults.audience_template。"""
    try:
        return load_touliu_config().audience_template
    except Exception:
        return None


_DEFAULT_TEMPLATE = _load_default_template()


class SubmitOrderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 「选择加热对象」弹窗：搜作者 / 设日期 / 选视频
# ---------------------------------------------------------------------------


async def _is_search_visible(page) -> bool:
    search = page.locator('input[placeholder*="输入视频号"]').first
    return bool(await search.count()) and await search.is_visible()


async def _has_link(page) -> bool:
    link = page.get_by_text("添加其他作者", exact=True).first
    return bool(await link.count()) and await link.is_visible()


async def _ensure_search_panel(page, timeout: float = 15.0) -> None:
    """确保作者搜索面板打开（幂等）：搜索框不可见时点「添加其他作者」。"""
    deadline = time.monotonic() + timeout
    dialog = page.locator("div.finder-ui-desktop-dialog").first
    while time.monotonic() < deadline:
        if await dialog.count() and await dialog.is_visible():
            break
        await page.wait_for_timeout(200)
    else:
        raise SubmitOrderError("「选择加热对象」对话框未出现")

    while time.monotonic() < deadline:
        if await _is_search_visible(page):
            return
        if await _has_link(page):
            link = page.get_by_text("添加其他作者", exact=True).first
            await link.evaluate("e => e.click()")
            await page.wait_for_timeout(1000)
        else:
            await page.wait_for_timeout(300)
    raise SubmitOrderError("无法打开搜索面板")


async def _search_author(page, keyword: str) -> None:
    search = page.locator('input[placeholder*="输入视频号"]').first
    await search.fill(keyword)
    await page.wait_for_timeout(300)
    btn = page.locator("div.finder-ui-desktop-search button").first
    if await btn.count():
        await btn.evaluate("e => e.click()")
    else:
        await search.press("Enter")
    await page.wait_for_timeout(2500)


async def _click_author_card(page, keyword: str, timeout: float = 8.0) -> dict:
    card = page.locator(f'div.option.entry-card:has-text("{keyword}")').first
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await card.count():
            box = await card.bounding_box()
            if box and box.get("width", 0) > 0:
                await card.evaluate("e => e.click()")
                await page.wait_for_timeout(2000)
                return {"keyword": keyword, "clicked": True, "box": box}
        await page.wait_for_timeout(200)
    raise SubmitOrderError(f"未找到匹配的作者卡片: {keyword}")


async def _set_date_range(page, year: int, month: int, day: int) -> dict:
    """点开始日期 → 在弹出的日历上选两次指定日（开始+结束，同一天=单日筛选）。"""
    # 1. 点开始日期输入框打开日历
    start_input = page.locator('input[placeholder="开始日期"]').first
    await start_input.focus()
    await start_input.click(force=True)
    await page.wait_for_timeout(1500)

    # 2. 导航到正确年月
    await _navigate_calendar(page, year, month)

    # 3. 找到 day 元素（非 faded，属于当前月）
    day_el = page.locator(
        'dd.finder-ui-desktop-picker__dd a:not(.finder-ui-desktop-picker__faded)',
        has_text=str(day),
    ).first

    if not await day_el.count():
        raise SubmitOrderError(f"日历上未找到 {year}-{month:02d}-{day:02d}")

    # 4. 第一次点击 → 选开始日期
    await day_el.evaluate("e => e.click()")
    await page.wait_for_timeout(800)
    start_val = await start_input.input_value()
    start_date_fmt = f"{year}-{month:02d}-{day:02d}"
    if start_val != start_date_fmt:
        raise SubmitOrderError(f"开始日期未正确设置: 期望\"{start_date_fmt}\", 得到\"{start_val}\"")

    # 5. 第二次点击 → 选结束日期（同一天）
    await day_el.evaluate("e => e.click()")
    await page.wait_for_timeout(1500)

    end_input = page.locator('input[placeholder="结束日期"]').first
    end_val = await end_input.input_value()
    if end_val != start_date_fmt:
        raise SubmitOrderError(f"结束日期未正确设置: 期望\"{start_date_fmt}\", 得到\"{end_val}\"")

    return {"start": start_val, "end": end_val}


async def _navigate_calendar(page, target_year: int, target_month: int, timeout: float = 10.0) -> None:
    """点击日历 header 的左右箭头，直到显示正确的年月。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        labels = page.locator("span.finder-ui-desktop-picker__panel__label")
        count = await labels.count()
        if count >= 2:
            year_text = (await labels.nth(0).inner_text()).strip()   # "2026年"
            month_text = (await labels.nth(1).inner_text()).strip()  # "07月"
            cur_year = int(year_text.replace("年", ""))
            cur_month = int(month_text.replace("月", ""))
            if cur_year == target_year and cur_month == target_month:
                return

            left = page.locator("button.finder-ui-desktop-btn__icon__left").first
            if (target_year, target_month) < (cur_year, cur_month):
                await left.evaluate("e => e.click()")
            else:
                right = page.locator("button.finder-ui-desktop-btn__icon__right").first
                if await right.count():
                    await right.evaluate("e => e.click()")
                else:
                    raise SubmitOrderError("未找到日历右箭头按钮")

            await page.wait_for_timeout(300)
        else:
            await page.wait_for_timeout(200)
    raise SubmitOrderError(f"日历导航超时: 无法到达 {target_year}-{target_month:02d}")


async def _click_first_video(page, timeout: float = 5.0) -> dict | None:
    """点击第一个可见的 video-card 选中它。返回选中信息或 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        card = page.locator("div.video-card").first
        if await card.count():
            box = await card.bounding_box()
            if box and box.get("width", 0) > 0:
                await card.evaluate("e => e.click()")
                await page.wait_for_timeout(1500)
                # 验证已选择数量
                selected_txt = page.locator("div.finder-ui-desktop-dialog").first
                text = await selected_txt.inner_text()
                for line in text.split("\n"):
                    line = line.strip()
                    if "已选择" in line:
                        return {"selected": line, "box": box}
                return {"selected": "已选择 (unknown)", "box": box}
        await page.wait_for_timeout(200)
    return None


async def _open_video_dialog(page, dialog, timeout: float = 15.0) -> None:
    """确保「选择加热对象」弹窗打开。

    弹窗只在表单没有已选视频时自动弹出；草稿带视频时点「添加视频」手动开。
    页面加载慢时「添加视频」也可能迟迟不渲染，统一在一个轮询里等两者。
    """
    deadline = time.monotonic() + timeout
    clicked = False
    while time.monotonic() < deadline:
        if await dialog.count() and await dialog.is_visible():
            await page.wait_for_timeout(1500)
            return
        if not clicked:
            add = page.get_by_text("添加视频", exact=True).first
            if await add.count() and await add.is_visible():
                await add.evaluate("e => e.click()")
                clicked = True
        await page.wait_for_timeout(400)
    raise SubmitOrderError("「选择加热对象」对话框未出现")


async def _clear_selected_videos(page, dialog, max_rounds: int = 10) -> None:
    """清空弹窗右侧「已选择」面板里的视频（逐个点 remove 图标）。"""
    for _ in range(max_rounds):
        rm = dialog.locator("svg.remove-icon").first
        if not await rm.count():
            return
        try:
            await rm.click(timeout=2000)
        except Exception:
            await rm.evaluate("e => e.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        await page.wait_for_timeout(800)


async def _select_first_and_done(page, dialog) -> dict:
    """在弹窗里选第一条视频并点「完成」关闭，带校验。"""
    video_info = await _click_first_video(page)
    if not video_info:
        raise SubmitOrderError("未能选中视频")
    # 用 page 级 locator，选可见的、在弹窗内的「完成」主按钮
    done_btn = page.locator(
        'div.finder-ui-desktop-dialog button.finder-ui-desktop-btn_primary:visible',
        has_text="完成",
    ).first
    if await done_btn.count() and await done_btn.is_visible():
        await done_btn.click(timeout=10000)
        await page.wait_for_timeout(2500)
    if await dialog.count() and await dialog.is_visible():
        raise SubmitOrderError("点完成后「选择加热对象」弹窗未关闭")
    return video_info


# ---------------------------------------------------------------------------
# 工具：打开 dropdown 并选选项
# ---------------------------------------------------------------------------


async def _open_dropdown(page, label_text: str) -> None:
    """通过可见的 form__control-group 容器定位字段，点它的 dropdown trigger。

    注意：页面隐藏 dialog 里有同名 label 文本，直接 get_by_text 会误命中，
    必须限定在可见的 control-group 容器内找 dt。
    """
    group = page.locator(
        "div.finder-ui-desktop-form__control-group:visible",
        has_text=label_text,
    ).first
    if not await group.count():
        raise SubmitOrderError(f"Form group not found: {label_text}")
    dt = group.locator("dt.finder-ui-desktop-form__dropdown__dt").first
    if not await dt.count():
        raise SubmitOrderError(f"Dropdown trigger not found in group: {label_text}")
    await dt.evaluate("e => e.click()")
    await page.wait_for_timeout(500)


async def _select_dropdown_option(page, option_text: str) -> None:
    """从可见的 dropdown 选项列表中点击指定文本的选项。"""
    option = page.locator(
        ".finder-ui-desktop-dropdown__list-ele:visible",
        has_text=option_text,
    ).first
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if await option.count() and await option.is_visible():
            await option.evaluate("e => e.click()")
            await page.wait_for_timeout(500)
            return
        await page.wait_for_timeout(200)
    raise SubmitOrderError(f"Dropdown option not found: {option_text}")


# ---------------------------------------------------------------------------
# 各字段填充
# ---------------------------------------------------------------------------


async def _set_priority_target(page, target: str = "商品ROI") -> None:
    """设置优先提升目标（dropdown 选择器）。"""
    await _open_dropdown(page, "优先提升目标")
    await _select_dropdown_option(page, target)


async def _set_budget(page, amount: int = 300) -> None:
    """设置订单预算：点「自定义」按钮，输入金额。查找范围限定在订单预算 group 内。"""
    budget_area = page.locator(
        "div.finder-ui-desktop-form__control-group:visible",
        has_text="订单预算",
    ).first
    custom_btn = budget_area.get_by_text("自定义", exact=True).first
    if await custom_btn.count() and await custom_btn.is_visible():
        await custom_btn.evaluate("e => e.click()")
        await page.wait_for_timeout(500)

    inp = budget_area.locator("input:visible").first
    if await inp.count():
        await inp.fill(str(amount))
        await page.wait_for_timeout(300)
    else:
        raise SubmitOrderError("Budget input not found after clicking 自定义")


async def _set_bid_method(page, method: str = "控成本加热") -> None:
    """设置出价方式（dropdown 选择器）。"""
    await _open_dropdown(page, "出价方式")
    await _select_dropdown_option(page, method)


async def _set_roi(page, roi: float = 7.3) -> None:
    """设置目标 ROI。数字输入框（placeholder 如「需在0.5-50之间」），选完 控成本加热 后出现。"""
    # 精确匹配：placeholder 含范围 "0.5" 的输入框
    inp = page.locator("input:visible[placeholder*='0.5']").first
    if await inp.count():
        await inp.fill(str(roi))
        await page.wait_for_timeout(300)
        return

    # fallback: 找其他 ROI 关键字
    for ph_kw in ["投产比", "ROI", "roi", "出价"]:
        inp = page.locator(f"input:visible[placeholder*='{ph_kw}']").first
        if await inp.count():
            await inp.fill(str(roi))
            await page.wait_for_timeout(300)
            return

    raise SubmitOrderError("ROI input not found")


async def _set_duration(page, hours: int = 24) -> None:
    """设置加热时长（dropdown 选择器）。"""
    await _open_dropdown(page, "加热时长")
    await _select_dropdown_option(page, f"{hours}小时")


async def _set_audience_template(page, template: str) -> None:
    """加热方案 → 自定义 → 使用模版 → 选模版卡片 → 应用（当前 UI 无「确定」步）。"""
    group = page.locator(
        "div.finder-ui-desktop-form__control-group:visible",
        has_text="加热方案",
    ).first
    if not await group.count():
        raise SubmitOrderError("Form group not found: 加热方案")

    custom = group.get_by_text("自定义", exact=True).first
    if await custom.count() and await custom.is_visible():
        await custom.evaluate("e => e.click()")
        await page.wait_for_timeout(800)

    use_tmpl = group.get_by_text("使用模版", exact=True).first
    if not (await use_tmpl.count() and await use_tmpl.is_visible()):
        raise SubmitOrderError("「使用模版」入口未出现")
    await use_tmpl.evaluate("e => e.click()")
    await page.wait_for_timeout(1500)

    # 模版卡片（模态框内，卡片标题即模版名）
    card = page.get_by_text(template, exact=True).first
    if not (await card.count() and await card.is_visible()):
        card = page.get_by_text(template).first  # 模版名可能被截断，放宽匹配
    if not (await card.count() and await card.is_visible()):
        raise SubmitOrderError(f"模版未找到: {template}")
    await card.evaluate("e => e.click()")
    await page.wait_for_timeout(800)

    apply_btn = page.get_by_role("button", name="应用").first
    if not (await apply_btn.count() and await apply_btn.is_visible()):
        raise SubmitOrderError("「应用」按钮未找到")
    await apply_btn.evaluate("e => e.click()")
    await page.wait_for_timeout(1200)


async def _check_agreement(page) -> None:
    """勾选「我已阅读并同意」协议（幂等：以 checkbox input 的 checked 为准）。

    页面上有几十个同 class 的 radio label（短视频/预算/扣费方式…），
    必须用 has_text 锁定协议那一条，不能以「提交」按钮状态当判据。
    """
    label = page.locator(
        "label.finder-ui-desktop-form__check-label:visible",
        has_text="我已阅读并同意",
    ).first
    if not await label.count():
        raise SubmitOrderError("协议勾选框未找到")
    inp = label.locator("input[type=checkbox]").first
    if await inp.count() and await inp.is_checked():
        return  # 已勾选
    await label.evaluate("e => e.click()")
    await page.wait_for_timeout(500)
    if await inp.count() and not await inp.is_checked():
        raise SubmitOrderError("协议勾选失败：checkbox 仍未选中")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def submit_order(
    roi: float = 7.3,
    budget: int | None = 300,
    hours: int = 24,
    priority_target: str = "商品ROI",
    bid_method: str = "控成本加热",
    keyword: str | None = None,
    start_date: str | None = None,
    template: str | None = _DEFAULT_TEMPLATE,
) -> dict:
    """完整流程：选作者 → 设日期 → 选视频 → 填表单。

    Args:
        roi: 目标 ROI（默认 7.3）
        budget: 订单预算（默认 300；传 None 跳过——选商品ROI后页面会自动带出 300）
        hours: 加热时长（默认 24）
        priority_target: 优先提升目标（默认 商品ROI）
        bid_method: 出价方式（默认 控成本加热）
        keyword: 作者关键词，传则先搜作者
        start_date: 筛选日期 YYYY-MM-DD（单日）
        template: 人群定向模版名（默认读 config.local.json 的 defaults.audience_template；传 None 跳过模版选择）
    """
    # —— 准备浏览器 ——
    result = await prepare_session()
    if result.get("status") != "ready":
        raise SubmitOrderError(f"Session not ready: {result}")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_BASE_URL, timeout=8000)
        context = browser.contexts[0]
        page = await pick_page(context)

        if TARGET_URL not in page.url:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=15000)

        # —— 选视频预流程 ——
        # 注意：弹窗只在没有已选视频时才自动弹出；草稿带视频时需点「添加视频」手动开。
        dialog = page.locator("div.finder-ui-desktop-dialog").first
        if keyword or start_date:
            await _open_video_dialog(page, dialog)
            # 清空草稿遗留的已选视频，避免新旧视频叠加进同一单
            await _clear_selected_videos(page, dialog)

            if keyword:
                # 添加其他作者 → 搜视频号 → 点作者卡片
                await _ensure_search_panel(page)
                await _search_author(page, keyword)
                await _click_author_card(page, keyword)
            if start_date:
                dt = datetime.date.fromisoformat(start_date)
                await _set_date_range(page, dt.year, dt.month, dt.day)

            await _select_first_and_done(page, dialog)
        else:
            # 无 keyword/date：沿用表单已选视频；新单会自动弹窗，则选第一条
            dlg_deadline = time.monotonic() + 5.0
            while time.monotonic() < dlg_deadline:
                if await dialog.count() and await dialog.is_visible():
                    break
                await page.wait_for_timeout(300)
            if await dialog.count() and await dialog.is_visible():
                await _select_first_and_done(page, dialog)

        # 验证是否在表单页（存在可见的「提交」按钮；页面上有多个隐藏同名节点，.first 常是不可见的）
        submit_btns = page.get_by_text("提交", exact=True)
        submit_visible = False
        for i in range(await submit_btns.count()):
            if await submit_btns.nth(i).is_visible():
                submit_visible = True
                break
        if not submit_visible:
            raise SubmitOrderError("表单页未就绪：找不到「提交」按钮")

        # ===== 填表 =====
        await _set_priority_target(page, priority_target)
        await _set_bid_method(page, bid_method)
        await _set_roi(page, roi)
        if budget is not None:
            await _set_budget(page, budget)
        await _set_duration(page, hours)
        if template:
            await _set_audience_template(page, template)
        await _check_agreement(page)

        return {
            "status": "ok",
            "filled_fields": {
                "priority_target": priority_target,
                "bid_method": bid_method,
                "roi": roi,
                "budget": budget,
                "hours": hours,
                "template": template,
                "agreement": True,
            },
            "url": page.url,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="视频号投流 — 选视频 + 填表单 + 模版 + 协议（不点提交）"
    )
    parser.add_argument("--roi", type=float, default=7.3, help="目标 ROI（默认 7.3）")
    parser.add_argument("--budget", type=int, default=300, help="订单预算（默认 300）")
    parser.add_argument("--no-budget", action="store_true", help="跳过预算设置（选商品ROI后页面自动带 300）")
    parser.add_argument("--hours", type=int, default=24, help="加热时长（默认 24）")
    parser.add_argument("--priority", default="商品ROI", help="优先提升目标")
    parser.add_argument("--bid-method", default="控成本加热", help="出价方式")
    parser.add_argument("--keyword", default=None, help="作者关键词（传此参数则先搜作者）")
    parser.add_argument("--start-date", default=None, help="筛选日期 YYYY-MM-DD")
    parser.add_argument("--template", default=None, help="人群定向模版名（默认读 config.local.json 的 defaults.audience_template）")
    parser.add_argument("--no-template", action="store_true", help="跳过模版选择")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        result = asyncio.run(submit_order(
            roi=args.roi,
            budget=None if args.no_budget else args.budget,
            hours=args.hours,
            priority_target=args.priority,
            bid_method=args.bid_method,
            keyword=args.keyword,
            start_date=args.start_date,
            template=None if args.no_template else (args.template or _DEFAULT_TEMPLATE),
        ))
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SubmitOrderError, SessionPrepareError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": str(exc),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
