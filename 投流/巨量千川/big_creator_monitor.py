"""Monitor designated big creators and create bounded chase tasks.

The creator list and policy constants intentionally live at the top of this
file so the module can be copied between workspaces without a separate config.
Run with ``python -m 巨量千川.big_creator_monitor``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .chase_create import create_chase
from .manual_whitelist import is_manual_whitelisted, manual_label
from .plan_metrics import PlanMetrics, search_plan_metrics
from .session_prepare import ACCOUNT_BY_DEPARTMENT

# Move or replace names here when migrating the feature.
BIG_CREATORS = [
    "彤妈爱美食",
    "大梦吴恙",
    # "抖音号或达人名称",
]
CREATOR_DEPARTMENTS = {
    "彤妈爱美食": "业务1部",
    "大梦吴恙": "业务3部",
    "味蕾食光": "业务1部",      # 计划「705- 味蕾食光-郑-重点」在业务1部
    "小月美食记": "业务1部",    # 计划「0428- 小月美食记-郑-重点」在业务1部
}
POOR_ORDER_THRESHOLD = 7
CHASE_BUDGET = 100
CHASE_HOURS = 24.0
MAX_CHASE_COUNT = 2
REST_DAYS = 7
STATE_FILE = Path(__file__).with_name("big_creator_monitor_state.json")

UTC = timezone.utc


@dataclass
class CreatorState:
    status: str = "normal"
    chase_count: int = 0
    plan_id: str | None = None
    task_id: str | None = None
    chase_started_at: str | None = None
    chase_ends_at: str | None = None
    rest_started_at: str | None = None
    rest_ends_at: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def load_state(path: Path = STATE_FILE) -> dict[str, CreatorState]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: CreatorState(**values) for name, values in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid monitor state file: {path}: {exc}") from exc


def save_state(states: dict[str, CreatorState], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: asdict(state) for name, state in states.items()}
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _is_poor(orders: int | None) -> bool:
    if orders is None:
        raise ValueError("页面未提供可识别的订单数")
    return orders < POOR_ORDER_THRESHOLD


def _finish_chase(state: CreatorState, now: datetime) -> None:
    state.chase_started_at = None
    state.chase_ends_at = None
    state.task_id = None
    state.status = "normal"


async def evaluate_creator(
    creator: str,
    state: CreatorState,
    *,
    department: str,
    metrics_reader: Callable[[str, str], Awaitable[list[PlanMetrics]]] = search_plan_metrics,
    chase_creator: Callable[..., Awaitable[dict[str, Any]]] = create_chase,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    result: dict[str, Any] = {"creator": creator, "status": state.status, "action": "skip", "chase_created": False}

    if is_manual_whitelisted(creator=creator, department=department):
        result["reason"] = f"{manual_label(creator=creator, department=department)}人工白名单，仅读取不追投"
        return result

    if state.status == "rest":
        rest_end = _parse_time(state.rest_ends_at)
        if rest_end and now < rest_end:
            result["reason"] = "休息期未结束"
            result["rest_ends_at"] = state.rest_ends_at
            return result
        state.status = "normal"
        state.chase_count = 0
        state.rest_started_at = state.rest_ends_at = None

    if state.status.startswith("chase_"):
        chase_end = _parse_time(state.chase_ends_at)
        if chase_end and now < chase_end:
            result["status"] = state.status
            result["reason"] = "追投任务仍在执行"
            result["task_id"] = state.task_id
            return result

    # search_plan_metrics has an optional plan_name as its second parameter;
    # use keywords so the department cannot be mistaken for the search term.
    # 大达人监控需要近7天数据做低效判定，而非页面默认的"今天"
    plans = await metrics_reader(creator_name=creator, department=department, time_range="last_7_days")
    if not plans:
        raise RuntimeError(f"未找到达人计划: {creator}")
    # 达人可能有多条计划（新建计划会顶到 plans[0]），低效判定按达人近7天
    # 全部计划聚合的单量；追投目标选消耗最高的主计划
    known_orders = [p.orders for p in plans if p.orders is not None]
    if not known_orders:
        raise RuntimeError(f"未读取到订单数: {creator}")
    total_orders = sum(known_orders)
    plan = max(plans, key=lambda p: (p.spent or 0.0))
    result.update({"orders": total_orders, "plan_id": plan.plan_id})
    state.plan_id = plan.plan_id or state.plan_id
    poor = _is_poor(total_orders)
    result["poor"] = poor

    if not poor:
        _finish_chase(state, now)
        state.chase_count = 0
        result.update({"status": state.status, "reason": "近七天单量达标"})
        return result

    if state.status.startswith("chase_") and state.chase_count >= MAX_CHASE_COUNT:
        state.status = "rest"
        state.rest_started_at = _iso(now)
        state.rest_ends_at = _iso(now + timedelta(days=REST_DAYS))
        state.task_id = None
        result.update({"status": "rest", "reason": "两次追投后仍低效", "rest_ends_at": state.rest_ends_at})
        return result

    created = await chase_creator(
        creator_name=creator,
        main_plan_id=plan.plan_id,
        department=department,
        budget=CHASE_BUDGET,
        hours=CHASE_HOURS,
    )
    task_id = created.get("task_id")
    if not task_id:
        raise RuntimeError(f"追投创建成功但未返回 task_id: {created}")
    state.chase_count += 1
    state.status = f"chase_{state.chase_count}"
    state.task_id = str(task_id)
    state.chase_started_at = _iso(now)
    state.chase_ends_at = _iso(now + timedelta(hours=CHASE_HOURS))
    result.update({"status": state.status, "action": "chase", "chase_created": True, "task_id": state.task_id})
    return result


async def run_monitor(
    creators: list[str] | None = None,
    *,
    department: str,
    state_path: Path = STATE_FILE,
) -> list[dict[str, Any]]:
    creators = creators if creators is not None else BIG_CREATORS
    states = load_state(state_path)
    results = []
    for creator in creators:
        state = states.setdefault(creator, CreatorState())
        creator_department = CREATOR_DEPARTMENTS.get(creator, department)
        try:
            result = await evaluate_creator(creator, state, department=creator_department)
            result["department"] = creator_department
        except Exception as exc:
            result = {"creator": creator, "status": state.status, "action": "error", "error": str(exc)}
        results.append(result)
        save_state(states, state_path)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="监控大达人近七天单量并执行追投轮转")
    parser.add_argument("--department", default="业务3部", choices=tuple(ACCOUNT_BY_DEPARTMENT))
    args = parser.parse_args()
    try:
        results = asyncio.run(run_monitor(department=args.department))
        print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False, indent=2))
        return 0 if not any(item.get("action") == "error" for item in results) else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
