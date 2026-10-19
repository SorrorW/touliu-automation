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
import subprocess
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
from .poll_runner import get_runner

HOST = "127.0.0.1"
PORT = 8321
URL = f"http://{HOST}:{PORT}"
LOG_FILE = Path(__file__).with_name("assets") / "desktop_error.log"
# uvicorn 子进程输出落文件：2026-09-02 子进程一天内两次无声死亡（stderr 指到
# devnull 什么痕迹都没留），此后它的 stdout/stderr 追加到这里，崩溃可溯源。
CHILD_LOG_FILE = Path(__file__).with_name("assets") / "uvicorn_child.log"
ICON_FILE = Path(__file__).with_name("assets") / "touliu.ico"
# 本地健康检查必须直连。系统代理或环境变量代理不应影响 127.0.0.1。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
        # Embedded uvicorn in a pythonw thread must run without signal handlers;
        # otherwise the server can remain alive/listening but never complete
        # startup, making the desktop shell report a false 8321 failure.
        config = uvicorn.Config(
            app, host=HOST, port=PORT, log_level="warning", loop="asyncio"
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server.run()
        if not server.started:
            _log("uvicorn 在完成启动前退出")
    except BaseException:
        _log("uvicorn 线程异常:\n" + traceback.format_exc())


def _open_child_log():
    """uvicorn 子进程的 stdout/stderr 追加句柄；打不开就退回 devnull。"""
    try:
        CHILD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        return open(CHILD_LOG_FILE, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None


def _spawn_server(root: Path):
    """拉起 uvicorn 子进程，返回 (Popen, 日志句柄)。"""
    # The shell itself is launched with pythonw.exe, but uvicorn must use
    # the console-capable bundled interpreter. Starting uvicorn under
    # pythonw can leave the child alive without completing the socket
    # startup on this Windows runtime.
    server_python = root / "runtime" / "python.exe"
    if not server_python.exists():
        server_python = Path(sys.executable)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stream = _open_child_log()
    proc = subprocess.Popen(
        [str(server_python), "-m", "uvicorn", "webui.app:app",
         "--host", HOST, "--port", str(PORT), "--log-level", "warning"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=stream if stream is not None else subprocess.DEVNULL,
        stderr=stream if stream is not None else subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return proc, stream


def _watch_server(root: Path, state: dict, closing: threading.Event) -> None:
    """后端看门狗：健康检查连续 2 次失败（约 30s）就重拉 uvicorn。

    2026-09-02 教训：uvicorn 子进程一天内两次无声死亡（stderr 到 devnull，
    无崩溃事件、无日志），窗口还在但所有接口 Failed to fetch。此后子进程
    输出落 CHILD_LOG_FILE，并由本线程自动重拉——poll 线程在壳进程内，
    不随后端重启中断。
    """
    failures = 0
    while not closing.wait(15):
        if _probe():
            failures = 0
            continue
        failures += 1
        if failures < 2:
            continue
        proc = state.get("process")
        if proc is not None and proc.poll() is None:
            continue  # 子进程还活着但尚未就绪，别并发重拉
        _log(f"后端失联（连续 {failures} 次探测失败），重新拉起 uvicorn…")
        try:
            old_stream = state.get("stream")
            proc, stream = _spawn_server(root)
            state["process"] = proc
            state["stream"] = stream
            if old_stream is not None:
                try:
                    old_stream.close()
                except OSError:
                    pass
            if _wait_ready():
                _log("后端已恢复监听")
                failures = 0
            else:
                _log("重拉后 30s 内未就绪，15s 后再试")
        except Exception:
            _log("重拉后端异常:\n" + traceback.format_exc())


def _probe() -> bool:
    try:
        with _LOCAL_OPENER.open(f"{URL}/healthz", timeout=1):
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


def _ensure_desktop_shortcut() -> None:
    """自动创建「投流操作台」快捷方式：桌面一份 + 开机启动文件夹一份
    （2026-09-30 用户要求开机自启，免得每次手动开；两份各自独立检查，
    已存在则跳过，相当于自愈）。"""
    try:
        root = Path(__file__).resolve().parents[1]  # 投流/
        candidates = [
            root / "runtime" / "pythonw.exe",
            root / ".venv" / "Scripts" / "pythonw.exe",
        ]
        pythonw = None
        for c in candidates:
            if c.exists():
                pythonw = str(c)
                break
        if not pythonw:
            _log("未找到 pythonw.exe，跳过快捷方式创建")
            return

        icon = str(root / "webui" / "assets" / "touliu.ico")
        if not os.path.exists(icon):
            _log("未找到 touliu.ico，跳过快捷方式创建")
            return

        desk = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        startup = os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs\Startup",
        )
        targets = [os.path.join(desk, "投流操作台.lnk")]
        if os.path.isdir(startup):
            targets.append(os.path.join(startup, "投流操作台.lnk"))

        import subprocess
        for lnk_path in targets:
            if os.path.exists(lnk_path):
                continue
            # 通过 PowerShell COM 创建快捷方式
            ps_script = f'''
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("{lnk_path}")
$s.TargetPath = "{pythonw}"
$s.Arguments = "-m webui.desktop"
$s.WorkingDirectory = "{root}"
$s.IconLocation = "{icon}"
$s.Description = "投流操作台 - 千川投流日常操作"
$s.Save()
'''
            subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if os.path.exists(lnk_path):
                _log(f"快捷方式已创建: {lnk_path}")
            else:
                _log(f"快捷方式创建后未找到文件: {lnk_path}")
    except Exception:
        _log("快捷方式创建失败:\n" + traceback.format_exc())


def main() -> None:
    # 任务栏分组/图标识别：给进程一个独立 AppUserModelID
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TouLiu.Console.1")
    except Exception:
        pass

    # 单实例守卫：开机自启后再双击桌面图标会起第二份，uvicorn 端口冲突、
    # 轮询器双跑。用命名互斥体，已存在则直接退出（2026-09-30 开机自启配套）。
    try:
        import ctypes

        _singleton_mutex = ctypes.windll.kernel32.CreateMutexW(
            None, False, "TouLiu.Console.Singleton"
        )
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            _log("投流操作台已在运行，本次启动退出（单实例）")
            return
        globals()["_singleton_mutex"] = _singleton_mutex  # 保活到进程结束
    except Exception:
        pass

    # 首次运行自动创建桌面图标 + 开机启动项
    _ensure_desktop_shortcut()

    state = {"process": None, "stream": None}
    closing = threading.Event()
    if not _probe():
        # Run uvicorn as a child process rather than in a pythonw thread. On
        # Windows, signal/event-loop setup inside a background thread can leave
        # the socket alive but make the embedded server fail to finish startup.
        root = Path(__file__).resolve().parents[1]
        proc, stream = _spawn_server(root)
        state["process"] = proc
        state["stream"] = stream
        if not _wait_ready():
            if proc.poll() is None:
                proc.terminate()
            raise RuntimeError(f"后端未能启动: {URL}")

    # 后端健康检查通过后才启动轮询，避免窗口启动失败时留下后台执行进程。
    # 不依赖或恢复 Windows 计划任务。
    runner = get_runner()
    if runner:
        runner.start()

    # 看门狗：后端中途死亡时自动重拉（窗口不关就不该丢接口）。
    threading.Thread(
        target=_watch_server,
        args=(Path(__file__).resolve().parents[1], state, closing),
        daemon=True,
    ).start()

    try:
        import webview
    except ImportError:
        webbrowser.open(URL)
        return

    # icon 参数由 pywebview 原生设置标题栏和任务栏图标；低层 Win32 线程仅作兜底。
    # text_select=True：允许鼠标拖选复制（pywebview 默认禁止文本选择）
    webview.create_window("投流操作台", URL, width=1280, height=860, text_select=True)
    threading.Thread(target=_set_window_icon, daemon=True).start()
    try:
        webview.start(icon=str(ICON_FILE) if ICON_FILE.exists() else None)
    finally:
        closing.set()  # 先停看门狗，防止窗口关闭时把后端又拉起来
        if runner:
            runner.stop()
        proc = state.get("process")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        _log(detail)
        _alert(f"启动失败，详情已写入:\n{LOG_FILE}\n\n{detail.splitlines()[-1]}")
        raise SystemExit(1)
