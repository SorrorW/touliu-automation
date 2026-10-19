"""Desktop-owned runner for the WeCom task-table poller.

The portable package must not depend on a Windows Scheduled Task.  This runner
exists only while the desktop UI is open and launches one ``poll_and_run.py``
process at a time.  The poller still owns the browser/file locks, so a manual
UI operation cannot run concurrently with a poll round.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


POLL_INTERVAL_SECONDS = 15 * 60


class PollRunner:
    """A conservative single-process loop controlled by automation.json."""

    def __init__(self, agent_dir: Path, flag_file: Path) -> None:
        self.agent_dir = agent_dir
        self.flag_file = flag_file
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._state: dict[str, Any] = {
            "mode": "not_started",
            "message": "桌面轮询器尚未启动",
            "last_started_at": "",
            "last_finished_at": "",
            "last_exit_code": None,
            "next_run_at": "",
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="wecom-poll-runner", daemon=True
            )
            self._thread.start()

    def notify(self) -> None:
        """Wake the loop after an automation setting changes."""
        self._wake.set()

    def stop(self) -> None:
        """Stop scheduling future rounds; never kill an active browser action."""
        self._stop.set()
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._state)
            process = self._process
            data["process_running"] = bool(process and process.poll() is None)
            return data

    def _enabled(self) -> bool:
        try:
            import json

            return bool(json.loads(self.flag_file.read_text(encoding="utf-8")).get("enabled", False))
        except Exception:
            return False

    def _set_state(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _python(self) -> str | None:
        portable = self.agent_dir.parent / "投流" / "runtime" / "python.exe"
        if portable.is_file():
            return str(portable)
        executable = Path(sys.executable)
        return str(executable) if executable.is_file() else None

    @staticmethod
    def _timestamp_after(seconds: float) -> str:
        return (datetime.datetime.now() + datetime.timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )

    def _wait(self, seconds: float) -> bool:
        self._wake.clear()
        return self._wake.wait(seconds)

    def _run_once(self) -> None:
        python = self._python()
        script = self.agent_dir / "poll_and_run.py"
        if not python or not script.is_file():
            self._set_state(
                mode="error",
                message="未找到便携运行时或 poll_and_run.py，无法启动轮询",
                next_run_at="",
            )
            return

        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        }
        log_dir = self.agent_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        runner_log = log_dir / "poll_runner_stdout.log"
        started = datetime.datetime.now().isoformat(timespec="seconds")
        self._set_state(
            mode="running",
            message="正在执行本轮企微表格轮询",
            last_started_at=started,
            next_run_at="",
        )
        try:
            with runner_log.open("a", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [python, script.name],
                    cwd=str(self.agent_dir),
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                with self._lock:
                    self._process = process
                exit_code = process.wait()
        except OSError as exc:
            self._set_state(
                mode="error",
                message=f"轮询进程启动失败: {exc}",
                last_finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
                last_exit_code=None,
                next_run_at="",
            )
            return
        finally:
            with self._lock:
                self._process = None

        finished = datetime.datetime.now().isoformat(timespec="seconds")
        if exit_code == 0:
            self._set_state(
                mode="waiting",
                message="上轮已结束，等待下一轮",
                last_finished_at=finished,
                last_exit_code=exit_code,
                next_run_at=self._timestamp_after(POLL_INTERVAL_SECONDS),
            )
        else:
            self._set_state(
                mode="error",
                message=f"上轮轮询异常退出（退出码 {exit_code}），请查看自动化日志",
                last_finished_at=finished,
                last_exit_code=exit_code,
                next_run_at=self._timestamp_after(POLL_INTERVAL_SECONDS),
            )

    def _loop(self) -> None:
        self._set_state(mode="waiting", message="桌面轮询器已启动", next_run_at="")
        while not self._stop.is_set():
            if not self._enabled():
                self._set_state(
                    mode="disabled",
                    message="自动化开关已关闭，桌面轮询器待命",
                    next_run_at="",
                )
                self._wait(3600)
                continue

            self._run_once()
            if self._stop.is_set():
                break
            if not self._enabled():
                # The switch may have changed while a round was running.
                continue
            # A settings change wakes this early. If still enabled it starts a new round;
            # otherwise the next loop marks the runner disabled.
            self._wait(POLL_INTERVAL_SECONDS)

        self._set_state(mode="stopped", message="桌面轮询器已停止", next_run_at="")


_runner: PollRunner | None = None


def configure(agent_dir: Path, flag_file: Path) -> PollRunner:
    global _runner
    if _runner is None:
        _runner = PollRunner(agent_dir, flag_file)
    return _runner


def get_runner() -> PollRunner | None:
    return _runner
