"""Prepare a verified 视频号小店推广 session in Microsoft Edge via CDP.

Adapted from the 巨量千川 session_prepare.py template.
Launches Edge (or reuses an existing one) on CDP port 9222,
then navigates to the 视频号推广创建订单 page.

浏览器于 2026-08-17 从 QQBrowser 换成 Edge：与千川自动化共用同一个
Edge 实例和专用用户数据目录（%LOCALAPPDATA%\\TouliuEdgeProfile），
与用户日常 Edge 隔离；首次使用需在浏览器里扫码登录一次微信小店。

Dependency: pip install "playwright>=1.60,<2"
Usage:     python session_prepare.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

try:  # 在 webui 下运行时：日志写入 webui.log（「系统日志」页可见）
    from webui.logging_setup import get_logger
    _log = get_logger("视频号投流")
except ImportError:  # 独立 CLI 运行：退化为标准 logging
    import logging
    _log = logging.getLogger("视频号投流")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Edge 安装路径候选（按优先级；也可用环境变量 EDGE_PATH 指定）
_EDGE_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
# 自动化专用用户数据目录：与千川自动化共用同一个 Edge 实例；
# 与用户日常 Edge 隔离，自动化重启不碰用户浏览器（含登录态，严禁打包带出）。
# 放 LOCALAPPDATA（机器级唯一）：开发树与发布包解析到同一目录，
# 换版本包不用重新扫码，也不会有两套 profile 抢 9222 端口。
EDGE_PROFILE_DIR = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
) / "TouliuEdgeProfile"
CDP_URL = "http://127.0.0.1:9222/json/version"
CDP_BASE_URL = "http://127.0.0.1:9222"
CDP_ARGUMENTS = [
    f"--user-data-dir={EDGE_PROFILE_DIR}",
    "--remote-debugging-port=9222",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",  # 强杀后不弹「恢复页面」气泡
    "--hide-crash-restore-bubble",
]

TARGET_URL = "https://store.weixin.qq.com/shop/channels-promotion/create-order"
TOTAL_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 3.0        # 会话恢复期的浏览器应答慢，0.5s 探活会误判死亡
BROWSER_START_TIMEOUT_SECONDS = 45.0  # 重会话机器 8s 起不来，反复被误杀重启


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
    # 只认 Edge：9222 被迁移前的旧 QQBrowser 或其他程序占用时视为未就绪，
    # 走「按端口杀占用者 → 启动 Edge」的自愈路径
    if browser and "Edg" in browser and ws_url.startswith("ws://127.0.0.1:9222/"):
        return browser
    return None


def _run_powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
        # CREATE_NO_WINDOW：无控制台父进程调用时，缺省会弹出可见黑窗口。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout or ""


def _our_browser_pids() -> list[int]:
    """自动化专用 Edge 实例的 PID（按命令行里的用户数据目录过滤）。

    绝不能 taskkill /IM msedge.exe：用户日常也在用 Edge，按映像名杀会把
    用户自己的浏览器一起杀掉（2026-08-17 换 Edge 时的头号风险）。只杀
    命令行含 edge-profile 路径的进程——那是我们拉起的实例及其子进程。
    """
    marker = str(EDGE_PROFILE_DIR).replace("'", "''")
    out = _run_powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{marker}*' }} "
        "| ForEach-Object { $_.ProcessId }"
    )
    return [int(t) for t in out.split() if t.isdigit()]


def _edge_process_exists() -> bool:
    return bool(_our_browser_pids())


def _stop_edge_processes() -> None:
    """只杀自动化专用 Edge 实例（见 _our_browser_pids），绝不动用户日常 Edge。"""
    for pid in _our_browser_pids():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _kill_port_owner() -> None:
    """杀掉占用 9222 端口的进程（如迁移前残留的 QQBrowser 实例）。

    按端口精确到 PID、不按映像名：不会误伤用户自己开的任何浏览器。
    """
    _run_powershell(
        "Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique "
        "| ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
    )


def _find_edge() -> Path:
    """按优先级定位 Edge：环境变量 EDGE_PATH → 常见安装路径。"""
    env = os.environ.get("EDGE_PATH")
    candidates = [env] if env else []
    candidates += list(_EDGE_BROWSER_CANDIDATES)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise SessionPrepareError(
        "未找到 Microsoft Edge，请先安装；非默认路径可用环境变量 EDGE_PATH 指定"
    )


def _launch_edge() -> None:
    exe = _find_edge()
    EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(exe), *CDP_ARGUMENTS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _ensure_cdp(deadline: float) -> tuple[str, bool]:
    """Ensure CDP port 9222 is responsive. Returns (browser_version, was_launched)."""
    existing = _probe_cdp()
    if existing:
        return existing, False

    _kill_port_owner()  # 端口被旧 QQBrowser/其他程序占用：精确按端口清掉
    if _edge_process_exists():
        _stop_edge_processes()
        kill_deadline = min(deadline, time.monotonic() + 5.0)
        while _edge_process_exists() and time.monotonic() < kill_deadline:
            time.sleep(0.3)

    # 单实例移交竞态：旧进程没死透时启动会被「接力」吞掉（新进程通知旧
    # 进程开窗后退出，旧进程随后死透 → 浏览器整个没了）。启动后探活失败再启动一次。
    for _ in range(2):
        _launch_edge()
        start_deadline = min(deadline, time.monotonic() + BROWSER_START_TIMEOUT_SECONDS)
        while time.monotonic() < start_deadline:
            browser = _probe_cdp()
            if browser:
                return browser, True
            time.sleep(0.2)

    raise SessionPrepareError("Edge 启动后 CDP 端口 9222 未就绪")


async def connect_browser(pw, timeout: float = 15000, retries: int = 1) -> Browser:
    """connect_over_cdp 的自愈版：ws 假死（HTTP 探活正常但协议消息不应答）时
    杀掉自动化专用 Edge 重启再连。与 巨量千川.session_prepare.connect_browser
    同款；重启不碰用户日常 Edge，登录态留在 profile 里。
    """
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            return await pw.chromium.connect_over_cdp(CDP_BASE_URL, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            print(f"CDP websocket 无响应，重启 Edge 后重连: {str(exc)[:120]}",
                  file=sys.stderr)
            _stop_edge_processes()
            kill_deadline = time.monotonic() + 15.0
            while _edge_process_exists() and time.monotonic() < kill_deadline:
                time.sleep(0.3)
            _kill_port_owner()
            ready = False
            for _ in range(2):
                _launch_edge()
                start_deadline = time.monotonic() + 45.0
                while time.monotonic() < start_deadline:
                    if _probe_cdp():
                        ready = True
                        break
                    time.sleep(0.5)
                if ready:
                    break
            if not ready:
                raise SessionPrepareError(
                    "Edge 重启后 CDP 端口未就绪") from last_exc
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Playwright page operations
# ---------------------------------------------------------------------------


async def pick_page(context) -> Page:
    """挑出微信小店页面；没有则复用第二个标签页，再不行就新建。

    Edge 重启后标签页顺序会变（导航页可能插到 store 页前面），
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
        raise SessionPrepareError("Edge CDP has no browser context")
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
    """Open Edge, navigate to 视频号推广创建订单页面, and verify.

    Returns a dict with status, browser, url, etc.
    """
    if deadline is None:
        deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS

    browser_name, launched = _ensure_cdp(deadline)

    async with async_playwright() as pw:
        try:
            browser = await asyncio.wait_for(
                connect_browser(pw, timeout=8000),
                timeout=max(0.5, deadline - time.monotonic()),
            )
            page = await _connected_page(browser)
            await _navigate_to_target(page, deadline)

            _log.info(
                "视频号会话准备完成: browser=%s launched=%s url=%s",
                browser_name, launched, page.url,
            )
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
