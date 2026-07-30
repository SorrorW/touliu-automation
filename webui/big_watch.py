"""重点关注大达人：复用 big_creator_monitor 的状态机规则做评估与一键追投。

规则（与 巨量千川.big_creator_monitor 完全一致）：
- 近七天单量 < 7 为低效 → 追投 100 元 / 24 小时
- 最多追投 2 次仍低效 → 休息 7 天
- 追投执行中 / 休息期内不重复追投

评估用依赖注入把 create_chase 换成预览桩，走同一条代码路径但不真实追投、
不写状态文件；一键追投才走真实 create_chase 并落状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from 巨量千川.big_creator_monitor import (
    BIG_CREATORS,
    CHASE_BUDGET,
    CHASE_HOURS,
    CREATOR_DEPARTMENTS,
    POOR_ORDER_THRESHOLD,
    STATE_FILE,
    evaluate_creator,
    load_state,
    save_state,
)
from 巨量千川.session_prepare import ACCOUNT_BY_DEPARTMENT

from .logging_setup import get_logger

_log = get_logger(__name__)

WATCHLIST_FILE = Path(__file__).with_name("assets") / "watchlist.json"

RULES = {
    "poor_order_threshold": POOR_ORDER_THRESHOLD,
    "chase_budget": CHASE_BUDGET,
    "chase_hours": CHASE_HOURS,
}


def _webui_watchlist() -> list[dict[str, str]]:
    """读取关注列表；文件不存在时按当前大达人播种（一次性迁移）。"""
    if not WATCHLIST_FILE.exists():
        seed: dict[str, str] = {}
        for name in BIG_CREATORS:
            seed[name] = CREATOR_DEPARTMENTS.get(name, "业务3部")
        if STATE_FILE.exists():
            for name in load_state(STATE_FILE):
                seed.setdefault(name, CREATOR_DEPARTMENTS.get(name, "业务3部"))
        items = [{"name": n, "department": d} for n, d in seed.items()]
        _save_watchlist(items)
        _log.info("关注列表播种: %s", [i["name"] for i in items])
        return items
    try:
        raw = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_watchlist(items: list[dict]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_creators() -> list[dict[str, Any]]:
    """关注的大达人清单：watchlist.json 是唯一数据源，页面全权维护。"""
    return _webui_watchlist()


def add_creator(name: str, department: str) -> list[dict[str, Any]]:
    """添加关注（写文件）。重名报错。"""
    name = name.strip()
    if not name:
        raise ValueError("达人名不能为空")
    if department not in ACCOUNT_BY_DEPARTMENT:
        raise ValueError(f"无效部门: {department}")
    items = _webui_watchlist()
    if any(i["name"] == name for i in items):
        raise ValueError(f"「{name}」已在关注列表中")
    items.append({"name": name, "department": department})
    _save_watchlist(items)
    _log.info("关注列表添加: %s（%s）", name, department)
    return items


def remove_creator(name: str) -> list[dict[str, Any]]:
    """移除关注（写文件）。不存在报错。"""
    items = _webui_watchlist()
    remaining = [i for i in items if i["name"] != name]
    if len(remaining) == len(items):
        raise ValueError(f"关注列表中没有「{name}」")
    _save_watchlist(remaining)
    _log.info("关注列表移除: %s", name)
    return remaining


async def _preview_chase(**kwargs: Any) -> dict[str, Any]:
    """预览桩：代替 create_chase，只记录"会追投"，不真实创建。"""
    return {"task_id": "PREVIEW", "message": "预览"}


async def evaluate_all(progress_cb=None) -> list[dict[str, Any]]:
    """逐个评估是否符合追投（不真实追投、不写状态）。

    progress_cb: Callable[[int, int], None]，每评估完一个达人上报 (已完成, 总数)。
    """
    states = load_state(STATE_FILE)
    items = list_creators()
    results = []
    for idx, item in enumerate(items, 1):
        name, department = item["name"], item["department"]
        state = states.get(name)
        if state is None:
            from 巨量千川.big_creator_monitor import CreatorState

            state = CreatorState()
        entry: dict[str, Any] = {**item}
        pre_status = state.status  # 预览评估会改内存状态，展示用评估前的状态
        try:
            result = await evaluate_creator(
                name, state, department=department, chase_creator=_preview_chase
            )
            result.pop("task_id", None)  # 预览桩的占位 id 不下发
            entry.update(result)
            entry["eligible"] = result.get("action") == "chase"
        except Exception as exc:
            entry.update({"action": "error", "error": str(exc), "eligible": False})
        entry["machine_status"] = pre_status
        results.append(entry)
        if progress_cb:
            progress_cb(idx, len(items))
    return results


async def chase_one(name: str, department: str) -> dict[str, Any]:
    """对单个大达人执行一次状态机评估并真实追投（写状态文件）。

    状态机内部会拦截：休息期/追投中/已达 2 次上限/单量达标 都不会追投。
    """
    if department not in ACCOUNT_BY_DEPARTMENT:
        raise ValueError(f"无效部门: {department}")
    states = load_state(STATE_FILE)
    from 巨量千川.big_creator_monitor import CreatorState

    state = states.setdefault(name, CreatorState())
    result = await evaluate_creator(name, state, department=department)
    save_state(states, STATE_FILE)
    if result.get("chase_created"):
        _log.info("大达人一键追投: %s（%s）task_id=%s", name, department, result.get("task_id"))
    else:
        _log.info("大达人一键追投未触发: %s（%s）%s", name, department, result.get("reason", ""))
    result["machine_status"] = state.status
    return result
