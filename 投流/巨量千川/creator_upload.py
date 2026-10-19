"""抖音号素材上传（creator_upload）— 把本地视频发布到自运营抖音号（含挂车、定时）。

架构（2026-09-30 定）：
- 每个抖音号一个独立 Edge 配置目录 %LOCALAPPDATA%\\TouliuCreatorProfiles\\<profile_key>，
  扫码登录一次，会话长期留在该目录；自动化经每号固定 CDP 端口连接（9331 起，
  与千川自动化 9223 的 TouliuEdgeProfile 完全隔离，互不挤浏览器）。
- 账号登记在 creator_accounts.json（本模块同目录）：显示名 → profile_key / cdp_port。
- 素材目录约定（素材库根见 MAT_LIBRARY_ROOT）：
      素材库\\<账号显示名>\\<商品名>\\*.mp4 + 文案.txt
  文案.txt 首行必须是「发布时间：yyyy-MM-dd HH:mm」（定时发布，用户口径 2026-09-30），
  其余行为发布文案，目录内视频共用。已发布的视频挪入同级「已上传\\」子目录，
  幂等靠 creator_upload_state.json（文件哈希 → item_id/发布时间），重跑不重复发布。
- 覆盖范围：仅自运营账号（官方号/矩阵号）；合作达人账号无法代发，维持人工。

Usage（cd 投流 && runtime/python.exe -X utf8 -m 巨量千川.creator_upload ...）：
  login --account 鲜会鲜     # 打开创作者中心扫码登录页（弹 Edge 窗口，人工扫码）
  status [--account 鲜会鲜]  # 只读检查登录态（缺省查全部已登记账号）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from .session_prepare import _EDGE_BROWSER_CANDIDATES

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ACCOUNTS_FILE = Path(__file__).resolve().parent / "creator_accounts.json"
STATE_FILE = Path(__file__).resolve().parent / "creator_upload_state.json"

# 素材库根目录：<账号显示名>\<商品名>\ 下分目录存放
MAT_LIBRARY_ROOT = Path(r"d:\Documents\SR_Project\计划投流\素材库")

# 抖音号专用 Edge 配置根（与千川 TouliuEdgeProfile 隔离）
CREATOR_PROFILE_ROOT = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    / "TouliuCreatorProfiles"
)

CREATOR_HOME = "https://creator.douyin.com/creator-micro/content/upload"
CDP_PROBE_TIMEOUT = 3.0


def load_accounts() -> dict[str, dict]:
    if not ACCOUNTS_FILE.exists():
        return {}
    return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))


def _edge_exe() -> str:
    for candidate in _EDGE_BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("未找到 Edge 安装路径")


# ---------------------------------------------------------------------------
# login：打开扫码登录页（窗口保持开着，人扫码后会话落进该号配置目录）
# ---------------------------------------------------------------------------


def cmd_login(account: str) -> None:
    accounts = load_accounts()
    info = accounts.get(account)
    if info is None:
        raise SystemExit(f"账号未登记：{account}（先写进 creator_accounts.json）")
    profile_dir = CREATOR_PROFILE_ROOT / info["profile_key"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        _edge_exe(),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={info['cdp_port']}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-gpu",
        CREATOR_HOME,
    ]
    subprocess.Popen(args, close_fds=True)
    print(f"已打开 {account} 的创作者中心登录页（配置目录 {profile_dir}）。")
    print(f"请用持有「{account}」抖音号的手机扫码登录；完成后跑 status 验证。")


# ---------------------------------------------------------------------------
# status：只读检查登录态（连该号的 CDP，开一次性标签页，读完即关）
# ---------------------------------------------------------------------------


async def _check_one(account: str, info: dict) -> dict:
    result = {"account": account, "cdp_port": info["cdp_port"], "logged_in": False}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{info['cdp_port']}", timeout=5000
            )
        except Exception as exc:
            result["error"] = f"CDP 未连上（{type(exc).__name__}）——先跑 login"
            return result
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
            text = (await page.evaluate("() => document.body.innerText || ''"))[:2000]
            result["final_url"] = page.url
            if "扫码登录" in text and "获取验证码" in text:
                result["logged_in"] = False
                result["note"] = "仍是登录页"
            else:
                result["logged_in"] = True
                result["note"] = "已进入创作者中心"
                # 读账号名线索（侧边栏/头像区文本片段）
                result["page_head"] = text[:300]
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await page.close()
    return result


def cmd_status(account: str | None) -> None:
    accounts = load_accounts()
    names = [account] if account else list(accounts)
    if not names:
        raise SystemExit("creator_accounts.json 为空")
    results = [
        asyncio.run(_check_one(name, accounts[name]))
        for name in names
        if name in accounts
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="抖音号素材上传（自运营账号）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="打开扫码登录页")
    p_login.add_argument("--account", required=True)
    p_status = sub.add_parser("status", help="只读检查登录态")
    p_status.add_argument("--account", default=None)
    args = parser.parse_args()
    if args.cmd == "login":
        cmd_login(args.account)
    elif args.cmd == "status":
        cmd_status(args.account)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
