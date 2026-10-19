"""创建巨量千川投放计划（适配 2026 新 UI）。

新版千川 UI 变化：
- 总览页入口：从"新建计划"改为"商品乘方"
- 所有操作在同一页面完成（不再开新标签页）
- 商品列表使用卡片结构（.card-product-item）
- 选达人使用右侧抽屉（.ovui-drawer）
- 素材改为智能优选自动选择

Usage:
    python -m 巨量千川.plan_create \
        --product 肥牛 --creator 20161121Z --department 业务3部

    # 填写并发布
    python -m 巨量千川.plan_create \
        --product 肥牛 --creator 20161121Z --department 业务3部 \
        --publish
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .plan_metrics import CDP_URL as CDP_BASE_URL, _ensure_cdp, _parse_plan_row, _search_plans, _wait_plan_table_ready
from .session_prepare import ACCOUNT_BY_DEPARTMENT, _cdp_http_opener, connect_browser, ensure_qianchuan_page, lookup_product, open_fresh_page


HOME_URL = "https://qianchuan.jinritemai.com/"
OVERALL_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
CREATE_URL = "https://qianchuan.jinritemai.com/uni-creation/overall-product-multi"
# 2026-10-13 乘方改版：创建页迁到 create-ad/overall-product（6 步新流程，
# 智能优选素材必选+达人营销三步）。入口跳转目标两版并存，URL 断言要同时认。
CREATE_URL_NEW = "create-ad/overall-product"


class PlanCreateError(RuntimeError):
    pass


class PlanExistsError(PlanCreateError):
    """抽屉勾选禁用且行内显示「已存在商品投放计划」：该商品+抖音号已有计划。

    多为上一轮建计划实际提交成功、但建成验证误报（09-09 rTEAVW/r8MKG1 实证），
    此时应收养现存计划当建成产物，而非按无权限回退授权。
    """

    def __init__(self, creator_display_name: str = ""):
        super().__init__(f"已存在商品投放计划（达人 {creator_display_name or '?'}）")
        self.creator_display_name = creator_display_name


@dataclass(frozen=True)
class PlanRequest:
    product_name: str
    creator_id: str
    department: str
    roi: float = 5.0
    daily_budget: int = 300
    creator_name: str | None = None
    seller: str | None = None          # 业务员姓氏，用于计划名
    plan_name: str | None = None
    select_first_video: bool = False
    publish: bool = False
    confirm_risky: bool = False
    pause_after: bool = False        # 兼容旧参数；当前流程不再暂停新计划
    created_ms: str = ""             # 表格记录创建时间（毫秒），48 小时新计划认定用
    plan_date: str = ""              # 预约投放日期 YYYY-MM-DD，用于计划名前缀
    allow_stale_video: bool = False  # 没投过的记录（旧计划处理匹配 0 条）豁免 48h 双超门


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


def sanitize_plan_name_text(text: str) -> str:
    """剔除千川计划名称不支持的表情/符号，仅用于「实际创建计划」时填入千川。

    UI 上的达人展示名保持原样（可带表情）；只有写进千川的计划名走此清洗。
    保留：各国文字（含中文）、数字、常见标点与空格；
    丢弃：emoji 及其组成件（变体选择符 U+FE0F、零宽连接符 ZWJ、肤色修饰符、
          旗帜区号）、装饰符号（So/Sk 类，如 ★☀✂❤™）与控制/格式符。
    """
    kept = [ch for ch in text if unicodedata.category(ch)[0] in ("L", "N", "P", "Z")]
    cleaned = "".join(kept)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()  # 表情删除后可能留下连续空格
    return cleaned.strip("-_").strip()                 # 去掉首尾残留分隔符


def build_plan_name(request: PlanRequest, extracted_creator_name: str = "", today: date | None = None) -> str | None:
    """生成计划名称。优先 --plan-name，其次 MMDD_达人显示名_姓。

    extracted_creator_name: 从千川抽屉提取的达人显示名（如"肥妈美食"），
                           当 request.creator_name 未提供时作为后备。
    注意：千川计划名称不支持表情符号，达人名/手动指定的名称会在此剔除
         表情后再填入（不影响 UI 展示的达人名）。
    """
    if request.plan_name:
        return sanitize_plan_name_text(request.plan_name) or None
    creator = request.creator_name or extracted_creator_name
    seller = request.seller
    if not creator and not seller:
        return None
    if not creator or not seller:
        return None
    run_date = today or date.today()
    if request.plan_date:
        try:
            run_date = date.fromisoformat(request.plan_date)
        except ValueError:
            # 手动调用传错时回退当天，避免建计划流程直接崩溃。
            pass
    name = sanitize_plan_name_text(creator.lstrip("@").strip())
    surname = seller.strip()[0] if seller.strip() else ""  # 业务员只取姓
    if not name or not surname:
        return None
    return f"{run_date:%m%d}_{name}_{surname}"


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------


async def _enter_create_page(page: Page, department: str) -> Page:
    """从总览页点击「商品乘方」进入创建页。返回实际可用的 Page（僵尸标签可能被换新）。"""
    page = await ensure_qianchuan_page(page, department, timeout=15.0)

    # 2026-10-14 实证：乘方改版公告弹窗（如何使用乘方?/我知道了）遮挡入口按钮，
    # 不点掉的话「商品乘方」点击被拦截、三轮重试全部无效。统一走 popup_killer。
    try:
        from .popup_killer import dismiss_popups
        await dismiss_popups(page)
    except Exception:
        pass

    # 2026-09-10 实测：页面上有两处「商品乘方」文本——真入口是左侧新建区的
    # BUTTON；右侧轮播卡片「商品乘方如何投放」是帮助入口，点了不跳转（纯文本
    # 匹配会撞它，ruPtoX 10:22 就死在这）。限定 button；点击后轮询 URL，
    # 未跳转重试（SPA 水合慢时首次点击可能无效）。
    btn = page.locator('button:has-text("商品乘方")').first
    for _ in range(3):
        try:
            await btn.wait_for(state="visible", timeout=8000)
            await btn.click()
        except Exception:
            pass
        for _ in range(14):
            if CREATE_URL in page.url or CREATE_URL_NEW in page.url:
                await _wait_stable(page, 2.0)
                return page
            await page.wait_for_timeout(1000)

    body = ""
    try:
        body = await page.locator("body").evaluate("e => e.innerText")
    except Exception:
        pass
    raise PlanCreateError(
        f"点击「商品乘方」后未能跳转到创建页，当前 URL: {page.url}；页面片段: {body[:200]}"
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
        # 远程搜索结果到达有延迟：再回车一次并多等一轮（2026-08-17 实测偶发
        # 1.5s 内不出结果，同一商品同一账户几分钟后又正常）
        await search.press("Enter")
        await _wait_stable(page, 3.0)
    if await card.count() == 0:
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


async def _disabled_reason(page: Page, creator_id: str) -> str:
    """勾选禁用时悬停达人行内标签，读平台 popover 的禁用原因（读不到返回 ''）。

    行内红标（如「投放限制」）的 hover popover 才有真实原因文案；只读 DOM
    文本拿不到。2026-09-29 探针实证：mouseenter/mouseover 后可见 popover
    内含「该抖音号暂无商品短视频带货权限」等平台原文。"""
    try:
        hovered = await page.evaluate(f"""() => {{
            const drawers = document.querySelectorAll('.ovui-drawer');
            if (!drawers.length) return false;
            const d = drawers[drawers.length - 1];
            for (const row of d.querySelectorAll('tr')) {{
                if ((row.innerText||'').includes('{creator_id}')) {{
                    const tag = row.querySelector('[data-auto-id="popover-tag"], [data-auto-id="popover-hover-slot"]');
                    if (tag) {{
                        tag.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                        tag.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                        return true;
                    }}
                }}
            }}
            return false;
        }}""")
        if not hovered:
            return ""
        await page.wait_for_timeout(1500)
        text = await page.evaluate("""() => {
            for (const p of document.querySelectorAll('.ovui-popover, [class*="popover"], [class*="tooltip"]')) {
                const style = window.getComputedStyle(p);
                const txt = (p.innerText || '').trim();
                if (txt && style.display !== 'none' && style.visibility !== 'hidden' && !p.className.includes('--hide')) {
                    return txt;
                }
            }
            return '';
        }""")
        # 只取首行原因（次行是「权限开通的条件有哪些？」帮助链接文案）
        first = (text.split("\n")[0] or "").strip()[:120]
        # 过滤消息角标等噪音：2026-10-06 实证 zjb520hj99/25656170876 把
        # 「99+」角标当禁用原因，导致「已有重复计划」被误判成无推广权限
        if not first or re.fullmatch(r"[\d+]+%?", first) or len(first) < 6:
            return ""
        return first
    except Exception:
        return ""


async def _select_creator_in_drawer(page: Page, creator_id: str) -> str:
    """在右侧抽屉 #1 中搜索并选择抖音号，返回达人显示名。

    点击"添加抖音号"后 drawer #1 打开，其中展示了所有已授权的抖音号。
    重要：必须点击 checkbox 的 <label> 元素（而非原生 <input>）才能触发选中。
    Returns: 达人显示名（如"肥妈美食"），提取失败时返回空字符串。
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

    # 2026-09-07 佣金闸：勾选前先读行内带货佣金率，>上限则不投（业务终态）。
    # 读不到佣金率（页面结构变化/联盟合作行）放行+记日志，不误停建计划。
    from .commission_gate import (CommissionBlockedError, is_commission_blocked,
                                  load_commission_max_pct, parse_commission_from_row_text)
    row_text = await page.evaluate(f"""() => {{
        const drawers = document.querySelectorAll('.ovui-drawer');
        if (drawers.length === 0) return '';
        const d = drawers[drawers.length - 1];
        const rows = d.querySelectorAll('tr, [class*="row"]');
        for (const row of rows) {{
            if ((row.innerText||'').includes('{creator_id}')) return row.innerText || '';
        }}
        return '';
    }}""")
    rate = parse_commission_from_row_text(row_text)
    max_pct = load_commission_max_pct()
    if is_commission_blocked(rate, max_pct):
        raise CommissionBlockedError(
            f"佣金过高：{creator_id} 带货佣金率{rate}%＞上限{max_pct}%，按规则不投放")
    if rate is None:
        print(f"[commission] {creator_id} 佣金率未读到，按放行处理")

    # 在 drawer 中找到对应行；若未勾选则点 label 勾选，同时提取达人显示名
    result = await page.evaluate(f"""async () => {{
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
                // 2026-10-13 乘方改版：label 常驻 ovui-checkbox--disabled 样式类
                //（勾上后也不消失），label class 不再代表禁用；真禁用只看 input.disabled。
                if (checkbox && checkbox.disabled) {{
                    // 禁用有两种语义：无推广权限（行内显示无权限/可申请）与
                    // 「商品与该抖音号已存在商品投放计划」（09-08 用户截图 tooltip）。
                    // 后者必须区分出来走收养，不能按无权限回退授权（09-09 误标失败根因）。
                    const rt = ((row.innerText||'') + ' ' +
                        Array.from(row.querySelectorAll('[title]')).map(e => e.title||'').join(' ')
                    ).replace(/\\s+/g, ' ');
                    if (rt.includes('已存在')) return 'exists:' + displayName;
                    return 'err:该抖音号对此商品无推广权限（勾选框被平台禁用，行内显示无权限/可申请）';
                }}
                const label = row.querySelector('label.ovui-checkbox');
                // 2026-10-13 改版：点击后必须回读 input.checked 实证——新 UI
                // 存在点击不注册的命中（嵌套容器行/虚拟列表重渲染）。
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                if (label) {{
                    label.click(); await sleep(400);
                    if (checkbox.checked) return 'ok:' + displayName;
                }}
                const wrapper = row.querySelector('[class*="wrapper"]');
                if (wrapper) {{
                    wrapper.click(); await sleep(400);
                    if (checkbox.checked) return 'ok:' + displayName;
                }}
                if (checkbox) {{
                    checkbox.click(); await sleep(400);
                    if (checkbox.checked) return 'ok:' + displayName;
                }}
                return 'err: 点击未生效（label/wrapper/input 三连点后仍未选中）';
            }}
        }}
        return `err: ${{rows.length}} rows searched, none contain {creator_id}`;
    }}""")
    if result.startswith('exists:'):
        raise PlanExistsError(result[len('exists:'):].strip())
    if not result.startswith('ok'):
        # 勾选禁用时悬停行内标签读平台 popover 的真实原因（如「该抖音号暂无
        # 商品短视频带货权限」）写进报错——投放授权生效≠短视频带货权限，
        # 缺原因会把达人资质问题误判成授权未生效、回退重查死循环
        # （2026-09-29 实证 49332231067：授权生效但行挂「投放限制」红标）。
        if "勾选框被平台禁用" in result:
            reason = await _disabled_reason(page, creator_id)
            if "已存在" in reason:
                # popover 原文说已有计划：走收养路径，绝不能按无权限回退授权
                raise PlanExistsError(creator_id)
            if reason:
                result = f"{result}；平台提示：{reason}"
            else:
                # 读不到真实原因时不得断言「无推广权限」（2026-10-06 实证
                # zjb520hj99 rqJMsk：授权生效、实为已有重复计划，popover
                # 只读到消息角标）。中性措辞避开「无推广权限/勾选框被平台
                # 禁用」关键词，防止 _PERM_BLOCKED 回退授权死循环。
                result = ("勾选禁用原因待人工核查（行内无已存在标记、popover "
                          "未读出原因，可能已有重复计划或无权限）")
        raise PlanCreateError(f"无法勾选抖音号 {creator_id}: {result}")
    display_name = result[3:] if result.startswith('ok:') else ""
    # 验证已选中：2026-10-13 改版后「当前商品已选」计数在抽屉「确定」前不更新，
    # 勾选实证已由 JS 回读 input.checked 完成；计数校验移到确定之后（页面计数）。
    await _wait_stable(page, 0.8)

    # 点击"确定"（在最后一个可见 drawer 中）；
    # 2026-10-13 改版：点完必须验证抽屉真关上，没关就用坐标级点击补（新 UI
    # force click 偶发只注册半次——选择生效但抽屉不关，后续素材抽屉打不开）。
    drawers = page.locator(".ovui-drawer")
    for _ in range(3):
        confirm_btn = None
        for i in range(await drawers.count() - 1, -1, -1):
            d = drawers.nth(i)
            if await d.is_visible():
                btn = d.get_by_role("button", name="确定", exact=True).first
                if await btn.count() and await btn.is_enabled():
                    confirm_btn = btn
                    break
        if not confirm_btn:
            break  # 抽屉已关
        try:
            await confirm_btn.click(timeout=5000)
        except Exception:
            await confirm_btn.click(force=True)
        await _wait_stable(page, 1.5)
        # 2026-10-13 改版：抽屉确定后平台弹「部分商品未选择抖音号，确定提交吗？」
        # 确认模态（再想想/确定）——必须再点模态的「确定」抽屉才真正关闭。
        await page.evaluate("""() => {
          const dialogs = document.querySelectorAll('[role="dialog"], .ovui-modal, [class*="modal"]');
          for (const d of dialogs) {
            if (d.offsetParent === null) continue;
            if (!(d.innerText || '').includes('确定提交')) continue;
            for (const b of d.querySelectorAll('button')) {
              if (b.offsetParent !== null && (b.innerText || '').trim() === '确定') {
                b.click(); return;
              }
            }
          }
        }""")
        await _wait_stable(page, 1.5)
        if await page.locator(".ovui-drawer:visible").count() == 0:
            break
    await _wait_stable(page, 1.0)

    sel_text = await page.evaluate("""() => {
        const lines = document.body.innerText.split('\\n');
        for (const line of lines) {
            if (/已添加[:：]/.test(line)) return line.trim();  // 改版后真计数（10-13 实证）
        }
        return '';
    }""")
    m = re.search(r"已添加[:：]\s*(\d+)\s*/", sel_text)
    if m and int(m.group(1)) == 0:
        raise PlanCreateError(f"勾选抖音号失败，确定后计数仍 0: {sel_text}")
    return display_name


# 素材硬闸（2026-09-23 用户定）：建计划/加素材只允许发布 ≤72 小时的视频；
# 没有合格素材就回写「待业务员补新素材」，绝不拿旧视频夜间投放。
NEW_VIDEO_MAX_AGE_HOURS = 72


def _fresh_video_indexes(video_list: list[dict] | None, now: float | None = None) -> list[int]:
    """返回发布 ≤72h 的视频下标，供建计划和旧计划加素材共用同一硬闸。"""
    now = time.time() if now is None else now
    out: list[int] = []
    for idx, video in enumerate(video_list or []):
        try:
            created = int((video or {}).get("createTime") or 0)
        except (TypeError, ValueError):
            continue
        if created > 0 and 0 <= now - created <= NEW_VIDEO_MAX_AGE_HOURS * 3600:
            out.append(idx)
    return out


def _pick_new_video_index(video_list: list[dict], now: float | None = None) -> int | None:
    """挑首个发布 ≤72h 的视频；没有合格素材返回 None（硬闸，不回退旧视频）。"""
    fresh = _fresh_video_indexes(video_list, now)
    return fresh[0] if fresh else None


async def _add_video_material(page: Page, created_ms: str = "",
                              allow_stale_video: bool = False) -> None:
    """添加视频素材：只选发布 ≤72 小时的视频；没有则硬拦截待业务员补素材。

    ``created_ms`` / ``allow_stale_video`` 为 CLI 兼容参数保留，但不再豁免素材
    新鲜度：用户明确要求夜间不得用超过 72 小时的素材投放。
    """
    # 1. 添加自选素材
    # 2026-10-13 改版：页面上有多个「添加自选素材」按钮，未选达人时的禁用实例
    # 常在前面——必须挑「可见且未禁用」的那个，JS 兜底也要过滤 disabled。
    clicked_material = await page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button'));
        for (const b of btns) {
            if (b.offsetParent !== null && !b.disabled && b.innerText.includes('添加自选素材')) {
                b.scrollIntoView({block: 'center'});
                b.click(); return true;
            }
        }
        return false;
    }""")
    if not clicked_material:
        # 2026-10-15 实证 ruhKDA：达人无该商品挂车视频时平台禁用「添加自选素材」，
        # 素材语义=没素材可加 → 归「待业务员补新素材」，不要落成笼统「失败」。
        raise PlanCreateError("达人当前无该商品挂车视频（添加自选素材按钮被平台禁用），待业务员补新素材")
    await _wait_stable(page, 2.0)

    # 视频抽屉打开时会调 get-items-by-product，响应里带每个视频的 createTime。
    # 必须先挂监听再点「添加视频」，否则可能错过响应。
    items_future: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _capture_items(resp) -> None:
        try:
            body = await resp.json()
        except Exception:
            return
        if not items_future.done():
            items_future.set_result(body)

    def _on_response(resp) -> None:
        if "get-items-by-product" in resp.url:
            asyncio.ensure_future(_capture_items(resp))

    page.on("response", _on_response)

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

    # 3. 按 48 小时规则挑视频卡片（必须用 Playwright 原生 .click()，JS click 不触发选中）
    try:
        payload = await asyncio.wait_for(asyncio.shield(items_future), timeout=15.0)
    except asyncio.TimeoutError as exc:
        raise PlanCreateError("视频列表接口 15s 未返回，无法核验视频发布时间（48 小时规则）") from exc
    finally:
        page.remove_listener("response", _on_response)
    videos = ((payload or {}).get("data") or {}).get("videoList") or []
    if not videos:
        raise PlanCreateError("视频抽屉接口未返回任何视频")
    # 2026-09-08 素材级佣金闸：佣金率标在视频卡上（梦妮 15% 实测），达人维度的
    # 闸读不到。候选顺序沿用 48h 规则（新鲜优先，全不新鲜退回最新一条）；
    # 逐候选读卡面「佣金率:N%」，>上限换下一个，全超标按「佣金过高」拒建。
    from .commission_gate import (CommissionBlockedError, is_commission_blocked,
                                  load_commission_max_pct, parse_commission_from_card_text)
    max_pct = load_commission_max_pct()
    now_ts = time.time()
    fresh: list[int] = []
    for idx, video in enumerate(videos):
        try:
            created = int((video or {}).get("createTime") or 0)
        except (TypeError, ValueError):
            continue
        if created > 0 and 0 <= now_ts - created <= NEW_VIDEO_MAX_AGE_HOURS * 3600:
            fresh.append(idx)
    # 素材硬闸：仅发布 ≤72h 的卡片可进入佣金筛选；无合格素材直接阻止建计划。
    candidates = fresh
    if not candidates and os.environ.get("TOULIU_ALLOW_STALE_ONCE") == "1":
        # 2026-10-06 用户人工指令：「过72小时的，也补一次投放」——仅本次手动
        # 补建放行过期素材（环境变量作用域，正常轮询不带此变量，硬闸不变）。
        # 顺手 dump 全部视频年龄，便于核查「视频很多却判无新素材」类投诉。
        aged = []
        for idx, video in enumerate(videos):
            try:
                aged.append((int(video.get("createTime") or 0), idx))
            except (TypeError, ValueError):
                pass
        for created, idx in sorted(aged, reverse=True):
            print(f"[video] 候选#{idx + 1} 发布 {(now_ts - created) / 3600:.1f} 小时前")
        candidates = [idx for _, idx in sorted(aged, reverse=True)]
        print(f"[video] TOULIU_ALLOW_STALE_ONCE 生效：放行 {len(candidates)} 张过期素材卡（人工补建）")
    if not candidates:
        ages = []
        for video in videos:
            try:
                ages.append((now_ts - int(video.get("createTime") or 0)) / 3600)
            except (TypeError, ValueError):
                pass
        newest_age = min(ages) if ages else None
        suffix = f"（最新素材约 {newest_age:.1f} 小时前）" if newest_age is not None else ""
        raise PlanCreateError(
            f"素材超过72小时：没有发布≤72小时的该商品挂车视频{suffix}，待业务员补新素材")
    cards = page.locator('[data-auto-id="create-material-card"]')
    card_count = await cards.count()
    blocked: list[tuple[int, float]] = []
    pick = None
    for idx in candidates:
        if idx >= card_count:
            continue
        rate = parse_commission_from_card_text(await cards.nth(idx).inner_text())
        if is_commission_blocked(rate, max_pct):
            blocked.append((idx, rate))
            print(f"[commission] 视频卡#{idx + 1} 佣金率{rate}%＞上限{max_pct}%，跳过")
            continue
        pick = idx
        break
    if pick is None:
        if blocked:
            raise CommissionBlockedError(
                f"佣金过高：{len(blocked)} 张候选视频卡佣金率均＞上限{max_pct}%，按规则不建计划")
        raise PlanCreateError(f"视频卡片渲染数量不足：候选 {len(candidates)} 张均无可用卡片")
    # pick 记录所选视频年龄便于追溯
    _age_h = None
    try:
        _age_h = (time.time() - int(videos[pick].get("createTime") or 0)) / 3600
        print(f"[video] 选中第 {pick + 1}/{len(videos)} 条，发布 {_age_h:.1f} 小时前")
    except (TypeError, ValueError):
        pass
    # 选择/加载期间跨过阈值也必须拦截，不能因旧的 allow_stale 参数放行。
    if (_age_h is not None and _age_h > NEW_VIDEO_MAX_AGE_HOURS
            and os.environ.get("TOULIU_ALLOW_STALE_ONCE") != "1"):
        raise PlanCreateError(
            f"素材超过72小时：选中视频发布 {_age_h:.1f} 小时前，待业务员补新素材")
    await cards.nth(pick).click()
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
    # 2026-10-13 乘方改版：ROI 输入框没有 placeholder（在「综合营销ROI目标」
    # 区块内），「请输入」只剩日预算一个。先按新 UI 定位打标，不行回退旧逻辑。
    tagged = await page.evaluate("""() => {
      for (const e of document.querySelectorAll('input')) e.removeAttribute('data-pc-tag');
      const texts = [...document.querySelectorAll('*')].filter(e =>
        e.children.length === 0 && (e.innerText || '').includes('综合营销ROI目标'));
      let roiOk = false, budgetOk = false;
      if (texts.length) {
        let p = texts[0];
        for (let i = 0; i < 7 && p; i++) {
          p = p.parentElement;
          if (!p) break;
          const inp = p.querySelector('input[type=text], input:not([type])');
          if (inp) { inp.setAttribute('data-pc-tag', 'roi'); roiOk = true; break; }
        }
      }
      const budget = [...document.querySelectorAll('input[placeholder="请输入"]')]
        .find(e => e.offsetWidth > 0);
      if (budget) { budget.setAttribute('data-pc-tag', 'budget'); budgetOk = true; }
      return {roiOk, budgetOk};
    }""")
    # 2026-10-15 三部/一部双实证：两个「请输入」并存时就是 旧序[0]=ROI [1]=预算；
    # 只剩一个时 ROI 框无 placeholder，按「综合营销ROI目标」标签爬取。一发 JS 内
    # 现找现填（跨调用打标会被 React 重渲染抹掉，小月亮两连超时实证）。
    filled = await page.evaluate("""(vals) => {
      const setVal = (el, v) => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      const visibles = [...document.querySelectorAll('input[placeholder="请输入"]')]
        .filter(e => e.offsetWidth > 0);
      let roi = null, budget = null;
      if (visibles.length >= 2) {
        roi = visibles[0]; budget = visibles[1];
      } else {
        budget = visibles[0] || null;
        const texts = [...document.querySelectorAll('*')].filter(e =>
          e.children.length === 0 && (e.innerText || '').includes('综合营销ROI目标'));
        if (texts.length) {
          let p = texts[0];
          for (let i = 0; i < 7 && p; i++) {
            p = p.parentElement;
            if (!p) break;
            const inp = p.querySelector('input[type=text], input:not([type])');
            if (inp && inp.offsetWidth > 0) { roi = inp; break; }
          }
        }
      }
      if (!roi || !budget) return 'lost:' + (roi ? '' : 'noRoi ') + (budget ? '' : 'noBudget');
      setVal(roi, vals.roi);
      setVal(budget, vals.budget);
      return (roi.value === vals.roi && budget.value === vals.budget) ? 'ok'
        : 'mismatch:' + roi.value + '/' + budget.value;
    }""", {"roi": str(request.roi), "budget": str(request.daily_budget)})
    if filled == "ok":
        pass
    elif tagged.get("roiOk") or tagged.get("budgetOk"):
        raise PlanCreateError(f"新 UI ROI/预算 JS 填充失败: {filled}")
    else:
        # 旧 UI 兜底（locator fill）
        inputs = page.locator('input[placeholder="请输入"]')
        if await inputs.count() < 2:
            raise PlanCreateError(f"未找到 ROI/日预算输入框（JS 填充也失败: {filled}）")
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


async def _adopt_existing_plan(page: Page, request: PlanRequest,
                               extracted_creator_name: str) -> tuple[str, str] | None:
    """「已存在商品投放计划」时收养现存计划：按期望计划名在总览精确搜索。

    返回 (plan_id, plan_name)；表格就绪但确实没有 → None（不重试）。
    期望名与首轮建计划同名（build_plan_name 复算），只收精确全等匹配，
    避免误收养该达人名下的其他计划。
    """
    expected = build_plan_name(request, extracted_creator_name)
    if not expected:
        return None
    for attempt in range(1, 3):
        try:
            page = await ensure_qianchuan_page(page, request.department, timeout=15.0)
            if not await _wait_plan_table_ready(page, timeout_s=15.0):
                print(f"[adopt] 总览表格未渲染（第{attempt}次），整页重载再试")
                await page.reload()
                await page.wait_for_timeout(2000)
                continue
            rows = await _search_plans(page, expected, request.department, by_creator=False)
            for tr in rows:
                metrics = await _parse_plan_row(tr)
                if metrics is None:  # 畸形行已留 WARNING，跳过
                    continue
                if metrics.plan_name == expected and metrics.plan_id:
                    print(f"[adopt] 收养现存计划: {metrics.plan_name} (ID: {metrics.plan_id})")
                    return metrics.plan_id, metrics.plan_name
            return None
        except Exception as e:
            print(f"[adopt] 收养搜索异常（第{attempt}次）: {e}")
    return None


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

    # 兜底：跳转总览页按名称搜索新发布的计划。
    # 09-09 教训：ensure_qianchuan_page 的 aavid 兜底只认头部账号 ID，页面可能
    # 停在乘方营销首页（表格未渲染），此时搜索静默返回空 → 计划其实已建成却
    # 误报失败（rTEAVW 梨小厨 / r8MKG1 小艾同学 两条实证）。必须先过就绪门，
    # 不就绪整页重载再来一轮；表格就绪但真没搜到才按未找到报错。
    for attempt in range(1, 3):
        try:
            page = await ensure_qianchuan_page(page, department, timeout=15.0)
            if not await _wait_plan_table_ready(page, timeout_s=15.0):
                print(f"[verify] 总览表格未渲染（第{attempt}次），整页重载再试")
                await page.reload()
                await page.wait_for_timeout(2000)
                continue
            rows = await _search_plans(page, plan_name, department, by_creator=False)
            for tr in rows:
                metrics = await _parse_plan_row(tr)
                if metrics is None:  # 畸形行已留 WARNING，跳过
                    continue
                if metrics.plan_name == plan_name and metrics.plan_id:
                    return metrics.plan_id
            break  # 表格就绪但确实搜不到 → 不再重试，走报错
        except Exception as e:
            print(f"[verify] 总览搜索异常（第{attempt}次）: {e}")

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
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]

        # 打开新页，导航到千川总览页（开新页前会清理积压超上限的旧千川标签）
        page = await open_fresh_page(context)

        # --- 1. 进入创建页（点击商品乘方）---
        page = await _enter_create_page(page, request.department)

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
        try:
            extracted_name = await _select_creator_in_drawer(page, request.creator_id)
        except PlanExistsError as e:
            # 「商品与该抖音号已存在商品投放计划」：多为上一轮建计划实际提交成功
            # 但建成验证误报（09-09 rTEAVW 梨小厨 / r8MKG1 小艾同学 实证）。
            # 收养现存计划当建成产物（下游暂停待发/挂票/回写照常），
            # 找不到才报错——绝不能按无权限回退授权（会走授权重提撞死）。
            adopted = await _adopt_existing_plan(page, request, e.creator_display_name)
            if adopted is None:
                raise PlanCreateError(
                    "已存在商品投放计划，但总览页未找到今日同名计划可收养")
            adopted_id, adopted_name = adopted
            return WorkflowResult(
                status="publish_confirmed",
                account=account,
                product_id=product_id,
                creator_id=request.creator_id,
                plan_name=adopted_name,
                plan_id=adopted_id,
                message="平台提示已存在商品投放计划：已收养现存计划（多为上轮建成但验证误报）。",
            )

        # --- 模块3：添加视频素材 ---
        if request.select_first_video:
            await _add_video_material(page, request.created_ms,
                                      request.allow_stale_video)

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
        message = "计划已成功发布。"
        if request.pause_after:
            message += "当前流程已取消建成即暂停；计划保持投放中。"
        return WorkflowResult(
            status="publish_confirmed",
            account=account,
            product_id=product_id,
            creator_id=request.creator_id,
            plan_name=plan_name,
            plan_id=plan_id,
            message=message,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> PlanRequest:
    parser = argparse.ArgumentParser(description="创建巨量千川投放计划（2026 新版 UI）")
    parser.add_argument("--product", required=True, help="商品名称（如 肥牛）")
    parser.add_argument("--creator", required=True, help="抖音号/达人 ID（如 20161121Z）")
    parser.add_argument("--department", required=True, choices=list(ACCOUNT_BY_DEPARTMENT), help="业务部")
    parser.add_argument("--roi", type=float, default=5.0, help="综合 ROI 目标（默认 5.0，2026-10-13 起）")
    parser.add_argument("--budget", type=int, default=300, help="日预算（默认 300）")
    parser.add_argument("--creator-name", help="达人显示名，用于生成计划名称（如 煮妇小承妈）")
    parser.add_argument("--seller", help="业务员姓氏，用于生成计划名称（如 赵）")
    parser.add_argument("--plan-name", help="固定计划名称，覆盖自动生成")
    parser.add_argument("--select-first-video", action="store_true", help="自动选择第一个可用视频")
    parser.add_argument("--publish", action="store_true", help="是否点击发布计划")
    parser.add_argument("--confirm-risky", action="store_true", help="自动确认风险弹窗")
    parser.add_argument("--pause-after", action="store_true",
                        help="旧版兼容参数；当前流程不会暂停新计划")
    parser.add_argument("--created-ms", default="",
                        help="表格记录创建时间（毫秒 epoch），48 小时新计划认定用")
    parser.add_argument("--allow-stale-video", action="store_true",
                        help="没投过的记录（旧计划处理匹配 0 条）豁免 48h 双超门，仍建计划")
    parser.add_argument("--plan-date", default="",
                        help="预约投放日期 YYYY-MM-DD，用于计划名称 MMDD 前缀")
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
        pause_after=args.pause_after,
        created_ms=args.created_ms,
        plan_date=args.plan_date,
        allow_stale_video=args.allow_stale_video,
    )


def _tab_targets() -> dict:
    """CDP /json/list 快照 {targetId: url}（只含 page 类标签；走绕代理 opener）。"""
    try:
        with _cdp_http_opener().open(f"{CDP_BASE_URL}/json/list", timeout=5) as resp:
            items = json.loads(resp.read().decode("utf-8", errors="replace"))
        return {t.get("id"): (t.get("url") or "") for t in items
                if t.get("type") == "page" and t.get("id")}
    except Exception:
        return {}


def _cleanup_new_tabs(before: dict, include_stale_creation: bool) -> None:
    """建计划退出时回收本进程新开的千川标签，防泄漏顶满 7 页上限。

    2026-09-26/29 只按 uni-creation URL 回收，但发布成功后验证兜底会把
    创建页导航去总览页（URL 变了），按 URL 匹配漏收 → 每建成一条留一个
    重复总览标签，9-30 夜 3 条即顶满 7 页整夜停摆。改为 targetId 差集：
    进入前快照，退出时关掉所有本次新开的千川域标签（不限 URL）。
    before 里已存在的标签永不碰；include_stale_creation=True（失败路径）
    额外回收 before 里残留的历史 uni-creation 草稿页。plan_create 由轮询
    以子进程串行调用（单实例锁+浏览器互斥），无并发误关。
    """
    try:
        now = _tab_targets()
        for tid, url in now.items():
            stale_draft = tid in before and include_stale_creation and "uni-creation" in url
            opened_by_us = tid not in before
            if not (opened_by_us or stale_draft):
                continue
            if "qianchuan.jinritemai.com" not in url:
                continue
            try:
                _cdp_http_opener().open(f"{CDP_BASE_URL}/json/close/{tid}", timeout=5).read()
            except Exception:
                pass
    except Exception:
        pass


def main() -> int:
    args = parse_args()
    before = _tab_targets()
    rc = 1
    status = ""
    try:
        result = asyncio.run(create_plan(args))
        status = result.status
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        rc = 0 if result.status in ("prepared", "publish_confirmed") else 1
        return rc
    except (PlanCreateError, PlaywrightTimeoutError, ValueError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        if rc:
            _cleanup_new_tabs(before, include_stale_creation=True)
        elif status == "publish_confirmed":
            _cleanup_new_tabs(before, include_stale_creation=False)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
