"""授权管理：抖音号授权 + 投放授权（非官方抖音号 · 商品投放权限）。

流程（2026-07 实测摸清，2026-08-17 修正）：
  A. 抖音号授权：账户中心 → 抖音号授权 → 添加抖音号
     → 矩阵号固定选择「自运营」；业务上传先选「自运营」，平台不允许时才选「合作达人」。
     → 授权时间「180天」→ 确定。
     合作码即达人同意凭证，提交即生效（个人号仍需达人在抖音APP确认）。
  B. 投放授权：账户中心 → 投放授权 → 非官方抖音号授权管理 → 申请授权
     → 下拉选抖音号 → 选「商品投放权限」→ 提交申请。
     注意：下拉里「待达人确认」的达人也会出现（2026-08-17 用户实测截图证实），
     不必等抖音号授权生效就能提交投放授权申请 —— 两项申请都应尽早提交，
     达人在抖音APP里把两个确认都点掉后一起生效（各 24h 内有效）。

入口：账户中心页面带 aavid 直接访问即可，不必走右上角账号浮窗。

注意：达人昵称在提交授权申请前表格里往往还没有行，需在提交后补抓；
平台在「待达人确认」期间可能不渲染昵称，此时只能等生效后再查询回填。

Usage:
    python -m 巨量千川.auth check --creator dou20161012
    python -m 巨量千川.auth authorize --creator dou20161012 --code 96840444931
    python -m 巨量千川.auth authorize --creator dou20161012   # 抖音号授权已生效，只补投放授权时免 --code
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import Error as _PwError

from .session_prepare import (
    ACCOUNT_BY_DEPARTMENT, _account_id, connect_browser, ensure_qianchuan_page, open_fresh_page,
)

CDP_URL = "http://127.0.0.1:9223"
DEFAULT_DEPARTMENT = "业务3部"
# 外部账号运营矩阵专用部门：矩阵号只允许「自运营」，禁止降级合作达人。
MATRIX_DEPARTMENTS = frozenset({"业务1部-1"})


def auth_mode_for_department(department: str) -> str:
    """Return the authorization policy for a workflow department."""
    return "matrix" if department in MATRIX_DEPARTMENTS else "business"


def auth_type_candidates(auth_mode: str) -> tuple[str, ...]:
    """Return authorization types in the only permitted fallback order.

    2026-09-11 用户改口径：业务上传部门一律「合作达人」——平台新政
    「个人抖音号授权要求更新」落地执行，涨粉不足的号自运营被禁
    （rY0cZV 54382564249 双候选实测均被预检拦），自运营优先只会白烧
    一次失败再回退。矩阵号仍只允许自运营（2026-09-02 起不变）。
    """
    if auth_mode == "matrix":
        return ("自运营",)
    if auth_mode == "business":
        return ("合作达人",)
    raise ValueError(f"未知授权模式: {auth_mode}")

AWEME_AUTH_URL = "https://qianchuan.jinritemai.com/account-center/aweme-auth"
TOUFANG_AUTH_URL = "https://qianchuan.jinritemai.com/account-center/uni-aweme-auth/detail"

STATUS_OK = "授权生效"
# 行文本里可能出现的状态关键词（授权生效已单独判断，勿重复加入）
_KNOWN_STATUSES = ("授权申请失效", "授权申请中", "申请失效", "待达人确认", "待确认", "已解除", "授权失效", "已过期")
# 处于这些状态时视为已经提交/正在生效流程中，不需要重新走授权申请
_PENDING_DOUYIN = (STATUS_OK, "待达人确认", "待确认", "授权申请中")
_PENDING_TOUFANG = (STATUS_OK, "待达人确认", "待确认", "授权申请中", "授权申请待确认")


class AuthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aavid(department: str) -> str:
    return _account_id(ACCOUNT_BY_DEPARTMENT[department])


async def _qianchuan_page(browser: Browser, department: str = "") -> Page:
    """每次开新标签页：复用的旧标签可能被浏览器卡死导航（JS 尚响应但 goto
    永不完成，实测反复出现），新页最稳。用毕由调用方关闭，避免标签堆积；
    开新页前 open_fresh_page 会把积压超上限的旧千川标签从最旧的开始清掉。"""
    if department == "主号":
        from .session_prepare import open_main_account_page
        return await open_main_account_page(browser.contexts[0])
    return await open_fresh_page(browser.contexts[0])


async def _open_page(page: Page, department: str, url: str) -> Page:
    """打开账户中心页，遇到僵尸标签或资源加载超时则换页重试。

    千川账户中心是 SPA，页面主体已可用时偶发有资源长期不结束，不能把
    ``domcontentloaded`` 超时直接当作授权失败。
    """
    target_url = f"{url}?aavid={_aavid(department)}"
    last_error: Exception | None = None
    for attempt in range(3):
        # 主号有显式 account URL，直达账户中心避免每次绕总览页冷加载。
        # 最终仍读平台当前账号 ID；发生登录跳转才走会话选择恢复。
        new_page = page if department == "主号" else await ensure_qianchuan_page(page, department, timeout=20.0)
        if new_page is not page:
            # ensure 换页（僵尸页/账号不符）时旧页会被遗弃成孤儿标签——
            # 2026-09-26 实证：授权失败一次漏一页，三次即顶满 7 页上限。
            try:
                await page.close()
            except Exception:
                pass
        page = new_page
        try:
            # 首次仍等待完整 DOM；超时后的新标签只等导航提交，交给后续定位器等待内容。
            wait_until = "domcontentloaded" if attempt == 0 else "commit"
            await page.goto(target_url, wait_until=wait_until, timeout=15000)
            if department == "主号" and "/login" in page.url:
                page = await ensure_qianchuan_page(page, department, timeout=20.0)
                await page.goto(target_url, wait_until=wait_until, timeout=15000)
            await page.wait_for_timeout(2500)
            if department == "主号":
                from .main_products import verify_account
                await verify_account(page)
            return page
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                raise
            context = page.context
            try:
                await page.close()
            except Exception:
                pass
            if department == "主号":
                from .session_prepare import open_main_account_page
                page = await open_main_account_page(context)
            else:
                page = await open_fresh_page(context)
            await page.wait_for_timeout(500)
    raise last_error or RuntimeError("授权页面打开失败")


# 授权页 SPA 主体渲染慢：头部账户区出来了，搜索框/表格/按钮还没挂载。
# 此时读行会把「授权生效」误判成「未授权」（行不存在→None），点按钮会
# 误报「弹窗未打开」（按钮还没挂载/还没绑处理器）。2026-09-01 三次实测
# （rGT6sK/r3Z3m0/rsW12t）全死在这；探针证实等约 6s 后按钮可点、弹窗
# 正常（.add-aweme-modal 类名未变）。所有读/操作前先过就绪门。
async def _await_anchor(page: Page, selector: str, timeout: float = 10000) -> bool:
    try:
        await page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


_AWEME_ANCHOR = 'input[placeholder*="输入抖音号ID/抖音昵称"], button:has-text("添加抖音号")'
_TOUFANG_ANCHOR = 'input[placeholder*="输入抖音号名称/ID回车搜索"]'


async def _open_aweme_ready(page: Page, department: str) -> Page:
    """打开抖音号授权页并等主体就绪；不就绪整页重载，最多 3 次。"""
    for _ in range(3):
        page = await _open_page(page, department, AWEME_AUTH_URL)
        if await _await_anchor(page, _AWEME_ANCHOR):
            return page
    raise AuthError("抖音号授权页多次重载仍未渲染出搜索框/添加按钮")


async def _open_toufang_ready(page: Page, department: str) -> Page:
    """打开投放授权页+切「非官方抖音号授权管理」tab 并等搜索框就绪，最多 3 次。"""
    for _ in range(3):
        page = await _open_page(page, department, TOUFANG_AUTH_URL)
        await _open_toufang_tab(page)
        if await _await_anchor(page, _TOUFANG_ANCHOR):
            return page
    raise AuthError("投放授权页多次重载仍未渲染出搜索框")


async def _filter_table(page: Page, placeholder: str, creator_id: str) -> None:
    """用列表页搜索框过滤指定抖音号（两个授权页都是回车触发搜索）。"""
    box = page.locator(f'input[placeholder*="{placeholder}"]').first
    try:
        # SPA 渲染慢时搜索框晚到：等 8s 再放弃（放弃=交上层就绪门/复查兜底）
        await box.wait_for(state="visible", timeout=8000)
    except Exception:
        return
    if await box.is_visible():
        await box.fill(creator_id)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)


def _id_rows(page: Page, creator_id: str):
    """按 ID 精确匹配两个授权页的表格行。

    :has-text 是子串匹配：搜「ID:333696111」会同时命中「ID:333696111a」
    （南汇猫蛋 333696111a / 浦东猫蛋 333696111 实测撞车），必须用负向断言
    排除紧跟字母数字的后缀，否则会读到别人的行、甚至给别人提交申请。
    """
    exact = re.compile(rf"ID[:：]\s*{re.escape(creator_id)}(?![0-9A-Za-z_.-])")
    # 2026-09-10 平台改版：两授权页表格统一为 aurora-qc-table（tr.aurora-qc-table-row），
    # 旧 .bui-table-tr / tr.ovui-tr 已零匹配（保留兼容旧账户）。
    return page.locator(".bui-table-tr, tr.ovui-tr, tr.aurora-qc-table-row", has_text=exact)


async def _row_status(page: Page, creator_id: str, scope_keyword: str | None = None, *, exact_only: bool = False) -> str | None:
    """当前表格中含 ID:{creator_id} 的行状态；任一行授权生效即返回授权生效。

    scope_keyword：只统计行文本含该关键词的行。投放授权页同一达人可能有多行
    （如「商品投放」「直播投放」各一行、状态不同），查投放授权时必须传
    scope_keyword="商品投放"，否则别的权限行的「授权生效」会冒充本权限的状态。

    抖音号授权页表格是 div（.bui-table-tr），投放授权页表格是真 tr（.ovui-tr）。
    传入的是达人昵称而非 ID 时（两个授权页的搜索框都支持按昵称过滤），
    退化为按昵称匹配行——纯数字 ID 不做此退化，避免误匹配被授权账户等数字列。
    """
    rows = _id_rows(page, creator_id)
    if await rows.count() == 0 and not creator_id.isdigit() and not exact_only:
        rows = page.locator(
            f'.bui-table-tr:has-text("{creator_id}"), tr.ovui-tr:has-text("{creator_id}"), '
            f'tr.aurora-qc-table-row:has-text("{creator_id}")'
        )
    first_text = None
    for i in range(await rows.count()):
        text = " ".join((await rows.nth(i).inner_text()).split())
        if scope_keyword and scope_keyword not in text:
            continue
        if STATUS_OK in text:
            return STATUS_OK
        if first_text is None:
            first_text = text
    if first_text is None:
        return None
    # 提取状态关键词；行文本很长（含被授权账户等），不能直接截断返回
    for status in _KNOWN_STATUSES:
        if status in first_text:
            return status
    return first_text[-40:]


async def _row_name(page: Page, creator_id: str) -> str | None:
    """从匹配行提取达人昵称（行文本为「昵称 ID:xxx …」结构，取 ID: 前的最后一行）。"""
    rows = _id_rows(page, creator_id)
    if await rows.count() == 0 and not creator_id.isdigit():
        rows = page.locator(
            f'.bui-table-tr:has-text("{creator_id}"), tr.ovui-tr:has-text("{creator_id}"), '
            f'tr.aurora-qc-table-row:has-text("{creator_id}")'
        )
    for i in range(await rows.count()):
        text = (await rows.nth(i).inner_text()).strip()
        m = re.search(r"ID[:：]", text)
        if not m:
            continue
        before = [ln.strip() for ln in text[: m.start()].split("\n") if ln.strip()]
        if before:
            return before[-1]
    return None


async def _open_toufang_tab(page: Page) -> None:
    """切到「非官方抖音号授权管理」tab（DIV.header-tab 叶子节点，等待渲染后再点）。"""
    tab = page.locator('.header-tab:has-text("非官方抖音号授权管理")').first
    try:
        await tab.wait_for(state="visible", timeout=10000)
        await tab.click()
    except Exception:
        # 兜底：JS 找文本精确相等的可见叶子节点点击（页面结构微调时也能中）
        await page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('*'))
                .find(e => e.children.length === 0
                    && (e.innerText || '').trim() === '非官方抖音号授权管理'
                    && e.offsetParent !== null);
            if (el) el.click();
        }""")
    await page.wait_for_timeout(2000)


async def _toast_text(page: Page) -> str:
    for sel in (".byted-message", '[class*="message__content"]', '[class*="toast"]'):
        el = page.locator(sel).last
        try:
            if await el.count() and await el.is_visible():
                return (await el.inner_text()).strip()[:120]
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# A. 抖音号授权
# ---------------------------------------------------------------------------


async def _modal_error(modal) -> str:
    """从仍然打开的弹窗里尽量抓出错误/校验文字。"""
    try:
        text = (await modal.inner_text()).replace("\n", " ")
    except Exception:
        text = ""
    # 常见的错误提示：红色文字、表单下错误提示等
    for keyword in ("错误", "失败", "校验", "请填写", "请输入", "请选择", "不一致", "已存在"):
        idx = text.find(keyword)
        if idx != -1:
            return text[max(0, idx - 30): idx + 60]
    return text[:120].strip()


async def _modal_any_visible_index(modal, field_sel: str = "") -> int:
    """返回第一个可见且（可选）已挂载表单字段的匹配下标；无匹配返回 -1。

    2026-09-10 实证：aurora 弹窗 DOM 首个匹配是隐藏的 aurora-qc-modal-root
    占位节点（visible=False），可见实例排在其后——.first 绑死隐藏节点是
    「弹窗未打开」12 连误报的根因。且弹窗壳先可见、表单字段异步后挂载
    （rvI8go 投放授权弹窗 nth(1) 可见但下拉框未渲染，填字段撞墙）——
    所以「开好了」的判定必须钉在表单字段上，不能只看壳。
    """
    try:
        for i in range(await modal.count()):
            try:
                el = modal.nth(i)
                if not await el.is_visible():
                    continue
                if field_sel and not await el.locator(field_sel).count():
                    continue
                return i
            except Exception:
                continue
    except Exception:
        pass
    return -1


async def _resolve_visible_modal(page: Page, modal_sel: str, field_sel: str = ""):
    """取弹窗里「可见且已挂载表单字段」的实例用于后续填写；兜底退回 .first。"""
    loc = page.locator(modal_sel)
    idx = await _modal_any_visible_index(loc, field_sel)
    return loc.nth(idx) if idx >= 0 else loc.first


async def _click_open_modal(page: Page, button, modal, *, desc: str, field_sel: str = "") -> None:
    """点击按钮并等待弹窗打开；打不开时重试，重试改用 JS click 绕遮挡。

    千川账户中心是 SPA，按钮可见≠点击处理器已绑定，偶发点击被吞、弹窗
    根本不开（2026-08-31 夜间 rdM50N/rsW12t/rek0KO 三条均死在这里，
    单发 8s 等待太脆）。3 次点击全吞时整页重载再来一轮（SPA 处理器
    没绑上就只能靠重渲染,2026-09-03 上午 rNmTvt/rddyio 连烧 6 轮的教训）。
    2026-09-10 根因实证：平台改版后弹窗 DOM 首个匹配是隐藏的
    aurora-qc-modal-root 占位节点，.first 等待它可见=永远等不到，
    「弹窗未打开」12 连误报全是它（弹窗其实秒开）。调用方须传完整
    locator（不要 .first），本函数按「任一可见实例」判定，45s 轮询。
    报错前抓页面 toast + 截图/浮层快照存 logs/auth_debug/，
    便于区分「点了没反应」和「开了别的弹窗」。
    """
    last_error: Exception | None = None
    for round_no in range(2):  # 第 2 轮前先整页重载
        if round_no:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            try:
                await button.wait_for(state="visible", timeout=10000)
            except Exception:
                continue
        for attempt in range(3):
            # 弹窗可能已被上一次点击延迟打开：点前先看，已开直接返回——
            # 多点一次会把正在开的弹窗 toggle 关掉。「开好了」钉在表单字段上
            # （壳先可见、字段异步后挂载，09-10 rvI8go 撞过），首个匹配是隐藏
            # 占位节点，必须按「任一可见且字段就绪的实例」判定。
            if await _modal_any_visible_index(modal, field_sel) >= 0:
                return
            try:
                # 按钮可能还没挂载/绑好处理器（SPA 慢渲染）：先等它真出现再点
                await button.wait_for(state="visible", timeout=8000)
                if attempt == 0 and round_no == 0:
                    await button.click()
                else:
                    await button.evaluate("el => el.click()")
            except Exception:
                pass  # 点击动作失败不急着抛，先看弹窗是否其实还是开了
            # 弹窗/表单挂载可能慢：2s 轮询 45s，不再用 10s 一发定生死
            for _ in range(23):
                if await _modal_any_visible_index(modal, field_sel) >= 0:
                    return
                await page.wait_for_timeout(2000)
    toast = await _toast_text(page)
    extra = f"；页面提示：{toast}" if toast else ""
    # 09-09 起「弹窗未打开」9 连发（selector 烂掉 or 遮挡层？），失败时落现场证据：
    # 截图 + 全部可见浮层清单 + 按钮状态，存 wecom-agent/logs/auth_debug/。
    try:
        import datetime as _dt
        from pathlib import Path as _P
        dbg_dir = _P(__file__).resolve().parent.parent.parent / "wecom-agent" / "logs" / "auth_debug"
        dbg_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=str(dbg_dir / f"{stamp}_{desc}.png"), full_page=False)
        info = await page.evaluate("""() => {
            const out = {url: location.href, title: document.title, overlays: [], buttons: [], inputs: []};
            document.querySelectorAll('div[class*="modal"], div[class*="dialog"], div[class*="drawer"], div[class*="popover"], div[class*="overlay"], div[class*="notification"], div[class*="message"], [role="dialog"]').forEach(el => {
                const r = el.getBoundingClientRect();
                const vis = r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
                if (vis) out.overlays.push({cls: (el.className||'').toString().slice(0,80), text: (el.innerText||'').replace(/\\s+/g,' ').slice(0,120)});
            });
            document.querySelectorAll('button').forEach(b => {
                const t = (b.innerText||'').trim();
                if (t.includes('添加') || t.includes('抖音')) out.buttons.push({text: t.slice(0,30), disabled: b.disabled, visible: b.getBoundingClientRect().width > 0});
            });
            document.querySelectorAll('input[placeholder]').forEach(i => {
                if (i.getBoundingClientRect().width > 0) out.inputs.push(i.placeholder.slice(0,40));
            });
            return out;
        }""")
        (dbg_dir / f"{stamp}_{desc}.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        extra += f"；现场已存 logs/auth_debug/{stamp}_{desc}.png/.json"
    except Exception:
        pass
    raise AuthError(f"{desc}弹窗未打开（点击+整页重载两轮均无响应）{extra}") from last_error


async def _modal_text_leaf_click(modal, text: str) -> None:
    """弹窗内按文本点击单选叶子（兼容 bui/aurora 两套组件库）。

    排除两处同名词陷阱（2026-09-02 探针实测）：授权要求说明浮层（popover）
    和权限对照表（table）里也有「自运营」等文本；优先点 radio 控件内的叶子
    （授权类型真身：bui=span.bui-radio-input-label，aurora=radioDetail 卡）。
    """
    await modal.evaluate("""(root, text) => {
        const bad = (e) => e.closest('[class*="popover"], [class*="popper"], [class*="table"]');
        const leaves = Array.from(root.querySelectorAll('span, div, label'))
            .filter(e => e.children.length === 0
                && (e.innerText || '').trim() === text
                && e.offsetParent !== null
                && !bad(e));
        const el = leaves.find(e => e.closest('[class*="radio"], [class*="Radio"]'))
                   || leaves[0];
        if (el) el.click();
    }""", text)


async def _modal_radio_selected(modal, text: str) -> bool:
    """指定文本的单选是否进入选中态：radio 叶子向上 5 层出现 checked 类。

    bui 选中 = byted-radio-checked，aurora = *-checked，统一 /checked/i。
    禁用单选被点击不会进入选中态——这就是「自运营点不了」的可靠判据。
    """
    try:
        return bool(await modal.evaluate("""(root, text) => {
            const bad = (e) => e.closest('[class*="popover"], [class*="popper"], [class*="table"]');
            const leaf = Array.from(root.querySelectorAll('span, div, label'))
                .filter(e => e.children.length === 0
                    && (e.innerText || '').trim() === text
                    && e.offsetParent !== null
                    && !bad(e))
                .find(e => e.closest('[class*="radio"], [class*="Radio"]'));
            if (!leaf) return false;
            let cur = leaf;
            for (let i = 0; i < 5 && cur; i++) {
                if (/checked/i.test(cur.className || '')) return true;
                cur = cur.parentElement;
            }
            return false;
        }""", text))
    except Exception:
        return False


async def _modal_ok_button_state(modal):
    """返回弹窗「确定」按钮的 (found, disabled)。aurora 渲染成「确 定」(带空格)。"""
    return await modal.evaluate("""(root) => {
        const b = Array.from(root.querySelectorAll('button'))
            .find(b => (b.innerText || '').replace(/\\s+/g, '') === '确定');
        return b ? [true, b.disabled] : [false, true];
    }""")


async def _add_aweme_auth(page: Page, creator_id: str, code: str, *, auth_mode: str = "business") -> str:
    """添加抖音号弹窗：填抖音号 ID + 合作码 → 授权类型 → 180天 → 确定。

    授权类型（2026-09-11 用户改口径）：业务上传直接「合作达人」（平台新政
    拦涨粉不足号的自运营，先试自运营只会白烧一次失败再回退）；矩阵号
    固定「自运营」（2026-09-02 用户定，含「使用抖音号发布作品」权限）。
    弹窗在 bui/aurora 两套组件间随机渲染，选择器全部双兼容；
    aurora 的「确定」渲染成带空格「确 定」。
    候选类型失败时全新开窗重来：提交被拒时弹窗状态已脏，全新开窗最稳。
    """
    # 弹窗选择器必须 div 限定:body 在弹窗打开时带 byted-modal-open 类,
    # 不限定会被 body 抢先命中,导致 hidden 等待永不成立、报错抓整页文本
    # (2026-09-03 rNmTvt/rddyio 首试失败含整页文本即此因)。
    # 2026-09-10 平台改版实证:根类从 .add-aweme-modal 换成 CSS 模块哈希
    # addAwemeModal-xxxx,合作码占位符可能加「达人」前缀——弹窗其实开着却
    # 误报「未打开」,连烧 10 轮。按「根类哈希 + 标题文本 + 宽松占位符」三兜底。
    modal_sel = ('.add-aweme-modal, '
                 'div[class*="addAwemeModal"], '
                 'div[class*="modal"]:has(input[placeholder*="合作码"]), '
                 'div[class*="modal"]:has-text("添加抖音号")')
    # 表格脏数据兜底:抖音号/合作码可能带尾随逗号空格(09-03 rddyio 实测)
    creator_id = creator_id.strip().strip(",").strip()
    code = code.strip().strip(",").strip()

    # 平台预检 pre-check-invite-user 在填号后返回各授权类型的禁用原因
    # (如「涨粉数未满足要求」);提交被前端静默拦截时(点确定弹窗不关、
    # 无 toast、无提交请求)这是唯一可拿到的真因(2026-09-03 rddyio 实测)。
    precheck_reasons: list[str] = []

    def _on_precheck_response(resp):
        if "pre-check-invite-user" not in resp.url:
            return

        async def _grab():
            try:
                data = json.loads(await resp.text())
                result = ((data.get("data") or {}).get("authTargetDisableResult") or {})
                for item in result.values():
                    for reason in (item or {}).get("reason") or []:
                        if reason and reason not in precheck_reasons:
                            precheck_reasons.append(reason)
            except Exception:
                pass

        try:
            asyncio.get_event_loop().create_task(_grab())
        except Exception:
            pass

    page.on("response", _on_precheck_response)

    def _with_precheck(msg: str) -> str:
        if precheck_reasons:
            return f"{msg}；平台预检：{'、'.join(precheck_reasons)}"
        return msg

    last_exc: Exception | None = None
    # 每个候选的失败原因都收集进 attempts（2026-09-04）：此前自运营失败后
    # 回退合作达人，最终只抛出合作达人的异常，自运营那次为什么不行无从查起。
    attempts: list[str] = []
    # 矩阵号严禁降级为合作达人；普通业务上传才允许自运营失败后回退。
    for candidate in auth_type_candidates(auth_mode):
        try:
            await _click_open_modal(
                page,
                page.locator('button:has-text("添加抖音号")').first,
                page.locator(modal_sel),
                desc="添加抖音号",
                field_sel='input[placeholder*="合作码"]',
            )
            modal = await _resolve_visible_modal(page, modal_sel, 'input[placeholder*="合作码"]')

            # 抖音号输入框可能比合作码晚挂载（2026-09-11 rY0cZV 业务3部实测：
            # 合作码就绪门已通过，填抖音号框 30s 超时直接崩出候选循环）。
            # 显式等它可见，等不到按候选失败走 Escape+全新开窗重试，不再崩模块。
            # 09-11 下午 rhcAKw 合作达人回退 10s 不够（开窗动画/异步挂载慢），加宽到 15s。
            try:
                await modal.locator('input[placeholder*="抖音号"]').first.wait_for(
                    state="visible", timeout=15000)
            except _PwError as exc:
                raise AuthError(f"抖音号输入框 15s 未就绪（{type(exc).__name__}）") from exc

            await modal.locator('input[placeholder*="抖音号"]').first.fill(creator_id)
            await modal.locator('input[placeholder*="合作码"]').first.fill(code)
            await page.wait_for_timeout(800)

            await _modal_text_leaf_click(modal, candidate)
            await page.wait_for_timeout(500)
            if not await _modal_radio_selected(modal, candidate):
                raise AuthError(_with_precheck(f"授权类型「{candidate}」点不上（禁用态）"))

            # 180天 + 勾选《商业合作授权协议》(不勾「确定」是禁用态);没解禁再试一轮
            enabled = False
            for _ in range(2):
                await _modal_text_leaf_click(modal, "180天")
                await page.wait_for_timeout(300)
                # 复选框必须点最内层载体:bui 的处理器挂在 .bui-checkbox-input-wrapper
                # 上,外层 .byted-checkbox 不挂(点了不生效,09-03 探针实测「empty」
                # 类不变、确定永不解禁);aurora 则点最深的可见 checkbox 元素。
                await modal.evaluate("""(root) => {
                    let el = root.querySelector('.byted-checkbox .bui-checkbox-input-wrapper');
                    if (!el || !el.offsetParent) {
                        const depth = (x) => { let n = 0, c = x; while (c) { n++; c = c.parentElement; } return n; };
                        el = Array.from(root.querySelectorAll('[class*="checkbox"]'))
                            .filter(e => e.offsetParent !== null)
                            .sort((a, b) => depth(b) - depth(a))[0];
                    }
                    if (el) el.click();
                }""")
                await page.wait_for_timeout(500)
                found, disabled = await _modal_ok_button_state(modal)
                if found and not disabled:
                    enabled = True
                    break
            if not enabled:
                raise AuthError(f"类型「{candidate}」已选但确定未解禁（时间/协议未选全）")

            await modal.evaluate("""(root) => {
                const b = Array.from(root.querySelectorAll('button'))
                    .find(b => (b.innerText || '').replace(/\\s+/g, '') === '确定');
                if (b) b.click();
            }""")
            try:
                await modal.wait_for(state="hidden", timeout=20000)
            except Exception as exc:
                toast = await _toast_text(page)
                detail = await _modal_error(modal)
                raise AuthError(
                    _with_precheck(
                        f"添加抖音号提交被拒（{candidate}）：{toast or detail or '弹窗未关闭，可能有校验错误/网络超时'}"
                    )
                ) from exc
            toast = await _toast_text(page)
            page.remove_listener("response", _on_precheck_response)
            return toast or f"添加抖音号已提交（{candidate}）"
        except AuthError as exc:
            last_exc = exc
            attempts.append(f"「{candidate}」{exc}")
            # 关掉残留弹窗,下一候选全新开窗重来。必须等旧弹窗彻底隐藏:
            # aurora 关窗有动画,旧实例短暂「可见且带字段」,下一候选的
            # 开窗检查会误认它已开好、解析到正在关窗的脏实例上
            # (2026-09-11 rhcAKw 合作达人回退两次撞此,15s 就绪门全灭)。
            try:
                await page.keyboard.press("Escape")
                try:
                    await page.locator(modal_sel).last.wait_for(state="hidden", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(800)
            except Exception:
                pass
        except _PwError as exc:
            # Playwright 自身超时/断连也算候选失败（2026-09-11 起）：此前只接
            # AuthError，页面卡顿导致的 fill/click 超时直接崩出模块，连「换候选
            # 全新开窗再试」的机会都没有（rY0cZV 09:20 单发崩溃即此）。
            last_exc = AuthError(f"页面操作超时/异常：{type(exc).__name__}")
            attempts.append(f"「{candidate}」{type(exc).__name__}: {str(exc)[:120]}")
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(800)
            except Exception:
                pass
    page.remove_listener("response", _on_precheck_response)
    tried = " → ".join(attempts)
    if auth_mode == "matrix":
        raise AuthError(f"矩阵号必须使用自运营授权：{tried or last_exc}（禁止改用合作达人）")
    raise AuthError(f"添加抖音号失败（{tried or last_exc}）")


# ---------------------------------------------------------------------------
# B. 投放授权（非官方抖音号 · 商品投放权限）
# ---------------------------------------------------------------------------


async def _reapply_via_row_button(page: Page, creator_id: str) -> str | None:
    """「申请失效」行优先用行内「重新申请」按钮重提（2026-09-26 乘方实证）。

    乘方迁移账号的顶部「申请授权」弹窗里，手动选完达人再切「商品投放权限」，
    提交按钮会被平台置灰（店铺卡片只读、无任何可补控件）；而行内「重新申请」
    打开的同款弹窗是预填状态（达人+商品投放权限+店铺均已带值），提交直接可点。
    返回 None 表示无失效行可重提，调用方回退顶部弹窗老路径。
    """
    rows = _id_rows(page, creator_id)
    n = await rows.count()
    target = None
    for i in range(n):
        row = rows.nth(i)
        try:
            text = await row.inner_text()
        except Exception:
            continue
        if "商品投放" not in text:
            continue
        btn = row.locator('button:has-text("重新申请"), :text("重新申请")').first
        if await btn.count():
            target = btn
            break
    if target is None:
        return None
    await target.click()
    await page.wait_for_timeout(2500)
    modal_sel = ('.account-uni-apply-auth-modal, '
                 'div[class*="modal"]:has-text("我要申请抖音号")')
    modal = page.locator(modal_sel).last
    submit = modal.locator('button:has-text("提交申请")').first
    # 预填弹窗的提交按钮可能晚一两拍才可用；等 5s，仍置灰说明此路不通，关窗回退
    enabled = False
    for _ in range(10):
        try:
            if await submit.count() and not await submit.evaluate("b => b.disabled"):
                enabled = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(500)
    if not enabled:
        try:
            cancel = modal.locator('button:has-text("取 消"), button:has-text("取消")').first
            if await cancel.count():
                await cancel.click()
                await page.wait_for_timeout(800)
        except Exception:
            pass
        return None
    await submit.click()
    await page.wait_for_timeout(3000)
    toast = await _toast_text(page)
    return f"投放授权已通过行内「重新申请」重提（预填弹窗直接提交）{('；' + toast) if toast else ''}"


async def _apply_toufang_auth(page: Page, creator_id: str, douyin_status: str | None = None) -> str:
    """申请授权弹窗：下拉选抖音号 → 商品投放权限 → 提交申请。

    douyin_status：A 步读到的抖音号授权状态——乘方账号上「待达人确认」的达人
    在选商品投放权限后提交钮被平台置灰，用它把该场景判回「等待达人确认」。"""
    # 弹窗容器双兼容：旧版 .account-uni-apply-auth-modal；2026-09-01 实测业务3部
    # 已切 aurora 新组件（aurora-qc-modal-*，类名里没有旧标识），统一用标题文本锚定。
    # 必须 div 限定:弹窗打开时 body 带 byted-modal-open 类,不限定会被 body 抢先命中。
    modal_sel = ('.account-uni-apply-auth-modal, '
                 'div[class*="modal"]:has-text("我要申请抖音号")')
    # 选择框双兼容：旧版是 input[placeholder*="请选择要申请"]；2026-09-10 实测平台
    # 已换 aurora-qc-select（div 渲染，占位文本节点 .aurora-qc-select-placeholder，
    # 弹层 .aurora-qc-select-dropdown + rc-virtual-list 选项
    # .aurora-qc-select-item-option，弹层内搜索框 placeholder「请输入抖音号名称/ID搜索」不变）。
    field_sel = ('input[placeholder*="请选择要申请"], '
                 '.aurora-qc-select-placeholder')
    await _click_open_modal(
        page,
        page.locator('button:has-text("申请授权")').first,
        page.locator(modal_sel),
        desc="申请授权",
        field_sel=field_sel,
    )
    modal = await _resolve_visible_modal(page, modal_sel, field_sel)

    # 先切「商品投放权限」再搜达人（2026-09-29 用户实证口径）：先选达人（默认直播
    # 投放权限）再切商品权限时，平台不在切换后重新校验，提交钮会一直置灰、店铺
    # 卡片只读；先点商品投放权限，达人选择本身即按商品投放校验，提交才可点。
    # 兼容新旧组件：旧 .ovui-radio-item，新 aurora 分段按钮无固定类名，退化到文本叶子。
    radio = page.locator('.ovui-radio-item:has-text("商品投放权限")')
    if await radio.count():
        await radio.first.click()
    else:
        await page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('*'))
                .filter(e => e.children.length === 0
                    && (e.innerText || '').trim() === '商品投放权限'
                    && e.offsetParent !== null)
                .pop();
            if (el) el.click();
        }""")
    await page.wait_for_timeout(500)

    # 下拉数据可能有同步延迟，重试几次。
    # 选择框点开后，选项是远程搜索的：弹层（body 级 portal .oc-select__popover）里有
    # 独立搜索框「请输入抖音号名称/ID搜索」，必须在那里输 ID 才会加载选项；
    # 弹层外的 .ovui-option 会被页面筛选下拉污染，选项一律限定在 popover 内找。
    # 选项必须按 ID 精确匹配：「待达人确认」的达人也会出现（可正常提交），
    # 子串匹配「ID:333696111」会同时命中「ID:333696111a」，选错人就给别人提交了。
    exact = re.compile(rf"ID[:：]\s*{re.escape(creator_id)}(?![0-9A-Za-z_.-])")
    option = None
    for attempt in range(5):
        # 点开选择框：新组件点占位文本节点，旧组件点 input
        ph = modal.locator('.aurora-qc-select-placeholder:has-text("请选择要申请")')
        if await ph.count():
            await ph.first.click(force=True)
        else:
            await modal.locator('input[placeholder*="请选择要申请"]').click()
        await page.wait_for_timeout(1500)
        try:
            search = page.locator('input[placeholder="请输入抖音号名称/ID搜索"]').first
            await search.wait_for(state="visible", timeout=5000)
            await search.fill(creator_id)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        opt = page.locator(
            ".oc-select__popover .ovui-option, "
            ".aurora-qc-select-dropdown .aurora-qc-select-item-option",
            has_text=exact,
        ).first
        if await opt.count():
            option = opt
            break
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(2000)
    if option is None:
        raise AuthError(
            f"投放授权下拉中未找到 ID:{creator_id}（抖音号授权可能尚未生效或同步延迟）"
        )
    # 选项可能在 DOM 里但不在可见区域（长列表底部），用 JS click 强制点
    await option.evaluate("el => { el.scrollIntoView({block:'nearest'}); el.click(); }")
    await page.wait_for_timeout(800)
    # 新组件是多选，点中后弹层不自动收；点弹窗 body 空白处收起（不收再 Escape），
    # 否则弹层盖住下面的权限项和提交按钮。旧组件单选点完即收，count=0 不进分支。
    dd = page.locator('.aurora-qc-select-dropdown:visible')
    if await dd.count():
        try:
            await modal.locator('.aurora-qc-modal-body').first.click(
                position={"x": 8, "y": 8}, timeout=2000)
        except Exception:
            pass
        await page.wait_for_timeout(600)
        if await dd.count():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)

    # 提交前先等钮可用（最多 6s）：乘方迁移账号对「抖音号授权待达人确认」的达人，
    # 选商品投放权限后平台直接置灰提交钮（前置依赖保护，2026-09-26 ZZ45447 实证：
    # 表单无任何可补控件，硬点只会 30s 超时）——此时应判「等待达人确认」而非失败。
    submit = modal.locator('button:has-text("提交申请")').first
    enabled = False
    for _ in range(12):
        try:
            if await submit.count() and not await submit.evaluate("b => b.disabled"):
                enabled = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(500)
    if not enabled:
        if douyin_status and any(k in douyin_status for k in ("待达人确认", "待确认")):
            raise AuthError(
                f"投放授权提交被平台置灰：{creator_id} 抖音号授权仍「{douyin_status}」，"
                f"待达人确认后自动重试"
            )
        raise AuthError(
            f"{creator_id} 投放授权弹窗「提交申请」被平台置灰"
            f"（已选商品投放权限、店铺卡片只读无可补控件）。"
            f"常见原因：同店商品投放授权被本公司另一部门账户持有"
            f"（平台规则：单店同时段仅可授权一个账户，2026-09-29 Diven96 实证"
            f"挂在业务1部导致业务2部置灰）；另一账户解除后平台有冷却期，"
            f"当日内可能仍置灰，次日重试"
        )
    await submit.click()
    try:
        # 提交成功后整个弹窗卸载。不能用选择框占位节点当关窗信号：
        # 多选选中后占位文本即隐藏，会误判已关窗。
        await modal.wait_for(state="hidden", timeout=20000)
    except Exception as exc:
        toast = await _toast_text(page)
        detail = await _modal_error(modal)
        raise AuthError(
            f"投放授权申请失败：{toast or detail or '弹窗未关闭，可能有网络超时'}"
        ) from exc
    toast = await _toast_text(page)
    return toast or "投放授权申请已提交，待达人 24h 内确认"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_authorization(
    creator_id: str, department: str = DEFAULT_DEPARTMENT
) -> dict:
    """检查达人的抖音号授权和投放授权状态。"""
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await _qianchuan_page(browser, department)
        try:
            page = await _open_aweme_ready(page, department)
            await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
            douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
            creator_name = await _row_name(page, creator_id)
            # 查不到行时先原地等一次、再整页重载重查一次（与 authorize_creator
            # 同一处理）：页面加载抖动会把已生效达人误判成未提交。
            # 2026-09-01 rek0KO 实测：check 读「未授权/未申请」，同分钟
            # authorize 看到「两项均已生效」，误判会烧掉轮询重试次数。
            if not douyin_status:
                await page.wait_for_timeout(3000)
                douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)
            if not douyin_status:
                page = await _open_aweme_ready(page, department)
                await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
                douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)

            page = await _open_toufang_ready(page, department)
            await _filter_table(page, "输入抖音号名称/ID回车搜索", creator_id)
            toufang_status = await _row_status(page, creator_id, scope_keyword="商品投放", exact_only=department == "主号")
            creator_name = creator_name or await _row_name(page, creator_id)
            # 投放授权页同样加二级复查：tab 切换/搜索延迟偶发读不到行
            if not toufang_status:
                await page.wait_for_timeout(3000)
                toufang_status = await _row_status(page, creator_id, scope_keyword="商品投放", exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)
            if not toufang_status:
                page = await _open_toufang_ready(page, department)
                await _filter_table(page, "输入抖音号名称/ID回车搜索", creator_id)
                toufang_status = await _row_status(page, creator_id, scope_keyword="商品投放", exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)
        finally:
            try:
                await page.close()
            except Exception:
                pass
            if department == "主号":
                from .session_prepare import close_main_account_page
                await close_main_account_page()

    authorized = douyin_status == STATUS_OK and toufang_status == STATUS_OK
    return {
        "creator": creator_id,
        "creator_name": creator_name,
        "department": department,
        "douyin_auth": douyin_status or "未授权",
        "toufang_auth": toufang_status or "未申请",
        "authorized": authorized,
        "message": "两项授权均已生效" if authorized else "授权未完成",
    }


async def authorize_creator(
    creator_id: str, code: str, department: str = DEFAULT_DEPARTMENT,
    auth_mode: str | None = None,
) -> dict:
    """执行授权；矩阵号固定自运营，业务上传自 2026-09-11 起直接合作达人。"""
    if auth_mode is None:
        auth_mode = auth_mode_for_department(department)
    if department == "主号" and auth_mode != "business":
        raise ValueError("主号只允许合作达人授权")
    if auth_mode not in {"business", "matrix"}:
        raise ValueError(f"未知授权模式: {auth_mode}")
    if department in MATRIX_DEPARTMENTS and auth_mode != "matrix":
        raise ValueError(f"{department} 是矩阵号专用账号，禁止使用合作达人授权模式")
    steps: list[str] = []
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await _qianchuan_page(browser, department)
        try:
            # A. 抖音号授权
            page = await _open_aweme_ready(page, department)
            await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
            douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
            creator_name = await _row_name(page, creator_id)
            # 查不到行时不要急着「添加抖音号」：页面加载抖动会把已生效的达人
            # 误判成未提交，对个人号发起添加会被平台新政直接拒绝并误标失败。
            # 先原地等一次，再整页重载重查一次，都查不到才按未提交处理。
            if not douyin_status:
                await page.wait_for_timeout(3000)
                douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)
            if not douyin_status:
                page = await _open_aweme_ready(page, department)
                await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
                douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号")
                creator_name = creator_name or await _row_name(page, creator_id)
            if douyin_status in _PENDING_DOUYIN:
                steps.append(f"抖音号授权状态：{douyin_status}，跳过")
            else:
                # 2026-09-04：把「为什么走添加」写进步骤——已生效的号被误判未提交
                # 时会重新发起添加（rKkl1t 案例），没有这行就无法区分是查无此行
                # （SPA 假读）还是状态词不在跳过集合（如已过期/已解除）。
                steps.append(f"抖音号授权状态：{douyin_status or '查无此行（二级复查后仍无记录）'}，执行添加")
                if not code:
                    raise AuthError(
                        f"{creator_id} 抖音号授权未提交，执行授权需要提供 --code 达人合作码"
                    )
                if department == "主号":
                    from .main_products import verify_account
                    await verify_account(page)
                steps.append(await _add_aweme_auth(page, creator_id, code, auth_mode=auth_mode))
                await page.wait_for_timeout(1500)
                douyin_status = await _row_status(page, creator_id, scope_keyword="合作达人" if department == "主号" else None, exact_only=department == "主号") or "授权申请中"
                # 提交后补抓昵称：全新达人提交前表格无行，上面抓不到
                creator_name = creator_name or await _row_name(page, creator_id)

            # B. 投放授权（只认「商品投放」行的状态；「直播投放」行生效不算）
            page = await _open_toufang_ready(page, department)
            await _filter_table(page, "输入抖音号名称/ID回车搜索", creator_id)
            toufang_status = await _row_status(page, creator_id, scope_keyword="商品投放", exact_only=department == "主号")
            creator_name = creator_name or await _row_name(page, creator_id)
            if toufang_status in _PENDING_TOUFANG:
                steps.append(f"投放授权状态：{toufang_status}，跳过")
            else:
                # 「申请失效」的行优先走行内「重新申请」（乘方账号顶部弹窗
                # 手动切商品投放权限会被平台置灰提交钮，2026-09-26 实证）
                if department == "主号":
                    from .main_products import verify_account
                    await verify_account(page)
                reapplied = await _reapply_via_row_button(page, creator_id)
                steps.append(reapplied if reapplied else await _apply_toufang_auth(page, creator_id, douyin_status=douyin_status))
                await page.wait_for_timeout(1500)
                toufang_status = await _row_status(page, creator_id, scope_keyword="商品投放", exact_only=department == "主号") or "待达人确认"
                # 提交后补抓昵称（同上）
                creator_name = creator_name or await _row_name(page, creator_id)
        finally:
            try:
                await page.close()
            except Exception:
                pass
            if department == "主号":
                from .session_prepare import close_main_account_page
                await close_main_account_page()

    # 成功 = 两项授权都已提交（已生效或待达人确认都算）：
    # 「待达人确认」是正常中间态——达人还没在抖音APP点确认，只能等，不是申请失败。
    # 之前 success 只认「授权生效」，导致刚提交完就返回退出码 1，
    # 轮询侧把「授权申请」误判失败白烧重试次数，3 次烧光误标「失败」。
    submitted = (douyin_status in _PENDING_DOUYIN
                 and toufang_status in _PENDING_TOUFANG)
    return {
        "creator": creator_id,
        "creator_name": creator_name,
        "department": department,
        "douyin_auth": douyin_status,
        "toufang_auth": toufang_status,
        "success": submitted,
        "message": "；".join(steps),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="达人授权管理（抖音号授权 + 投放授权）")
    sub = parser.add_subparsers(dest="action", required=True)

    check_p = sub.add_parser("check", help="检查授权状态")
    check_p.add_argument("--creator", required=True, help="抖音号 ID")

    auth_p = sub.add_parser("authorize", help="执行授权（抖音号授权 + 投放授权申请）")
    auth_p.add_argument("--creator", required=True, help="抖音号 ID")
    auth_p.add_argument("--code", help="达人合作码（抖音号授权未生效时必填）")
    auth_p.add_argument("--auth-mode", choices=("business", "matrix"),
                       help="授权策略：business=自运营失败后回退合作达人；matrix=只允许自运营")

    for p in (check_p, auth_p):
        p.add_argument(
            "--department",
            default=DEFAULT_DEPARTMENT,
            choices=tuple(ACCOUNT_BY_DEPARTMENT),
        )

    args = parser.parse_args()
    try:
        if args.action == "check":
            result = asyncio.run(check_authorization(args.creator, args.department))
            ok = result["authorized"]
        else:
            result = asyncio.run(
                authorize_creator(args.creator, args.code, args.department, args.auth_mode)
            )
            ok = result["success"]
    except AuthError as exc:
        result, ok = {"success": False, "message": str(exc)}, False

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
