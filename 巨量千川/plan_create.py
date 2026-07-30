"""创建巨量千川投放计划（适配 2026 新 UI）。

新版千川 UI 变化：
- 总览页入口：从"新建计划"改为"商品乘方"
- 所有操作在同一页面完成（不再开新标签页）
- 商品列表使用卡片结构（.card-product-item）
- 选达人使用右侧抽屉（.ovui-drawer）
- 素材改为智能优选自动选择

Usage:
    python -m 巨量千川.plan_create \
        --product 商品A --creator 12345678A --department 业务3部

    # 填写并发布
    python -m 巨量千川.plan_create \
        --product 商品A --creator 12345678A --department 业务3部 \
        --publish
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .plan_metrics import CDP_URL as CDP_BASE_URL, _ensure_cdp, _parse_plan_row, _search_plans
from .session_prepare import ACCOUNT_BY_DEPARTMENT, ensure_qianchuan_page, lookup_product


HOME_URL = "https://qianchuan.jinritemai.com/"
OVERALL_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
CREATE_URL = "https://qianchuan.jinritemai.com/uni-creation/overall-product-multi"


class PlanCreateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanRequest:
    product_name: str
    creator_id: str
    department: str
    roi: float = 7.3
    daily_budget: int = 300
    creator_name: str | None = None
    seller: str | None = None          # 业务员姓氏，用于计划名
    plan_name: str | None = None
    select_first_video: bool = False
    publish: bool = False
    confirm_risky: bool = False


@dataclass
class WorkflowResult:
    status: Literal[
        "prepared",
        "publish_confirmed",
        "failed",
    ]
    account: str
    product_id: str
    creator_id: str
    plan_name: str | None = None
    plan_id: str | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# 页面工具
# ---------------------------------------------------------------------------


async def _body_text(page: Page) -> str:
    return await page.locator("body").evaluate("e => e.innerText")


async def _click_btn(page: Page, text: str, *, timeout: float = 8.0) -> None:
    """点击包含指定文本的可见按钮（force 防止 Drawer 遮挡问题）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        buttons = page.get_by_role("button")
        for i in range(await buttons.count()):
            b = buttons.nth(i)
            if await b.is_visible() and text in (await b.inner_text()):
                await b.click(force=True)
                return
        await asyncio.sleep(0.2)
    raise PlanCreateError(f"按钮未找到或不可点击: {text}")


async def _wait_stable(page: Page, seconds: float = 1.0) -> None:
    await page.wait_for_timeout(int(seconds * 1000))


def build_plan_name(request: PlanRequest, extracted_creator_name: str = "", today: date | None = None) -> str | None:
    """生成计划名称。优先 --plan-name，其次 MMDD_达人显示名_姓。
    
    extracted_creator_name: 从千川抽屉提取的达人显示名（如"示例达人"），
                           当 request.creator_name 未提供时作为后备。
    """
    if request.plan_name:
        return request.plan_name
    creator = request.creator_name or extracted_creator_name
    seller = request.seller
    if not creator and not seller:
        return None
    if not creator or not seller:
        return None
    run_date = today or date.today()
    name = creator.lstrip("@").strip()
    surname = seller.strip()[0] if seller.strip() else ""  # 业务员只取姓
    if not name or not surname:
        return None
    return f"{run_date:%m%d}_{name}_{surname}"


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------


async def _enter_create_page(page: Page, department: str) -> None:
    """从总览页点击「商品乘方」进入创建页。"""
    await ensure_qianchuan_page(page, department, timeout=15.0)

    # 点击"商品乘方"按钮——在新 UI 中代替了旧的"新建计划"
    # 兼容精确/模糊匹配以及可能的子元素排版
    clicked = False
    for exact in [True, False]:
        try:
            chengfang = page.get_by_text("商品乘方", exact=exact).first
            await chengfang.wait_for(state="visible", timeout=6000)
            await chengfang.click(force=True)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        # 兜底：用 JS 点击页面中可见的包含"商品乘方"的按钮
        clicked = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const b of btns) {
                if (b.offsetParent !== null && b.innerText.includes('商品乘方')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        if not clicked:
            body = await page.locator("body").evaluate("e => e.innerText")
            raise PlanCreateError(f"未找到「商品乘方」按钮。页面文本片段: {body[:300]}")

    await _wait_stable(page, 4.0)

    if CREATE_URL not in page.url:
        raise PlanCreateError(
            f"点击「商品乘方」后未能跳转到创建页，当前 URL: {page.url}"
        )


async def _select_product(page: Page, product_id: str) -> None:
    """商品选择：添加商品 → 搜索 → 勾选（卡片结构）→ 下一步。"""
    # 点击"添加商品"
    await _click_btn(page, "添加商品", timeout=10.0)
    await _wait_stable(page, 1.5)

    # 搜索商品
    search = page.locator('input[placeholder*="商品名称/ID"]').first
    await search.wait_for(state="visible", timeout=8000)
    await search.fill(product_id)
    await search.press("Enter")
    await _wait_stable(page, 1.5)

    # 找到商品卡片并勾选 checkbox (新 UI 使用 .card-product-item)
    card = page.locator(f'.card-product-item:has-text("{product_id}")').first
    if await card.count() == 0:
        # 兜底：直接搜页面文本
        raise PlanCreateError(f"未找到商品卡片 (ID: {product_id})")
    checkbox = card.locator('input[type="checkbox"]').first
    if await checkbox.count() == 0:
        raise PlanCreateError(f"商品卡片中未找到勾选框 (ID: {product_id})")
    if not await checkbox.is_checked():
        await checkbox.check(force=True)
        await _wait_stable(page, 0.3)

    # 点击"下一步"确认商品选择
    await _click_btn(page, "下一步", timeout=5.0)
    await _wait_stable(page, 2.0)


async def _open_creator_drawer(page: Page) -> None:
    """点击「手动添加抖音号」/「添加抖音号」打开右侧抽屉 #1。"""
    clicked = False
    for label in ["手动添加抖音号", "管理手动添加抖音号", "添加抖音号"]:
        try:
            await _click_btn(page, label, timeout=5.0)
            clicked = True
            break
        except PlanCreateError:
            continue

    if not clicked:
        clicked = await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const el of items) {
                if (el.offsetParent !== null) {
                    const text = el.innerText || '';
                    if (text.includes('手动添加抖音号') || text.includes('管理手动添加抖音号') || text.includes('添加抖音号')) {
                        el.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        if not clicked:
            body = await _body_text(page)
            raise PlanCreateError(f"无法点击「添加抖音号」按钮。页面文本片段: {body[:600]}")
    await _wait_stable(page, 2.0)
    # 确认 drawer #1 已打开（任意可见 drawer 即可）
    for i in range(10):
        drawers = page.locator(".ovui-drawer")
        count = await drawers.count()
        for idx in range(count):
            if await drawers.nth(idx).is_visible():
                return
        await _wait_stable(page, 0.5)
    body = await _body_text(page)
    raise PlanCreateError(f"添加抖音号抽屉未在预期时间内打开。页面文本片段: {body[:600]}")


async def _select_creator_in_drawer(page: Page, creator_id: str) -> str:
    """在右侧抽屉 #1 中搜索并选择抖音号，返回达人显示名。

    点击"添加抖音号"后 drawer #1 打开，其中展示了所有已授权的抖音号。
    重要：必须点击 checkbox 的 <label> 元素（而非原生 <input>）才能触发选中。
    Returns: 达人显示名（如"示例达人"），提取失败时返回空字符串。
    """
    # 等待抽屉搜索框出现
    search = page.locator('input[placeholder*="请输入抖音号"]').first
    await search.wait_for(state="attached", timeout=15000)
    await _wait_stable(page, 1.0)

    # 搜索抖音号（兜底处理 input 未完全可见的情况）
    try:
        await search.fill(creator_id)
        await search.press("Enter")
    except Exception:
        # 聚焦后通过键盘输入并回车
        await search.evaluate("el => { el.focus(); el.select(); el.scrollIntoView({block:'center'}); }")
        await page.keyboard.type(creator_id)
        await page.keyboard.press("Enter")
    await _wait_stable(page, 2.0)

    # 确认搜索结果
    body = await _body_text(page)
    if creator_id not in body:
        # 兜底：尝试点击搜索图标
        await page.evaluate("""() => {
            const icons = Array.from(document.querySelectorAll('i, span, svg, button'));
            for (const el of icons) {
                const cls = (el.className || '').toString();
                if (cls.includes('search') || cls.includes('Search') || el.getAttribute('name') === 'search') {
                    el.click();
                    return;
                }
            }
        }""")
        await _wait_stable(page, 2.0)
        body = await _body_text(page)
        if creator_id not in body:
            raise PlanCreateError(f"未搜索到抖音号: {creator_id}。页面文本片段: {body[:800]}")

    # 在 drawer 中找到对应行；若未勾选则点 label 勾选，同时提取达人显示名
    result = await page.evaluate(f"""() => {{
        const drawers = document.querySelectorAll('.ovui-drawer');
        if (drawers.length === 0) return 'err: no drawer';
        const d = drawers[drawers.length - 1];
        const rows = d.querySelectorAll('tr, [class*="row"]');
        for (const row of rows) {{
            if ((row.innerText||'').includes('{creator_id}')) {{
                // 提取达人显示名（行内第一个不含 ID/编辑/删除 的非空文本行）
                const lines = (row.innerText||'').split('\\n').map(l => l.trim()).filter(l => l);
                let displayName = '';
                for (const line of lines) {{
                    if (!line.includes('ID:') && !line.includes('编辑') && !line.includes('删除')
                        && line !== '{creator_id}' && line.length > 0 && line.length < 50) {{
                        displayName = line;
                        break;
                    }}
                }}
                // 行拆分失败时从 cell 结构提取
                if (!displayName) {{
                    const cells = row.querySelectorAll('td, th, [class*="cell"]');
                    for (const cell of cells) {{
                        const t = (cell.innerText||'').trim();
                        if (t && t !== '{creator_id}' && !t.includes('ID:') && !t.includes('编辑') && !t.includes('删除') && t.length < 50) {{
                            displayName = t;
                            break;
                        }}
                    }}
                }}
                // 勾选 checkbox
                const checkbox = row.querySelector('input[type="checkbox"]');
                if (checkbox && checkbox.checked) {{
                    return 'ok:' + displayName;
                }}
                const label = row.querySelector('label.ovui-checkbox');
                if (label) {{ label.click(); return 'ok:' + displayName; }}
                const wrapper = row.querySelector('[class*="wrapper"]');
                if (wrapper) {{ wrapper.click(); return 'ok:' + displayName; }}
                return 'err: found row but no clickable target';
            }}
        }}
        return `err: ${{rows.length}} rows searched, none contain {creator_id}`;
    }}""")
    if not result.startswith('ok'):
        raise PlanCreateError(f"无法勾选抖音号 {creator_id}: {result}")
    display_name = result[3:] if result.startswith('ok:') else ""
    await _wait_stable(page, 0.8)

    # 验证已选中
    sel_text = await page.evaluate("""() => {
        const drawers = document.querySelectorAll('.ovui-drawer');
        if (drawers.length < 2) return '';
        for (const line of drawers[1].innerText.split('\\n')) {
            if (line.includes('当前商品已选') || line.includes('已选')) return line.trim();
        }
        return '';
    }""")
    if "0/30" in sel_text:
        raise PlanCreateError(f"勾选抖音号失败，选中计数不变: {sel_text}")

    # 点击"确定"（在最后一个可见 drawer 中）
    drawers = page.locator(".ovui-drawer")
    confirm_btn = None
    for i in range(await drawers.count() - 1, -1, -1):
        d = drawers.nth(i)
        if await d.is_visible():
            btn = d.get_by_text("确定", exact=True).first
            if await btn.count() and await btn.is_enabled():
                confirm_btn = btn
                break
    if not confirm_btn:
        raise PlanCreateError("未找到「确定」按钮")
    await confirm_btn.click(force=True)
    await _wait_stable(page, 2.0)
    return display_name


async def _add_video_material(page: Page) -> None:
    """添加视频素材：自选素材 → 选中第一个视频 → 确认。

    流程：点击"添加自选素材" → 点击"添加视频" → 视频抽屉打开
          → 点选第一个视频 → 视频抽屉确定 → 素材面板确定
    """
    # 1. 添加自选素材
    try:
        await _click_btn(page, "添加自选素材", timeout=8.0)
    except PlanCreateError:
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                if (b.offsetParent !== null && b.innerText.includes('添加自选素材')) {
                    b.click(); return;
                }
            }
        }""")
    await _wait_stable(page, 2.0)

    # 2. 点击添加视频打开视频抽屉
    try:
        await _click_btn(page, "添加视频", timeout=8.0)
    except PlanCreateError:
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                if (b.offsetParent !== null && b.innerText.trim() === '添加视频') {
                    b.click(); return;
                }
            }
        }""")
    await _wait_stable(page, 3.0)

    # 3. 点击第一个视频卡片（必须用 Playwright 原生 .click()，JS click 不触发选中）
    card = page.locator('[data-auto-id="create-material-card"]').first
    if await card.count() == 0:
        raise PlanCreateError("视频抽屉中未找到可选的视频卡片")
    await card.click()
    await _wait_stable(page, 0.5)

    # 验证已选中
    selected = await page.evaluate("""() => {
        const drawers = document.querySelectorAll('.ovui-drawer');
        for (let i = drawers.length - 1; i >= 0; i--) {
            const text = drawers[i].innerText;
            if (text.includes('已选择')) {
                for (const line of text.split('\\n')) {
                    if (line.includes('已选择')) return line.trim();
                }
            }
        }
        return '';
    }""")
    if "1/500" not in selected and "选择 1" not in selected:
        raise PlanCreateError(f"视频卡片点击后未选中: {selected}")

    # 4. 找视频抽屉（包含"已选择"文本的）→ 点「确定」
    video_confirm = page.locator(".ovui-drawer").filter(has_text="已选择").get_by_text("确定", exact=True).first
    if await video_confirm.count() == 0:
        # 兜底：用最后一个 drawer
        video_confirm = page.locator(".ovui-drawer").last.get_by_text("确定", exact=True).first
    if await video_confirm.count() and await video_confirm.is_enabled():
        await video_confirm.click(force=True)
        # 等待视频抽屉关闭
        for _ in range(15):
            await _wait_stable(page, 0.2)
            closed = True
            for i in range(await page.locator(".ovui-drawer").count()):
                d = page.locator(".ovui-drawer").nth(i)
                if await d.is_visible() and "已选择" in await d.inner_text():
                    closed = False
                    break
            if closed:
                break
        await _wait_stable(page, 1.0)

    # 5. 素材面板「确定」（关闭自选素材抽屉）
    material_confirm = page.locator(".ovui-drawer").filter(has_text="添加自选素材").get_by_text("确定", exact=True).first
    if await material_confirm.count() == 0:
        material_confirm = page.get_by_text("确定", exact=True).first
    if await material_confirm.count() and await material_confirm.is_visible():
        await material_confirm.click(force=True)
        await _wait_stable(page, 1.5)


async def _fill_delivery_fields(page: Page, request: PlanRequest, extracted_creator_name: str = "") -> str | None:
    """填写 ROI、日预算、计划名称。
    
    extracted_creator_name: 从抽屉提取的达人显示名，当 request.creator_name 未提供时回填。
    """
    # ROI 和预算的输入框 placeholder="请输入"（第一个 ROI，第二个预算）
    inputs = page.locator('input[placeholder="请输入"]')
    if await inputs.count() < 2:
        raise PlanCreateError("未找到 ROI/日预算输入框")

    await inputs.nth(0).fill(str(request.roi))
    await inputs.nth(1).fill(str(request.daily_budget))

    # 计划名称：若用户未提供达人显示名，用抽屉中提取的名称
    name_input = page.locator('input[placeholder*="计划名称"]').first
    plan_name = build_plan_name(request, extracted_creator_name)
    if plan_name:
        if await name_input.count() == 0:
            raise PlanCreateError("未找到计划名称输入框")
        await name_input.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        await name_input.fill(plan_name)
        await name_input.press("Tab")
        await page.wait_for_timeout(300)
        # 验证已填写
        actual = await name_input.input_value()
        if actual != plan_name:
            raise PlanCreateError(f"计划名称填写后未生效: 预期={plan_name}, 实际={actual}")
    return plan_name or (await name_input.input_value() if await name_input.count() else None)


async def _click_modal_confirm(page: Page) -> bool:
    """点击当前弹窗/对话框中的主按钮（非「取消」）。"""
    return await page.evaluate("""() => {
        // 优先在 role="dialog" 或 modal 类容器中找按钮
        const dialogs = document.querySelectorAll('[role="dialog"], .ovui-modal, .oc-modal, [class*="modal"], [class*="dialog"]');
        for (const d of dialogs) {
            if (d.offsetParent === null) continue;
            const btns = Array.from(d.querySelectorAll('button'));
            const visible = btns.filter(b => b.offsetParent !== null);
            if (visible.length >= 2) {
                for (const b of visible) {
                    const text = (b.innerText || '').trim();
                    if (text !== '取消' && text !== 'Cancel') {
                        b.click();
                        return true;
                    }
                }
            } else if (visible.length === 1) {
                visible[0].click();
                return true;
            }
        }
        // 兜底：任意可见的「确认/确定」按钮
        for (const b of document.querySelectorAll('button')) {
            const text = (b.innerText || '').trim();
            if (b.offsetParent !== null && (text === '确认' || text === '确定')) {
                b.click();
                return true;
            }
        }
        return false;
    }""")


async def _toggle_oc_switch(page: Page, label: str, turn_on: bool, scope: str | None = None) -> str:
    """点击可见的 .oc-switch 开关，将其置为指定状态，并自动处理二次确认弹窗。"""
    for _ in range(3):
        result = await page.evaluate(
            """(args) => {
                const label = args.label;
                const turnOn = args.turnOn;
                const scopeSelector = args.scope;

                function findSwitch() {
                    let scope = document;
                    if (scopeSelector === '__last_drawer__') {
                        const drawers = document.querySelectorAll('.ovui-drawer');
                        if (drawers.length) scope = drawers[drawers.length - 1];
                    } else if (scopeSelector) {
                        const el = document.querySelector(scopeSelector);
                        if (el) scope = el;
                    }
                    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_ELEMENT);
                    let node;
                    while (node = walker.nextNode()) {
                        const text = (node.innerText || '').trim();
                        if (text.includes(label)) {
                            // 优先点击内部真正的 .ovui-switch，没有再用外层 .oc-switch
                            let sw = node.querySelector('.ovui-switch') || node.querySelector('.oc-switch');
                            if (sw) return sw;
                            let root = node;
                            for (let i = 0; i < 5 && root; i++) {
                                sw = root.querySelector('.ovui-switch') || root.querySelector('.oc-switch');
                                if (sw) return sw;
                                root = root.parentElement;
                            }
                            if (node.parentElement) {
                                const sibs = node.parentElement.querySelectorAll('.ovui-switch, .oc-switch');
                                if (sibs.length) return sibs[0];
                            }
                        }
                    }
                    return null;
                }

                function readState(sw) {
                    if (sw.getAttribute('aria-checked') === 'true') return true;
                    const cls = (sw.className || '').toString();
                    if (cls.includes('ovui-switch--checked') || cls.includes('is-checked') || cls.includes('checked')) return true;
                    let root = sw;
                    for (let i = 0; i < 3 && root; i++) {
                        const text = (root.innerText || '').trim();
                        if (text.includes('未启用') || text.includes('未开启')) return false;
                        if (text.includes('已启用') || text.includes('已开启')) return true;
                        root = root.parentElement;
                    }
                    return false;
                }

                const sw = findSwitch();
                if (!sw) return 'not_found';
                const isOn = readState(sw);
                if (isOn === turnOn) return 'already_' + (turnOn ? 'on' : 'off');
                sw.scrollIntoView({ block: 'center', inline: 'center' });
                const rect = sw.getBoundingClientRect();
                return {
                    action: isOn ? 'turned_off' : 'turned_on',
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2
                };
            }""",
            {"label": label, "turnOn": turn_on, "scope": scope},
        )

        if result == "not_found":
            return result

        if isinstance(result, dict) and "action" in result:
            await page.mouse.click(result["x"], result["y"])
        elif isinstance(result, str) and result.startswith(("turned_off", "turned_on")):
            return result
        else:
            return str(result)

        # 处理可能弹出的二次确认
        await _wait_stable(page, 0.5)
        await _click_modal_confirm(page)
        await _wait_stable(page, 1.0)

        if isinstance(result, dict):
            return result["action"]
    return "failed"


async def _set_checkbox_card(page: Page, label: str, turn_on: bool) -> str:
    """按文案找到复选卡片（如「含达人佣金出价」），勾选或取消勾选。"""
    for _ in range(3):
        result = await page.evaluate(
            """(args) => {
                const label = args.label;
                const turnOn = args.turnOn;

                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                let node;
                while (node = walker.nextNode()) {
                    const text = (node.innerText || '').trim();
                    if (text.includes(label)) {
                        let root = node;
                        for (let i = 0; i < 5 && root; i++) {
                            const cb = root.querySelector('input[type="checkbox"]');
                            if (cb) {
                                const isOn = !!cb.checked;
                                if (isOn !== turnOn) {
                                    cb.click();
                                    return isOn ? 'turned_off' : 'turned_on';
                                }
                                return 'already_' + (turnOn ? 'on' : 'off');
                            }
                            if (root.className && root.className.toString().includes('checked')) {
                                const isOn = true;
                                if (isOn !== turnOn) {
                                    root.click();
                                    return 'turned_off';
                                }
                                return 'already_on';
                            }
                            root = root.parentElement;
                        }
                        return 'no_control';
                    }
                }
                return 'not_found';
            }""",
            {"label": label, "turnOn": turn_on},
        )

        if result == "not_found":
            return result

        await _wait_stable(page, 0.5)
        await _click_modal_confirm(page)
        await _wait_stable(page, 1.0)

        if result.startswith(("turned_off", "turned_on")):
            return result
    return "failed"


def _plan_id_from_text(text: str, plan_name: str) -> str | None:
    """从成功页文本中提取 plan_id。"""
    match = re.search(rf"{re.escape(plan_name)}\s*(?:\n[^\n]*){{0,2}}\nID[：:]?(\d+)", text)
    return match.group(1) if match else None


async def _verify_published_plan(page: Page, plan_name: str, department: str) -> str | None:
    """在发布后检查页面，提取 plan_id；未检测到成功页则去总览页按名称搜索。"""
    for _ in range(30):
        text = await _body_text(page)
        if "创建成功" in text or "发布成功" in text or "sourceFrom=createSuccess" in page.url:
            if plan_name:
                pid = _plan_id_from_text(text, plan_name)
                if pid:
                    return pid
        await page.wait_for_timeout(1000)

    # 兜底：跳转总览页按名称搜索新发布的计划
    try:
        await ensure_qianchuan_page(page, department, timeout=15.0)
        rows = await _search_plans(page, plan_name, department, by_creator=False)
        for tr in rows:
            metrics = await _parse_plan_row(tr)
            if metrics.plan_name == plan_name and metrics.plan_id:
                return metrics.plan_id
    except Exception:
        pass

    text = await _body_text(page)
    raise PlanCreateError(f"未检测到创建成功页，总览页也未搜索到计划。页面文本摘要: {text[:300]}")


async def create_plan(request: PlanRequest) -> WorkflowResult:
    """执行完整建计划流程（适配 2026 新 UI）。"""
    account = ACCOUNT_BY_DEPARTMENT.get(request.department)
    if not account:
        raise PlanCreateError(f"不支持的部门: {request.department}")

    product_id = lookup_product(request.product_name)
    if not product_id:
        raise PlanCreateError(f"未知商品: {request.product_name}")

    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_BASE_URL, timeout=8000)
        context = browser.contexts[0]

        # 打开新页，导航到千川总览页
        page = await context.new_page()

        # --- 1. 进入创建页（点击商品乘方）---
        await _enter_create_page(page, request.department)

        # --- 模块1：选择商品 ---
        await _select_product(page, product_id)

        # 关闭 AIGC 动态创意（主页面开关）
        await _toggle_oc_switch(page, "AIGC 动态创意", turn_on=False)
        await _wait_stable(page, 1.0)

        # --- 模块2：选择达人（抖音号抽屉）---
        await _open_creator_drawer(page)
        # 进入抽屉后先关闭「联盟合作达人自动投」，弹窗确认后再手动选达人
        await _toggle_oc_switch(page, "联盟合作达人自动投", turn_on=False, scope="__last_drawer__")
        await _wait_stable(page, 2.0)
        extracted_name = await _select_creator_in_drawer(page, request.creator_id)

        # --- 模块3：添加视频素材 ---
        if request.select_first_video:
            await _add_video_material(page)

        # 先取消勾选「含达人佣金出价」，避免页面刷新丢失已填字段
        await _set_checkbox_card(page, "含达人佣金出价", turn_on=False)
        await _wait_stable(page, 0.5)

        # --- 4. 填写投放设置 ---
        plan_name = await _fill_delivery_fields(page, request, extracted_name)

        if not request.publish:
            return WorkflowResult(
                status="prepared",
                account=account,
                product_id=product_id,
                creator_id=request.creator_id,
                plan_name=plan_name,
                message="计划草稿已填好，可人工检查后用 --publish 提交。",
            )

        # --- 5. 发布 ---
        # 页面可能会自动上跳导致发布按钮没点中，尝试最多 3 次：滚动 -> 点击 -> 等待 -> 检测是否还在创建页
        for attempt in range(3):
            publish_btn = page.get_by_role("button", name="发布计划", exact=True).first
            if await publish_btn.count():
                await publish_btn.scroll_into_view_if_needed()
                await publish_btn.click()
            else:
                await _click_btn(page, "发布计划", timeout=10.0)
            await _wait_stable(page, 4.0 + attempt)

            # 风险确认弹窗
            risk_btn = page.get_by_text("确认发布", exact=True).first
            if await risk_btn.count() and await risk_btn.is_visible():
                if not request.confirm_risky:
                    return WorkflowResult(
                        status="prepared",
                        account=account,
                        product_id=product_id,
                        creator_id=request.creator_id,
                        plan_name=plan_name,
                        message="平台弹出风险确认弹窗，未自动确认。可用 --confirm-risky 自动确认。",
                    )
                await risk_btn.click(force=True)
                await _wait_stable(page, 3.0)

            # 如果已经离开创建页或出现成功提示，则跳出重试
            body = await _body_text(page)
            if (
                "创建成功" in body
                or "发布成功" in body
                or "sourceFrom=createSuccess" in page.url
                or CREATE_URL not in page.url
            ):
                break

        plan_id = await _verify_published_plan(page, plan_name or "", request.department)
        return WorkflowResult(
            status="publish_confirmed",
            account=account,
            product_id=product_id,
            creator_id=request.creator_id,
            plan_name=plan_name,
            plan_id=plan_id,
            message="计划已成功发布。",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> PlanRequest:
    parser = argparse.ArgumentParser(description="创建巨量千川投放计划（2026 新版 UI）")
    parser.add_argument("--product", required=True, help="商品名称（如 商品A）")
    parser.add_argument("--creator", required=True, help="抖音号/达人 ID（如 12345678A）")
    parser.add_argument("--department", required=True, choices=list(ACCOUNT_BY_DEPARTMENT), help="业务部")
    parser.add_argument("--roi", type=float, default=7.3, help="综合 ROI 目标（默认 7.3）")
    parser.add_argument("--budget", type=int, default=300, help="日预算（默认 300）")
    parser.add_argument("--creator-name", help="达人显示名，用于生成计划名称（如 示例达人）")
    parser.add_argument("--seller", help="业务员姓氏，用于生成计划名称（如 张）")
    parser.add_argument("--plan-name", help="固定计划名称，覆盖自动生成")
    parser.add_argument("--select-first-video", action="store_true", help="自动选择第一个可用视频")
    parser.add_argument("--publish", action="store_true", help="是否点击发布计划")
    parser.add_argument("--confirm-risky", action="store_true", help="自动确认风险弹窗")
    args = parser.parse_args()

    return PlanRequest(
        product_name=args.product,
        creator_id=args.creator,
        department=args.department,
        roi=args.roi,
        daily_budget=args.budget,
        creator_name=args.creator_name,
        seller=args.seller,
        plan_name=args.plan_name,
        select_first_video=args.select_first_video,
        publish=args.publish,
        confirm_risky=args.confirm_risky,
    )


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(create_plan(args))
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in ("prepared", "publish_confirmed") else 1
    except (PlanCreateError, PlaywrightTimeoutError, ValueError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
