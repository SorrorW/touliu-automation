"""监控扫描自动运行：开关存 assets/autoscan.json，后台协程到点提交扫描任务。

只在控制台（webui）运行期间生效；自动提交的任务仍走 JobManager
（浏览器锁 + 与 wecom 轮询的互斥），与手动点「开始扫描」完全同路径，
结果落同一个库，监控页「上次扫描」时间随之刷新。

设置文件 autoscan.json::
    {"enabled": true, "interval_hours": 2,
     "last_run": "2026-08-17T14:00:00", "next_run": "2026-08-17T16:00:00"}
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any, Callable

from .logging_setup import get_logger

_log = get_logger(__name__)

SETTINGS_FILE = Path(__file__).with_name("assets") / "autoscan.json"
DEFAULT_INTERVAL_HOURS = 2
INTERVAL_CHOICES = (1, 2, 4)


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    interval = data.get("interval_hours", DEFAULT_INTERVAL_HOURS)
    if interval not in INTERVAL_CHOICES:
        interval = DEFAULT_INTERVAL_HOURS
    return {
        "enabled": bool(data.get("enabled", False)),
        "interval_hours": interval,
        "last_run": data.get("last_run"),
        "next_run": data.get("next_run"),
    }


def save_settings(enabled: bool, interval_hours: int) -> dict[str, Any]:
    """更新开关/周期；开启时从现在起一个周期后为首跑时间。"""
    current = load_settings()
    current["enabled"] = bool(enabled)
    if interval_hours in INTERVAL_CHOICES:
        current["interval_hours"] = interval_hours
    current["next_run"] = (
        (datetime.datetime.now()
         + datetime.timedelta(hours=current["interval_hours"])
         ).isoformat(timespec="seconds")
        if current["enabled"] else None
    )
    SETTINGS_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.info("自动扫描设置更新: enabled=%s interval=%sh next=%s",
              current["enabled"], current["interval_hours"], current["next_run"])
    return current


async def autoscan_loop(run_scan: Callable[[], Any]) -> None:
    """每分钟检查一次是否到点；到点调用 run_scan()（应提交一次全部门扫描）。

    run_scan 抛错只记日志不影响排程：无论成败都按周期排下一次，
    失败由任务系统/下次扫描兜底，避免循环本身死掉。
    """
    while True:
        await asyncio.sleep(60)
        try:
            st = load_settings()
            if not st["enabled"] or not st["next_run"]:
                continue
            if datetime.datetime.now() < datetime.datetime.fromisoformat(st["next_run"]):
                continue
            _log.info("自动扫描到点，提交监控扫描（每 %s 小时一轮）", st["interval_hours"])
            try:
                run_scan()
            except Exception:
                _log.warning("自动扫描提交失败", exc_info=True)
            now = datetime.datetime.now()
            st["last_run"] = now.isoformat(timespec="seconds")
            st["next_run"] = (
                now + datetime.timedelta(hours=st["interval_hours"])
            ).isoformat(timespec="seconds")
            SETTINGS_FILE.write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            _log.warning("自动扫描循环异常", exc_info=True)
