"""授权管理：抖音号授权 + 投放授权（非官方抖音号 · 商品投放权限）。

流程（2026-07 实测摸清）：
  A. 抖音号授权：账户中心 → 抖音号授权 → 添加抖音号
     → 填抖音号 ID + 达人合作码 → 授权类型「合作达人」→ 授权时间「180天」→ 确定。
     合作码即达人同意凭证，提交即生效。
  B. 投放授权：账户中心 → 投放授权 → 非官方抖音号授权管理 → 申请授权
     → 下拉选抖音号 → 选「商品投放权限」→ 提交申请。
     需达人 24h 内在抖音 APP 站内信或其官方千川账户确认后才生效。

入口：账户中心页面带 aavid 直接访问即可，不必走右上角账号浮窗。

注意：达人昵称在提交授权申请前表格里往往还没有行，需在提交后补抓；
平台在「待达人确认」期间可能不渲染昵称，此时只能等生效后再查询回填。

Usage:
    python -m 巨量千川.auth check --creator dou12345678
    python -m 巨量千川.auth authorize --creator dou12345678 --code 10000000001
    python -m 巨量千川.auth authorize --creator dou12345678   # 抖音号授权已生效，只补投放授权时免 --code
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from playwright.async_api import Browser, Page, async_playwright

from .session_prepare import ACCOUNT_BY_DEPARTMENT, _account_id, ensure_qianchuan_page

CDP_URL = "http://127.0.0.1:9222"
DEFAULT_DEPARTMENT = "业务3部"

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


async def _qianchuan_page(browser: Browser) -> Page:
    context = browser.contexts[0]
    pages = [p for p in context.pages if "qianchuan" in p.url]
    return pages[0] if pages else await context.new_page()


async def _open_page(page: Page, department: str, url: str) -> None:
    """保证会话就绪后跳到指定账户中心页面。"""
    await ensure_qianchuan_page(page, department, timeout=20.0)
    await page.goto(
        f"{url}?aavid={_aavid(department)}", wait_until="domcontentloaded", timeout=15000
    )
    await page.wait_for_timeout(2500)


async def _filter_table(page: Page, placeholder: str, creator_id: str) -> None:
    """用列表页搜索框过滤指定抖音号（两个授权页都是回车触发搜索）。"""
    box = page.locator(f'input[placeholder*="{placeholder}"]').first
    if await box.count() and await box.is_visible():
        await box.fill(creator_id)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)


async def _row_status(page: Page, creator_id: str) -> str | None:
    """当前表格中含 ID:{creator_id} 的行状态；任一行授权生效即返回授权生效。

    抖音号授权页表格是 div（.bui-table-tr），投放授权页表格是真 tr（.ovui-tr）。
    传入的是达人昵称而非 ID 时（两个授权页的搜索框都支持按昵称过滤），
    退化为按昵称匹配行——纯数字 ID 不做此退化，避免误匹配被授权账户等数字列。
    """
    rows = page.locator(
        f'.bui-table-tr:has-text("ID:{creator_id}"), tr.ovui-tr:has-text("ID:{creator_id}")'
    )
    if await rows.count() == 0 and not creator_id.isdigit():
        rows = page.locator(
            f'.bui-table-tr:has-text("{creator_id}"), tr.ovui-tr:has-text("{creator_id}")'
        )
    first_text = None
    for i in range(await rows.count()):
        text = " ".join((await rows.nth(i).inner_text()).split())
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
    rows = page.locator(
        f'.bui-table-tr:has-text("ID:{creator_id}"), tr.ovui-tr:has-text("ID:{creator_id}")'
    )
    if await rows.count() == 0 and not creator_id.isdigit():
        rows = page.locator(
            f'.bui-table-tr:has-text("{creator_id}"), tr.ovui-tr:has-text("{creator_id}")'
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


async def _add_aweme_auth(page: Page, creator_id: str, code: str) -> str:
    """添加抖音号弹窗：填抖音号 ID + 合作码 → 合作达人 → 180天 → 确定。"""
    await page.locator('button:has-text("添加抖音号")').first.click()
    modal = page.locator(".add-aweme-modal").first
    await modal.wait_for(state="visible", timeout=8000)

    await modal.locator('input[placeholder*="请输入已认证的抖音号ID"]').fill(creator_id)
    await modal.locator('input[placeholder*="请输入合作码"]').fill(code)
    await modal.locator('span.bui-radio-input-label:text-is("合作达人")').click()
    await modal.locator('span.bui-radio-input-label:text-is("180天")').click()

    # 勾选底部《商业合作授权协议》，否则「确定」是禁用态
    await modal.locator(".byted-checkbox .bui-checkbox-input-wrapper").first.click()
    await page.wait_for_timeout(300)

    await modal.locator('button:has-text("确定")').click()
    try:
        await modal.wait_for(state="hidden", timeout=20000)
    except Exception as exc:
        toast = await _toast_text(page)
        detail = await _modal_error(modal)
        raise AuthError(
            f"添加抖音号失败：{toast or detail or '弹窗未关闭，可能有校验错误/网络超时'}"
        ) from exc
    toast = await _toast_text(page)
    return toast or "添加抖音号已提交"


# ---------------------------------------------------------------------------
# B. 投放授权（非官方抖音号 · 商品投放权限）
# ---------------------------------------------------------------------------


async def _apply_toufang_auth(page: Page, creator_id: str) -> str:
    """申请授权弹窗：下拉选抖音号 → 商品投放权限 → 提交申请。"""
    await page.locator('button:has-text("申请授权")').first.click()
    modal = page.locator(".account-uni-apply-auth-modal").first
    await modal.wait_for(state="visible", timeout=8000)

    # 下拉数据可能有同步延迟，重试几次
    option = None
    for attempt in range(5):
        await modal.locator('input[placeholder*="请选择要申请"]').click()
        await page.wait_for_timeout(1500)
        opt = page.locator(f'.ovui-option:has-text("ID:{creator_id}")').first
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

    # 默认选中「直播投放权限」，切成「商品投放权限」
    await page.locator('.ovui-radio-item:has-text("商品投放权限")').first.click()
    await page.wait_for_timeout(300)

    await page.locator('.ovui-modal button:has-text("提交申请")').first.click()
    try:
        # 弹窗关闭后用 select 输入框是否还在 DOM 判断
        await modal.locator('input[placeholder*="请选择要申请"]').wait_for(
            state="hidden", timeout=20000
        )
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
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = await _qianchuan_page(browser)

        await _open_page(page, department, AWEME_AUTH_URL)
        await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
        douyin_status = await _row_status(page, creator_id)
        creator_name = await _row_name(page, creator_id)

        await _open_page(page, department, TOUFANG_AUTH_URL)
        await page.locator("text=非官方抖音号授权管理").first.click()
        await page.wait_for_timeout(2000)
        await _filter_table(page, "输入抖音号名称/ID回车搜索", creator_id)
        toufang_status = await _row_status(page, creator_id)
        creator_name = creator_name or await _row_name(page, creator_id)

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
    creator_id: str, code: str, department: str = DEFAULT_DEPARTMENT
) -> dict:
    """执行抖音号授权 + 投放授权申请。已生效的步骤自动跳过。"""
    steps: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = await _qianchuan_page(browser)

        # A. 抖音号授权
        await _open_page(page, department, AWEME_AUTH_URL)
        await _filter_table(page, "输入抖音号ID/抖音昵称", creator_id)
        douyin_status = await _row_status(page, creator_id)
        creator_name = await _row_name(page, creator_id)
        if douyin_status in _PENDING_DOUYIN:
            steps.append(f"抖音号授权状态：{douyin_status}，跳过")
        else:
            if not code:
                raise AuthError(
                    f"{creator_id} 抖音号授权未提交，执行授权需要提供 --code 达人合作码"
                )
            steps.append(await _add_aweme_auth(page, creator_id, code))
            await page.wait_for_timeout(1500)
            douyin_status = await _row_status(page, creator_id) or "已提交"
            # 提交后补抓昵称：全新达人提交前表格无行，上面抓不到
            creator_name = creator_name or await _row_name(page, creator_id)

        # B. 投放授权
        await _open_page(page, department, TOUFANG_AUTH_URL)
        await page.locator("text=非官方抖音号授权管理").first.click()
        await page.wait_for_timeout(2000)
        await _filter_table(page, "输入抖音号名称/ID回车搜索", creator_id)
        toufang_status = await _row_status(page, creator_id)
        creator_name = creator_name or await _row_name(page, creator_id)
        if toufang_status in _PENDING_TOUFANG:
            steps.append(f"投放授权状态：{toufang_status}，跳过")
        else:
            steps.append(await _apply_toufang_auth(page, creator_id))
            await page.wait_for_timeout(1500)
            toufang_status = await _row_status(page, creator_id) or "待达人确认"
            # 提交后补抓昵称（同上）
            creator_name = creator_name or await _row_name(page, creator_id)

    return {
        "creator": creator_id,
        "creator_name": creator_name,
        "department": department,
        "douyin_auth": douyin_status,
        "toufang_auth": toufang_status,
        "success": douyin_status == STATUS_OK,
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
                authorize_creator(args.creator, args.code, args.department)
            )
            ok = result["success"]
    except AuthError as exc:
        result, ok = {"success": False, "message": str(exc)}, False

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
