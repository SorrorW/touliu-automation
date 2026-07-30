"""桌面壳：守护线程起 uvicorn，pywebview 打开桌面窗口；无 pywebview 时回退浏览器。

用法（在 投流/ 目录下）：
    python -m webui.desktop

pythonw（桌面快捷方式）下没有控制台：
- sys.stdout/stderr 为 None 会导致部分三方库写流异常 → 开头重定向到 devnull
- 任何异常都写日志并弹前置错误框，避免"双击没反应"无法排查
日志：webui/assets/desktop_error.log
"""

from __future__ import annotations

import os
import sys

# pythonw 无控制台：stdout/stderr 为 None，先兜底再import三方库
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .app import app

HOST = "127.0.0.1"
PORT = 8321
URL = f"http://{HOST}:{PORT}"
LOG_FILE = Path(__file__).with_name("assets") / "desktop_error.log"
ICON_FILE = Path(__file__).with_name("assets") / "touliu.ico"


def _log(text: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def _alert(message: str) -> None:
    """无控制台环境下弹 Windows 错误框（置前，避免藏在其他窗口后面）。"""
    try:
        import ctypes

        MB_ICONERROR = 0x10
        MB_SETFOREGROUND = 0x10000
        ctypes.windll.user32.MessageBoxW(
            0, message, "投流操作台 - 启动失败", MB_ICONERROR | MB_SETFOREGROUND
        )
    except Exception:
        pass


def _serve() -> None:
    try:
        config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
        uvicorn.Server(config).run()
    except Exception:
        _log("uvicorn 线程异常:\n" + traceback.format_exc())


def _probe() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/status", timeout=1):
            return True
    except OSError:
        return False


def _wait_ready(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe():
            return True
        time.sleep(0.2)
    return False


def _set_window_icon() -> None:
    """Win32 设置窗口/任务栏图标。

    任务栏图标取自窗口类图标（GCLP_HICON），仅 WM_SETICON 可能被
    pywebview 后初始化覆盖，因此窗口类 + 消息双写，并隔秒重试两轮。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            hwnd = user32.FindWindowW(None, "投流操作台")
            if hwnd:
                break
            time.sleep(0.3)
        if not hwnd or not ICON_FILE.exists():
            return

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x10
        WM_SETICON = 0x80
        GCLP_HICON = -14
        GCLP_HICONSM = -34

        def _apply() -> None:
            h_small = user32.LoadImageW(
                None, str(ICON_FILE), IMAGE_ICON, 16, 16, LR_LOADFROMFILE
            )
            h_big = user32.LoadImageW(
                None, str(ICON_FILE), IMAGE_ICON, 32, 32, LR_LOADFROMFILE
            )
            if h_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, h_small)
                ctypes.windll.user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, h_small)
            if h_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, h_big)
                ctypes.windll.user32.SetClassLongPtrW(hwnd, GCLP_HICON, h_big)

        for _ in range(3):
            _apply()
            time.sleep(1.5)
    except Exception:
        pass


def main() -> None:
    # 任务栏分组/图标识别：给进程一个独立 AppUserModelID
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TouLiu.Console.1")
    except Exception:
        pass

    server = None
    if not _probe():
        server = threading.Thread(target=_serve, daemon=True)
        server.start()
        if not _wait_ready():
            raise RuntimeError(f"后端未能启动: {URL}")

    try:
        import webview
    except ImportError:
        webbrowser.open(URL)
        if server:
            server.join()
        return

    # text_select=True：允许鼠标拖选复制（pywebview 默认禁止文本选择）
    webview.create_window("投流操作台", URL, width=1280, height=860, text_select=True)
    threading.Thread(target=_set_window_icon, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        _log(detail)
        _alert(f"启动失败，详情已写入:\n{LOG_FILE}\n\n{detail.splitlines()[-1]}")
        raise SystemExit(1)
