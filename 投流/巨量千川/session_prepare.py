"""Prepare a verified Qianchuan (巨量千川) session in Microsoft Edge via CDP.

Single entry point — launch (or reuse) Edge with CDP port 9223,
navigate to Qianchuan, select a department account, and verify the URL.

浏览器于 2026-08-17 从 QQBrowser 换成 Edge：用自动化专用用户数据目录
（%LOCALAPPDATA%\\TouliuEdgeProfile），与用户日常的 Edge 完全隔离——
自动化重启浏览器不会动到用户自己的 Edge 窗口；首次使用需在新窗口里
扫码登录一次千川。

Dependency: pip install "playwright>=1.60,<2"
Usage:     python session_prepare.py --department 业务3部
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Edge 安装路径候选（按优先级；也可用环境变量 EDGE_PATH 指定）
_EDGE_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
# 自动化专用用户数据目录：与用户日常 Edge 隔离，自动化重启不碰用户浏览器；
# 登录态（千川扫码）保存在这里，绝不能打包带出（含 Cookie）。
# 放 LOCALAPPDATA（机器级唯一）：开发树、运行中的发布包、以后换版本包
# 都解析到同一目录——换包不用重新扫码，也不会有两套 profile 抢 9223 端口。
EDGE_PROFILE_DIR = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
) / "TouliuEdgeProfile"
CDP_PORT = 9223
CDP_URL = f"http://127.0.0.1:{CDP_PORT}/json/version"
CDP_BASE_URL = f"http://127.0.0.1:{CDP_PORT}"
CDP_ARGUMENTS = [
    f"--user-data-dir={EDGE_PROFILE_DIR}",
    f"--remote-debugging-port={CDP_PORT}",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",  # 强杀后不弹「恢复页面」气泡
    "--hide-crash-restore-bubble",
    # 2026-09-21 用户要求「不能丢第一页」：万不得已重拉时恢复上次会话的
    # 全部标签，用户正在操作的页面随重启回来，而不是一个空窗口。
    "--restore-last-session",
    # 自动化专用 Edge 在本机曾出现启动即崩溃（0xc06d007e）；禁用 GPU 不影响
    # 千川页面自动化，却避开图形进程初始化路径。
    "--disable-gpu",
]

HOME_URL = "https://qianchuan.jinritemai.com/"
OVERALL_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
TOTAL_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 3.0        # 会话恢复期的浏览器应答慢，0.5s 探活会误判死亡（2026-08-17）
BROWSER_START_TIMEOUT_SECONDS = 45.0  # 重会话机器 8s 起不来，反复被误杀重启

try:
    from .main_products import MAIN_AAVID, PRODUCT_BY_NAME
except ImportError:  # 保留脚本直接运行方式
    from main_products import MAIN_AAVID, PRODUCT_BY_NAME

# 可唤起账号；2026-10-20 移除一部-1，主号与三个业务部保留。
# 业务部 → 千川账号显示名
ACCOUNT_BY_DEPARTMENT: dict[str, str] = {
    "主号": "鲜会鲜",
    "业务1部": "鲜会鲜官方旗舰店:一部",
    "业务2部": "鲜会鲜官方旗舰店:二部",
    "业务3部": "鲜会鲜官方旗舰店:三部",
}

# 商品名称 → 千川商品 ID（增删改在这里）
PRODUCT_MAP: dict[str, str] = {
    "肥牛": PRODUCT_BY_NAME["肥牛主品"].product_id,
    "仔骨": PRODUCT_BY_NAME["牛仔骨"].product_id,
    "上脑": PRODUCT_BY_NAME["上脑"].product_id,
    "板腱": PRODUCT_BY_NAME["牛板腱"].product_id,
}


def lookup_product(key: str) -> str | None:
    """商品名称/ID → 千川商品 ID。已知 ID 则原样返回。

    2026-10-15 实证脏数据：业务员把 ID 打成「3,562896022277644951」（首字符后
    多了个逗号），整串 split 后拿「3」去查 → 误判商品ID无效。先剥离逗号/空格
    再查（剥离后恰为合法 ID 时自动治愈）。
    """
    if key:
        cleaned = str(key).replace(",", "").replace("，", "").replace(" ", "").strip()
        if cleaned != key and cleaned:
            key = cleaned
    if key in PRODUCT_MAP:
        return PRODUCT_MAP[key]
    if key in {v: k for k, v in PRODUCT_MAP.items()}:
        return key  # 已经是 ID
    return None


def _account_id(account: str) -> str:
    """Map display account name to platform numeric account ID."""
    return {
        "鲜会鲜": MAIN_AAVID,
        "鲜会鲜官方旗舰店:三部": "1859511296951635",
        "鲜会鲜官方旗舰店:二部": "1861049584106636",
        "鲜会鲜官方旗舰店:一部": "1861049594712072",
    }[account]


# ---------------------------------------------------------------------------
# CDP / Browser lifecycle
# ---------------------------------------------------------------------------


class SessionPrepareError(RuntimeError):
    pass


def _probe_cdp() -> str | None:
    try:
        # 必须走 _cdp_http_opener（绕过系统代理）：裸 urlopen 会读 IE/环境变量
        # 代理，uvicorn 进程一旦带着代理变量启动，127.0.0.1:9223 被送进代理
        # 导致探测永远失败、控制台误报「平台未连接」（2026-09-30 实证）。
        with _cdp_http_opener().open(CDP_URL, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        browser = str(payload.get("Browser", ""))
        ws_url = str(payload.get("webSocketDebuggerUrl", ""))
        # 9223 是投流专用端口：先确认其确为 Edge 的本机 CDP 端口。
        # 不能要求此时已存在千川标签页——Edge 刚启动时只有新标签页，
        # prepare_session 正是下一步负责打开千川并选账户的入口。若这里把
        # 新标签页判为“不就绪”，会反复杀掉正常 Edge，最终永远无法连接。
        if not (browser and "Edg" in browser and ws_url.startswith(f"ws://127.0.0.1:{CDP_PORT}/")):
            return None
        return browser
    except Exception:
        return None


def _run_powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
        # CREATE_NO_WINDOW：本函数由无控制台的子进程（auth/plan_create 等）
        # 调用，缺省会给 powershell 新配一个可见黑窗口，每轮刷用户屏幕
        # （2026-09-30 用户指认「一直都有弹窗」）。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout or ""


def _another_task_busy() -> str | None:
    """其他自动化任务正占用浏览器时返回描述，否则 None。

    杀浏览器前的护栏（2026-09-21/22 连续两天 12:00 任务互杀：一边 ws 超时
    误判假死→杀整个 Edge 重拉，另一边正在恢复的页面全崩、90 条老池没开成）。
    忙标新鲜 / 轮询进行中 = 浏览器多半只是被压住，不能杀，失败快速退出等下轮。
    """
    wecom_dir = Path(__file__).resolve().parents[2] / "wecom-agent"
    now = time.time()
    for name in ("plan_power_busy.json", "report_busy.json"):
        try:
            data = json.loads((wecom_dir / name).read_text(encoding="utf-8"))
            age = now - float(data.get("ts") or 0)
            if age < 600:
                return f"{name}(action={data.get('action')}, {int(age)}s前)"
        except Exception:
            continue
    try:
        status_file = wecom_dir / "status.json"
        if now - status_file.stat().st_mtime < 300:
            if json.loads(status_file.read_text(encoding="utf-8")).get("state") == "running":
                return "企微轮询进行中(status.json)"
    except Exception:
        pass
    return None


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
    """杀掉占用 9223 端口的进程（如迁移前残留的 QQBrowser 实例）。

    按端口精确到 PID、不按映像名：不会误伤用户自己开的任何浏览器。
    """
    _run_powershell(
        f"Get-NetTCPConnection -LocalPort {CDP_PORT} -State Listen -ErrorAction SilentlyContinue "
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


# 浏览器重拉冷却（跨进程）：最后一次杀拉时刻记在这里，所有自动化子进程共享。
RELAUNCH_COOLDOWN_SECONDS = 120.0
_RELAUNCH_STATE_FILE = (
    Path(__file__).resolve().parents[2] / "wecom-agent" / "logs" / "browser_relaunch_state.json"
)


def _last_relaunch_ts() -> float:
    try:
        return float(json.loads(
            _RELAUNCH_STATE_FILE.read_text(encoding="utf-8")).get("ts") or 0.0)
    except Exception:
        return 0.0


def _mark_relaunch() -> None:
    try:
        _RELAUNCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RELAUNCH_STATE_FILE.write_text(
            json.dumps({"ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def _ensure_cdp(deadline: float) -> tuple[str, bool]:
    """确认 9223 可用；绝不因为探活失败杀掉或重启用户正在操作的 Edge。

    2026-09-23 用户明确要求保留第一页。CDP 临时无响应时，旧逻辑会把专用
    Edge 整个杀掉再重拉，导致批量启停里的 Page 被关闭、用户标签丢失。现在只
    短暂重试探活；仍不可用则让本轮明确失败，等待后续轮次或用户手动恢复。
    """
    existing = _probe_cdp()
    if existing:
        return existing, False

    wait_deadline = min(deadline, time.monotonic() + 15.0)
    while time.monotonic() < wait_deadline:
        browser = _probe_cdp()
        if browser:
            return browser, False
        time.sleep(0.5)
    raise SessionPrepareError(
        "Edge CDP 端口 9223 未就绪；按用户要求不自动重启浏览器，本轮跳过")


# ---------------------------------------------------------------------------
# Playwright page operations
# ---------------------------------------------------------------------------


async def _connected_qianchuan_page(browser: Browser) -> Page:
    if not browser.contexts:
        raise SessionPrepareError("Edge CDP has no browser context")
    context = browser.contexts[0]
    pages = [p for p in context.pages if "qianchuan" in p.url]
    return pages[0] if pages else await context.new_page()


MAX_OTHER_TABS = 7  # 除第一页外其他标签上限（2026-09-21 用户定：第一页是用户
                    # 手动操作页，永不关闭；其余页面不超过 7 个，超出从最旧的
                    # 千川标签开始回收。原 MAX_QIANCHUAN_TABS=7 只数千川域名且
                    # 不豁免第一页——积压超限时第一个被关的正是用户的第一页）


def _cdp_http_opener():
    """绕过系统代理访问本机 CDP（127.0.0.1 不该走代理）。"""
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cdp_websocket_url() -> str:
    """从本机 CDP 直接取得浏览器 WebSocket，避免 Playwright 的 HTTP 探测受代理影响。"""
    with _cdp_http_opener().open(CDP_URL, timeout=PROBE_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    ws_url = str(payload.get("webSocketDebuggerUrl", ""))
    if not ws_url.startswith(f"ws://127.0.0.1:{CDP_PORT}/"):
        raise SessionPrepareError("CDP 未返回可用的本机 WebSocket 地址")
    return ws_url


def _cdp_list_targets() -> list:
    with _cdp_http_opener().open(CDP_BASE_URL + "/json/list", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _cdp_close_target(target_id: str) -> bool:
    """经 CDP HTTP 端点关闭标签。新内核要求 PUT，旧内核 GET，都试。"""
    import urllib.request
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(
                f"{CDP_BASE_URL}/json/close/{target_id}", method=method)
            with _cdp_http_opener().open(req, timeout=5) as resp:
                resp.read()
            return True
        except Exception:
            continue
    return False


async def open_fresh_page(context) -> Page:
    """为当前操作新开独占标签页，第一页永不碰，其他标签硬上限为 7。

    不能通过关闭“最旧标签”腾位：它可能正被另一个批量任务使用，正是
    ``Target page ... has been closed`` 的根源。到上限时让当前任务明确跳过，
    等既有任务在 finally 释放自己的标签后下轮再试；安全优先于硬抢。
    """
    all_pages = list(context.pages)
    pinned = all_pages[0] if all_pages else None
    others = [p for p in all_pages if p is not pinned]
    if len(others) >= MAX_OTHER_TABS:
        raise SessionPrepareError(
            f"浏览器其他标签数已达标签上限 {MAX_OTHER_TABS}，为保护第一页及运行中的任务，本轮跳过")
    return await context.new_page()


_MAIN_POLL_LOCK = Path(__file__).resolve().parents[2]/'wecom-agent/browser_poll.lock'
_main_owned_page = None

def main_browser_lock_owned():
    try:
        info=json.loads(_MAIN_POLL_LOCK.read_text(encoding='utf-8'))
        return (info.get('pid') in (os.getpid(),os.getppid()) and
                time.time()-_MAIN_POLL_LOCK.stat().st_mtime < 30*60)
    except (OSError,ValueError):
        return False

async def open_main_account_page(context) -> Page:
    """共享锁下只持有一个主号临时页，不关闭或借用其他业务的既有标签。"""
    global _main_owned_page
    if not main_browser_lock_owned():
        raise SessionPrepareError('主号操作必须通过持有浏览器互斥锁的统一入口运行')
    if _main_owned_page is not None and not _main_owned_page.is_closed():
        raise SessionPrepareError('本进程已有主号临时页，禁止叠加新页')
    page=await context.new_page()
    _main_owned_page=page
    def release(*_):
        global _main_owned_page
        if _main_owned_page is page:_main_owned_page=None
    page.on('close',release)
    return page

async def close_main_account_page():
    """异常换页时调用方可能仍引用旧页，只释放工厂登记的本进程临时页。"""
    page=_main_owned_page
    if page is not None and not page.is_closed():
        try:await page.close()
        except Exception:pass

async def connect_browser(pw, timeout: float = 15000, retries: int = 1) -> Browser:
    """连接既有 CDP 会话；WebSocket 异常时只重试连接，绝不重启 Edge。

    2026-09-23 用户要求保留第一标签页。此前 WebSocket 假死会触发
    ``_stop_edge_processes()``，批量开启中的页面被一起关闭。现在连接失败只
    让当前自动化任务失败，等待下一轮或用户自行恢复浏览器。
    """
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            return await pw.chromium.connect_over_cdp(
                _cdp_websocket_url(), timeout=timeout
            )
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(1.0)
    raise SessionPrepareError(
        "Edge CDP WebSocket 无响应；按用户要求不自动重启浏览器，本轮跳过"
    ) from last_exc


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


async def force_plans_tab(page, tries: int = 12, interval_ms: int = 500) -> None:
    """把乘方总览页的子 Tab 软切回计划Tab（uniVideoTab=1），直到表格/搜索框出现。

    2026-09-25 乘方改版坑：被迁账号的总览页 SPA 按平台持久态把子 Tab 拨回
    素材Tab（uniVideoTab=2），计划表格和搜索框整体不渲染；整页 goto/reload
    带 hash 无效（启动时被持久态覆盖），只有 hashchange 软切生效。
    整页重载后必须重新拨——plan_metrics 的「整页重载重试」就靠这里补救。
    """
    for i in range(tries):
        try:
            if await page.locator('input[placeholder*="计划名称"]').count():
                return
            if await page.locator("table tbody tr").count():
                return
            await page.evaluate(
                """() => {
                    const h = new URLSearchParams(location.hash.slice(2));
                    if (h.get('uniVideoTab') !== '1') {
                        h.set('uniVideoTab', '1');
                        location.hash = '#?' + h.toString();
                    }
                }"""
            )
            if i in (4, 8):
                # 首次 hashchange 可能赶在 SPA 路由注册前被吞——强制 2→1 再触发一次
                await page.evaluate(
                    """() => {
                        const h = new URLSearchParams(location.hash.slice(2));
                        h.set('uniVideoTab', '2');
                        location.hash = '#?' + h.toString();
                    }"""
                )
                await page.wait_for_timeout(300)
                await page.evaluate(
                    """() => {
                        const h = new URLSearchParams(location.hash.slice(2));
                        h.set('uniVideoTab', '1');
                        location.hash = '#?' + h.toString();
                    }"""
                )
        except Exception:
            pass
        await page.wait_for_timeout(interval_ms)


async def ensure_qianchuan_page(page: Page, department: str = "业务3部", timeout: float = 15.0) -> Page:
    """Navigate to the overall plan page, handle login/account selection if needed.

    Key: must include ?aavid=<account_id> in the URL, otherwise Qianchuan
    redirects to the login/account-chooser page.

    Flow: 导航到总览页(带aavid) → 如跳转登录则选部门 → 重试直到搜索框出现

    返回实际可用的 Page：标签页导航卡死时（浏览器偶发：JS 尚能响应但文档
    加载永不 commit）自动关闭僵尸标签、换新页继续，调用方必须使用返回值。
    """
    account = ACCOUNT_BY_DEPARTMENT[department]
    aavid = _account_id(account)
    target_url = f"{OVERALL_URL}?aavid={aavid}"
    deadline = time.monotonic() + timeout

    for attempt in range(3):
        try:
            # Playwright 在 CDP 假活时可能忽略 goto 自身 timeout；外层硬超时
            # 确保单页失败后能关闭僵尸标签并重试，不阻塞整轮生命周期任务。
            await asyncio.wait_for(
                page.goto(target_url, wait_until="commit", timeout=10000),
                timeout=12.0,
            )
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
            # 僵尸标签页：不换新页则后续所有 goto 都会卡死
            context = page.context
            try:
                await page.close()
            except Exception:
                pass
            page = await open_fresh_page(context)
            continue
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
                await asyncio.wait_for(
                    page.goto(target_url, wait_until="commit", timeout=10000),
                    timeout=12.0,
                )
                await page.wait_for_timeout(500)

        # Now check for search input
        # 2026-10-14 用户口径「弹窗见就点掉」：每轮探测前先清弹窗/遮罩。
        from .popup_killer import dismiss_popups
        for _try in range(20):
            if _try in (0, 5, 10):
                await dismiss_popups(page)
            for sel in [
                'input[placeholder*="抖音号"]',
                'input[placeholder*="计划名称"]',
            ]:
                el = page.locator(sel)
                if await el.count() and await el.first.is_visible():
                    return page
            # 2026-09-25 乘方改版坑：三个旗舰店账号被迁到千川乘方后，总览页 SPA
            # 会按平台持久态把子 Tab 拨回素材Tab（uniVideoTab=2），计划表格和
            # 搜索框整体不渲染；整页 goto 带 hash 无效（启动时被持久态覆盖），
            # 只有 hashchange 软切能把 Tab 拨回计划Tab（uniVideoTab=1）。
            if _try % 2 == 1:
                try:
                    await page.evaluate(
                        """() => {
                            const h = new URLSearchParams(location.hash.slice(2));
                            if (h.get('uniVideoTab') !== '1') {
                                h.set('uniVideoTab', '1');
                                location.hash = '#?' + h.toString();
                            }
                        }"""
                    )
                except Exception:
                    pass
            if _try in (10, 15):
                # 首次 hashchange 可能赶在 SPA 路由注册前被吞——强制 2→1 再触发一次
                try:
                    await page.evaluate(
                        """() => {
                            const h = new URLSearchParams(location.hash.slice(2));
                            h.set('uniVideoTab', '2');
                            location.hash = '#?' + h.toString();
                        }"""
                    )
                    await page.wait_for_timeout(400)
                    await page.evaluate(
                        """() => {
                            const h = new URLSearchParams(location.hash.slice(2));
                            h.set('uniVideoTab', '1');
                            location.hash = '#?' + h.toString();
                        }"""
                    )
                except Exception:
                    pass
            # 新账号(零计划)总览页是乘方营销首页,永远没有搜索框——
            # 头部账户区出现本账号 aavid 即证明已落到正确账号(2026-09-02 业务1部-1 实测)
            # 2026-09-25 起该兜底延后到后半程：乘方页头部也带 aavid，提前命中会
            # 跳过上面的 Tab 软切，导致计划表格永远等不到。
            if _try >= 8 and "/login" not in page.url and "overall" in page.url:
                try:
                    if await page.locator(f'text=ID：{aavid}').count():
                        return page
                except Exception:
                    pass
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
                connect_browser(pw, timeout=8000),
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
