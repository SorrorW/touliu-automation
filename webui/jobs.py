"""后台任务管理：所有浏览器操作经全局锁串行执行，任务记录落 SQLite（保留 7 天）。

所有脚本共享同一个 QQBrowser CDP（9222），任何两个浏览器操作并发都会
互相抢页面，因此每个 job 的执行体都在 ``_browser_lock`` 里运行。

任务生命周期：queued（等锁）→ running（拿到锁，正在操作浏览器）→
done / error / cancelled。asyncio.Lock 按 FIFO 唤醒，先提交的先执行。
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, Awaitable, Callable

from . import db
from .logging_setup import get_logger

# 全局浏览器锁：保证同一时间只有一个浏览器操作在执行
browser_lock = asyncio.Lock()

# 当前正在执行的 job_id（按 asyncio task 隔离）：任务体任意深度都能上报进度
_current_job_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_job_id", default=None
)

_log = get_logger(__name__)


class JobManager:
    """提交/跟踪后台任务；记录持久化到 SQLite（见 db.py）。"""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._progress: dict[int, dict[str, int]] = {}

    def report_progress(self, done: int, total: int) -> None:
        """上报当前任务的进度（从任务协程内调用，无需知道 job_id）。"""
        job_id = _current_job_id.get()
        if job_id is not None:
            self._progress[job_id] = {"done": int(done), "total": int(total)}

    def submit(
        self,
        name: str,
        coro: Awaitable[Any],
        on_done: Callable[[Any], None] | None = None,
    ) -> int:
        """提交一个协程为后台任务，返回 job_id。须在运行中的事件循环里调用。

        on_done: 任务成功完成后的同步回调（如扫描结果落库），参数为任务结果。
        """
        job_id = db.insert_job(name, time.time())
        _log.info("任务提交 #%d %s（排队）", job_id, name)
        task = asyncio.create_task(self._run(job_id, name, coro, on_done))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        return job_id

    def cancel(self, job_id: int) -> bool:
        """终止任务：排队中的立即取消，执行中的中断浏览器操作。返回是否受理。"""
        job = db.get_job(job_id)
        if not job or job["status"] not in {"queued", "running"}:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()  # 状态在 _run 的 CancelledError 分支里落库
        else:
            # 内存里已没有该任务（如服务重启后），直接改状态
            db.update_job(job_id, status="cancelled", finished_at=time.time())
        _log.info("任务终止请求 #%d %s", job_id, job["name"])
        return True

    async def _run(
        self,
        job_id: int,
        name: str,
        coro: Awaitable[Any],
        on_done: Callable[[Any], None] | None,
    ) -> None:
        started_at = time.time()
        _current_job_id.set(job_id)
        try:
            async with browser_lock:
                db.mark_job_running(job_id, time.time())
                result = await coro
            finished_at = time.time()
            db.update_job(job_id, status="done", finished_at=finished_at, result=result)
            _log.info("任务完成 #%d %s（%.1f 秒）", job_id, name, finished_at - started_at)
            if on_done:
                try:
                    on_done(result)
                except Exception:  # 持久化失败不影响任务本身的结果
                    _log.warning("任务 #%d 结果落库失败", job_id, exc_info=True)
        except asyncio.CancelledError:
            # 排队时协程从未被 await，close 掉避免 "never awaited" 警告；
            # 执行中被取消时协程已随 CancelledError 展开，close 是无害的 no-op。
            coro.close()  # type: ignore[attr-defined]
            db.update_job(job_id, status="cancelled", finished_at=time.time())
            _log.info("任务已终止 #%d %s", job_id, name)
            raise
        except Exception as exc:  # noqa: BLE001 — 错误要回传给前端
            db.update_job(
                job_id, status="error", finished_at=time.time(), error=str(exc)
            )
            _log.error("任务失败 #%d %s: %s", job_id, name, str(exc)[:300])
        finally:
            self._progress.pop(job_id, None)

    def get(self, job_id: int) -> dict[str, Any] | None:
        job = db.get_job(job_id)
        if job is not None:
            job["progress"] = self._progress.get(job_id)
        return job

    def list(self) -> list[dict[str, Any]]:
        return db.list_jobs()


manager = JobManager()
