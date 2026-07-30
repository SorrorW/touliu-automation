"""Prepare a verified Qianchuan (巨量千川) session in QQBrowser via CDP.

Single entry point — launch (or reuse) QQBrowser with CDP port 9222,
navigate to Qianchuan, select a department account, and verify the URL.

Dependency: pip install "playwright>=1.60,<2"
Usage:     python session_prepare.py --department 业务3部
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QQ_BROWSER = Path(r"D:\QQBrowser\QQBrowser.exe")
CDP_URL = "http://127.0.0.1:9222/json/version"
CDP_BASE_URL = "http://127.0.0.1:9222"
CDP_ARGUMENTS = ["-sc=desktopshortcut", "-fixlaunch=0", "--remote-debugging-port=9222"]

HOME_URL = "https://qianchuan.jinritemai.com/"
OVERALL_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
TOTAL_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 0.5
BROWSER_START_TIMEOUT_SECONDS = 8.0

# 业务部 / 商品 / 达人等账号相关配置统一放在仓库根目录 config.local.json，
# 模板见 config.example.json（config.local.json 已被 .gitignore 排除）。
try:
    from touliu_config import load_touliu_config
except ImportError:  # 允许在包目录内直接运行本脚本
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from touliu_config import load_touliu_config

_TOULIU_CFG = load_touliu_config()

# 业务部 → 千川账号显示名
ACCOUNT_BY_DEPARTMENT: dict[str, str] = dict(_TOULIU_CFG.account_by_department)

# 商品名称 → 千川商品 ID（增删改在 config.local.json 的 products 里）
PRODUCT_MAP: dict[str, str] = dict(_TOULIU_CFG.product_map)

# 业务部 → 千川账号 ID（aavid）
AAVID_BY_DEPARTMENT: dict[str, str] = dict(_TOULIU_CFG.aavid_by_department)


def lookup_product(key: str) -> str | None:
    """商品名称/ID → 千川商品 ID。已知 ID 则原样返回。"""
    if key in PRODUCT_MAP:
        return PRODUCT_MAP[key]
    if key in {v: k for k, v in PRODUCT_MAP.items()}:
        return key  # 已经是 ID
    return None


def _account_id(account: str) -> str:
    """Map display account name to platform numeric account ID."""
    return _TOULIU_CFG.account_id_by_name[account]


# ---------------------------------------------------------------------------
# CDP / Browser lifecycle
# ---------------------------------------------------------------------------


class SessionPrepareError(RuntimeError):
    pass


def _probe_cdp() -> str | None:
    try:
        with urlopen(CDP_URL, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    browser = str(payload.get("Browser", ""))
    ws_url = str(payload.get("webSocketDebuggerUrl", ""))
    return browser if browser and ws_url.startswith("ws://127.0.0.1:9222/") else None


def _qqbrowser_process_exists() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq QQBrowser.exe", "/NH"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    return "QQBrowser.exe" in result.stdout


def _stop_qqbrowser_processes() -> None:
    subprocess.run(
        ["taskkill", "/IM", "QQBrowser.exe", "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )


def _launch_qqbrowser() -> None:
    if not QQ_BROWSER.is_file():
        raise SessionPrepareError(f"QQBrowser not found: {QQ_BROWSER}")
    subprocess.Popen(
        [str(QQ_BROWSER), *CDP_ARGUMENTS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _ensure_cdp(deadline: float) -> tuple[str, bool]:
    """Ensure CDP port 9222 is responsive. Returns (browser_version, was_launched)."""
    existing = _probe_cdp()
    if existing:
        return existing, False

    if _qqbrowser_process_exists():
        _stop_qqbrowser_processes()
        kill_deadline = min(deadline, time.monotonic() + 3.0)
        while _qqbrowser_process_exists() and time.monotonic() < kill_deadline:
            time.sleep(0.1)

    _launch_qqbrowser()
    start_deadline = min(deadline, time.monotonic() + BROWSER_START_TIMEOUT_SECONDS)
    while time.monotonic() < start_deadline:
        browser = _probe_cdp()
        if browser:
            return browser, True
        time.sleep(0.2)

    raise SessionPrepareError("QQBrowser CDP port 9222 did not become available within 20 s")


# ---------------------------------------------------------------------------
# Playwright page operations
# ---------------------------------------------------------------------------


async def _connected_qianchuan_page(browser: Browser) -> Page:
    if not browser.contexts:
        raise SessionPrepareError("QQBrowser CDP has no browser context")
    context = browser.contexts[0]
    pages = [p for p in context.pages if "qianchuan" in p.url]
    return pages[0] if pages else await context.new_page()


async def _select_department(page: Page, account: str, deadline: float) -> bool:
    card = page.locator(f'.account-list-item:has([title="{account}"])').first
    chooser_deadline = min(deadline, time.monotonic() + 6.0)
    while time.monotonic() < chooser_deadline:
        if await card.count() and await card.is_visible():
            await card.evaluate("el => el.click()")
            return True
        await page.wait_for_timeout(100)
    return False


async def _verify_account(page: Page, account: str, deadline: float) -> None:
    expected_id = _account_id(account)
    while time.monotonic() < deadline:
        if f"aavid={expected_id}" in page.url and "/login" not in page.url:
            return
        await page.wait_for_timeout(100)
    raise SessionPrepareError(
        f"Account verification timed out: expected {account} ({expected_id})"
    )


# ---------------------------------------------------------------------------
# Public API & CLI
# ---------------------------------------------------------------------------


async def ensure_qianchuan_page(page: Page, department: str = "业务3部", timeout: float = 15.0) -> None:
    """Navigate to the overall plan page, handle login/account selection if needed.

    Key: must include ?aavid=<account_id> in the URL, otherwise Qianchuan
    redirects to the login/account-chooser page.

    Flow: 导航到总览页(带aavid) → 如跳转登录则选部门 → 重试直到搜索框出现
    """
    account = ACCOUNT_BY_DEPARTMENT[department]
    aavid = _account_id(account)
    target_url = f"{OVERALL_URL}?aavid={aavid}"
    deadline = time.monotonic() + timeout

    for attempt in range(3):
        await page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
        await page.wait_for_timeout(500)

        if "/login" in page.url:
            found = await _select_department(page, account, deadline)
            if found:
                switch_deadline = min(deadline, time.monotonic() + 5.0)
                while "/login" in page.url and time.monotonic() < switch_deadline:
                    await page.wait_for_timeout(100)
            if "/login" in page.url:
                continue  # try again
            else:
                # Landed on /home, navigate to overall again with aavid
                await page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(500)

        # Now check for search input
        for _ in range(20):
            for sel in [
                'input[placeholder*="抖音号"]',
                'input[placeholder*="计划名称"]',
            ]:
                el = page.locator(sel)
                if await el.count() and await el.first.is_visible():
                    return
            await page.wait_for_timeout(500)
        if attempt < 2:
            await page.wait_for_timeout(1000)
            continue
        break

    raise RuntimeError("Overall plan page did not fully load within timeout")


async def prepare_session(department: str, deadline: float) -> dict[str, object]:
    """Open Qianchuan, select the department account, and verify the session.

    Returns a dict with status, department, account, browser, url, etc.
    """
    account = ACCOUNT_BY_DEPARTMENT[department]
    browser_name, launched = _ensure_cdp(deadline)

    async with async_playwright() as pw:
        try:
            browser = await asyncio.wait_for(
                pw.chromium.connect_over_cdp(CDP_BASE_URL, timeout=5000),
                timeout=max(0.5, deadline - time.monotonic()),
            )
            page = await _connected_qianchuan_page(browser)
            await asyncio.wait_for(
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=6000),
                timeout=max(0.5, deadline - time.monotonic()),
            )
            chooser_found = await _select_department(page, account, deadline)
            if chooser_found:
                switch_deadline = min(deadline, time.monotonic() + 5.0)
                while "/login" in page.url and time.monotonic() < switch_deadline:
                    await page.wait_for_timeout(100)
            await _verify_account(page, account, deadline)
            return {
                "status": "ready",
                "department": department,
                "account": account,
                "browser": browser_name,
                "launched": launched,
                "url": page.url,
            }
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
            raise SessionPrepareError(f"Session preparation timed out: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open Qianchuan and prepare a verified department session."
    )
    parser.add_argument(
        "--department",
        choices=tuple(ACCOUNT_BY_DEPARTMENT),
        default="业务3部",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        result = asyncio.run(
            prepare_session(args.department, started + TOTAL_TIMEOUT_SECONDS)
        )
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SessionPrepareError, ValueError) as exc:
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
