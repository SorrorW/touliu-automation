"""Prepare a verified 视频号小店推广 session in QQBrowser via CDP.

Adapted from the 巨量千川 session_prepare.py template.
Launches QQBrowser (or reuses an existing one) on CDP port 9222,
then navigates to the 视频号推广创建订单 page.

Dependency: pip install "playwright>=1.60,<2"
Usage:     python session_prepare.py
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

QQ_BROWSER = Path(r"C:\Program Files\Tencent\QQBrowser\QQBrowser.exe")
CDP_URL = "http://127.0.0.1:9222/json/version"
CDP_BASE_URL = "http://127.0.0.1:9222"
CDP_ARGUMENTS = ["-sc=desktopshortcut", "-fixlaunch=0", "--remote-debugging-port=9222"]

TARGET_URL = "https://store.weixin.qq.com/shop/channels-promotion/create-order"
TOTAL_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 0.5
BROWSER_START_TIMEOUT_SECONDS = 8.0


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


async def pick_page(context) -> Page:
    """挑出微信小店页面；没有则复用第二个标签页，再不行就新建。

    QQBrowser 重启后标签页顺序会变（导航页可能插到 store 页前面），
    不能写死 pages[1]，优先按 URL 找。
    """
    for page in context.pages:
        if "store.weixin.qq.com" in page.url:
            return page
    if len(context.pages) > 1:
        return context.pages[1]
    return await context.new_page()


async def _connected_page(browser: Browser) -> Page:
    if not browser.contexts:
        raise SessionPrepareError("QQBrowser CDP has no browser context")
    return await pick_page(browser.contexts[0])


async def _navigate_to_target(page: Page, deadline: float) -> None:
    """Navigate to the 视频号推广-创建订单 page. Handles login redirects if needed."""
    for attempt in range(3):
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(500)

        # Verify we landed on the target page (or already logged in)
        current_url = page.url
        if "channels-promotion" in current_url or "store.weixin.qq.com" in current_url:
            if "/login" not in current_url and "passport" not in current_url:
                return  # success

        if time.monotonic() >= deadline:
            raise SessionPrepareError("Timed out waiting for target page to load")

        if attempt < 2:
            await page.wait_for_timeout(2000)
            continue
        break

    raise SessionPrepareError(
        f"Failed to reach target page after 3 attempts. Current URL: {page.url}"
    )


# ---------------------------------------------------------------------------
# Public API & CLI
# ---------------------------------------------------------------------------


async def prepare_session(deadline: float | None = None) -> dict[str, object]:
    """Open QQBrowser, navigate to 视频号推广创建订单页面, and verify.

    Returns a dict with status, browser, url, etc.
    """
    if deadline is None:
        deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS

    browser_name, launched = _ensure_cdp(deadline)

    async with async_playwright() as pw:
        try:
            browser = await asyncio.wait_for(
                pw.chromium.connect_over_cdp(CDP_BASE_URL, timeout=5000),
                timeout=max(0.5, deadline - time.monotonic()),
            )
            page = await _connected_page(browser)
            await _navigate_to_target(page, deadline)

            return {
                "status": "ready",
                "platform": "视频号投流",
                "browser": browser_name,
                "launched": launched,
                "url": page.url,
                "title": await page.title(),
            }
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
            raise SessionPrepareError(f"Session preparation timed out: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open 视频号推广 and prepare a verified session."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        result = asyncio.run(prepare_session())
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
