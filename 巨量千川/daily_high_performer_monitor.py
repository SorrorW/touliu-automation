"""每日高ROI达人自动发现与出单视频追投。

扫描已知达人列表中的计划，找出单日 ROI≥11 且成交≥5 单的计划，
自动对符合条件的计划创建素材追投（针对出单视频）。

区别于 big_creator_monitor.py（低效订单兜底追投），本模块聚焦
高ROI高效达人，放大优质流量。

Run with ``python -m 巨量千川.daily_high_performer_monitor``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .chase_create import create_chase
from .plan_metrics import PlanMetrics, list_all_plans, search_plan_metrics
from .session_prepare import ACCOUNT_BY_DEPARTMENT

# ---------------------------------------------------------------------------
# 配置常量（可直接修改）
# ---------------------------------------------------------------------------

# 触发阈值：单日 ROI ≥ 此值 且 成交 ≥ 此值
MIN_ROI = 11.0
MIN_ORDERS = 5

# 追投参数
CHASE_BUDGET = 100
CHASE_HOURS = 24.0

# 同一天内同一达人最多追投次数
MAX_DAILY_CHASE_PER_CREATOR = 1

# 状态文件路径
STATE_FILE = Path(__file__).with_name("daily_high_performer_state.json")

# 从大达人监控状态文件加载已知达人（如果存在）
BIG_CREATOR_STATE_FILE = Path(__file__).with_name("big_creator_monitor_state.json")

# 手动补充达人列表（抖音号名称 — 用于按名称搜索计划）
# 仅当 BIG_CREATOR_STATE_FILE 为空或不完整时使用
FALLBACK_CREATORS: list[str] = []

# 达人与部门映射（优先级高于全局 --department）
CREATOR_DEPARTMENTS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 状态管理
# ---------------------------------------------------------------------------

UTC = timezone.utc


@dataclass
class HighPerformerState:
    """追踪单个达人的高ROI追投状态。"""

    last_chase_date: str | None = None  # "2026-07-27"
    chase_count_today: int = 0
    plan_id: str | None = None
    task_id: str | None = None
    roi: float | None = None
    orders: int = 0


def _today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_state(path: Path = STATE_FILE) -> dict[str, HighPerformerState]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: HighPerformerState(**values) for name, values in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid high-performer state file: {path}: {exc}") from exc


def save_state(states: dict[str, HighPerformerState], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: asdict(state) for name, state in states.items()}
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _load_known_creators() -> list[str]:
    """从大达人状态文件加载已知达人列表，合并手动补充列表。"""
    creators: set[str] = set(FALLBACK_CREATORS)

    big_state = BIG_CREATOR_STATE_FILE
    if big_state.exists():
        try:
            raw = json.loads(big_state.read_text(encoding="utf-8"))
            creators.update(raw.keys())
        except (OSError, json.JSONDecodeError):
            pass

    return sorted(creators)


# ---------------------------------------------------------------------------
# 扫描与筛选
# ---------------------------------------------------------------------------


async def scan_and_chase(
    creator: str,
    state: HighPerformerState,
    *,
    department: str,
    metrics_reader: Callable[..., Awaitable[list[PlanMetrics]]] = search_plan_metrics,
    chase_creator: Callable[..., Awaitable[dict[str, Any]]] = create_chase,
) -> dict[str, Any]:
    """扫描单个达人计划，符合条件则追投。

    Returns:
        {"creator": ..., "action": "chase"|"skip"|"error", ...}
    """
    today = _today_str()
    result: dict[str, Any] = {
        "creator": creator,
        "department": department,
        "action": "skip",
        "chase_created": False,
    }

    # 今日已达上限，跳过
    if state.last_chase_date == today and state.chase_count_today >= MAX_DAILY_CHASE_PER_CREATOR:
        result["reason"] = f"今日已追投{state.chase_count_today}次，已达上限"
        return result

    # 查询计划指标
    plans = await metrics_reader(creator_name=creator, department=department)
    if not plans:
        result["action"] = "error"
        result["error"] = f"未找到达人计划: {creator}"
        return result

    plan = plans[0]
    roi = plan.roi
    orders = plan.orders

    result.update({
        "plan_name": plan.plan_name,
        "plan_id": plan.plan_id,
        "roi": roi,
        "orders": orders,
        "spent": plan.spent,
    })

    # 判断是否达标
    roi_ok = roi is not None and roi >= MIN_ROI
    orders_ok = orders is not None and orders >= MIN_ORDERS

    if not roi_ok or not orders_ok:
        missing = []
        if not roi_ok:
            missing.append(f"ROI={roi}<{MIN_ROI}")
        if not orders_ok:
            missing.append(f"成交={orders}<{MIN_ORDERS}")
        result["reason"] = "不满足阈值: " + "，".join(missing)
        return result

    # 达标 — 创建追投
    result["action"] = "chase"
    try:
        created = await chase_creator(
            creator_name=creator,
            main_plan_id=plan.plan_id,
            department=department,
            budget=CHASE_BUDGET,
            hours=CHASE_HOURS,
        )
    except Exception as exc:
        result["action"] = "error"
        result["error"] = f"追投创建失败: {exc}"
        return result

    task_id = created.get("task_id")
    result.update({
        "chase_created": True,
        "task_id": task_id,
        "chase_message": created.get("message", ""),
    })

    # 更新状态
    state.last_chase_date = today
    state.chase_count_today = (state.chase_count_today if state.last_chase_date == today else 0) + 1
    state.plan_id = plan.plan_id
    state.task_id = task_id
    state.roi = roi
    state.orders = orders or 0

    return result


async def scan_all_plans(
    creators: list[str] | None = None,
    *,
    department: str,
    progress_cb=None,  # Callable[[int, int], None] — (已读计划数, 总条数)，跨部门聚合
) -> dict[str, Any]:
    """全表扫描：按部门列出总览页全部计划并标记达标（不追投、不写状态）。

    department 传 "all" 时按 业务1部→业务2部→业务3部 顺序逐个扫描。
    creators 传入时按计划名提取的达人名过滤。
    """
    departments = list(ACCOUNT_BY_DEPARTMENT) if department == "all" else [department]

    # 跨部门聚合进度：reads/totals 按部门记录，总和作为全局进度
    reads: dict[str, int] = {}
    totals: dict[str, int] = {}

    def _dep_progress(dep: str):
        def _cb(read: int, total: int) -> None:
            reads[dep] = read
            totals[dep] = total
            if progress_cb:
                progress_cb(sum(reads.values()), sum(totals.values()))
        return _cb

    report: dict[str, Any] = {}
    for dep in departments:
        try:
            plans = await list_all_plans(
                department=dep,
                progress_cb=_dep_progress(dep) if progress_cb else None,
            )
        except Exception as exc:
            report[dep] = {"error": str(exc)}
            continue
        entries: list[dict[str, Any]] = []
        for p in plans:
            if creators and p.creator_name not in creators:
                continue
            qualified = (
                p.roi is not None and p.roi >= MIN_ROI
                and p.orders is not None and p.orders >= MIN_ORDERS
            )
            entries.append({
                "plan_name": p.plan_name,
                "plan_id": p.plan_id,
                "status": p.status,
                "creator_name": p.creator_name,
                "spent": p.spent,
                "roi": p.roi,
                "orders": p.orders,
                "qualified": qualified,
            })
        report[dep] = {
            "total_plans": len(plans),
            "qualified_count": sum(1 for e in entries if e["qualified"]),
            "plans": entries,
        }
    return report


async def run_monitor(
    creators: list[str] | None = None,
    *,
    department: str,
    state_path: Path = STATE_FILE,
) -> list[dict[str, Any]]:
    """扫描全部已知达人并追投达标计划。

    Args:
        creators: 达人列表（None 则从状态文件 + FALLBACK 加载）
        department: 默认部门
        state_path: 高ROI状态文件
    """
    resolved_creators = creators if creators is not None else _load_known_creators()
    if not resolved_creators:
        raise RuntimeError(
            "无可用达人列表。请确保 big_creator_monitor_state.json 存在，"
            "或在 FALLBACK_CREATORS 中手动添加达人名称。"
        )

    states = load_state(state_path)
    results: list[dict[str, Any]] = []

    for creator in resolved_creators:
        state = states.setdefault(creator, HighPerformerState())
        creator_department = CREATOR_DEPARTMENTS.get(creator, department)

        try:
            result = await scan_and_chase(
                creator, state, department=creator_department
            )
        except Exception as exc:
            result = {
                "creator": creator,
                "department": creator_department,
                "action": "error",
                "error": str(exc),
            }
        results.append(result)
        save_state(states, state_path)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="每日高ROI达人扫描与出单视频追投"
    )
    parser.add_argument(
        "--department",
        default="业务3部",
        choices=tuple(ACCOUNT_BY_DEPARTMENT) + ("all",),
        help="默认部门（达人未单独配置时使用）；all = 依次扫描业务1/2/3部（仅 --dry-run）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅扫描不追投：按部门全表扫描所有计划，输出ROI/成交及是否达标",
    )
    parser.add_argument(
        "--creator",
        nargs="*",
        help="按达人名过滤扫描结果（dry-run）；或指定达人列表（覆盖自动加载）",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.dry_run:
        try:
            report = asyncio.run(
                scan_all_plans(
                    creators=args.creator if args.creator else None,
                    department=args.department,
                )
            )
        except Exception as exc:
            print(
                json.dumps(
                    {"status": "failed", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "dry_run",
                    "thresholds": {"min_roi": MIN_ROI, "min_orders": MIN_ORDERS},
                    "departments": report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.department == "all":
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "--department all 目前仅支持 --dry-run 数据收集模式",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    try:
        results = asyncio.run(
            run_monitor(
                creators=args.creator if args.creator else None,
                department=args.department,
            )
        )

        chased = [r for r in results if r.get("chase_created")]
        errors = [r for r in results if r.get("action") == "error"]
        print(
            json.dumps(
                {
                    "status": "ok" if not errors else "partial",
                    "total_scanned": len(results),
                    "chase_created": len(chased),
                    "errors": len(errors),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        return 0 if not any(r.get("action") == "error" for r in results) else 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
