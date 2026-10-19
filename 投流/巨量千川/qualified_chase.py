"""达标达人自动追投（2026-08-17 新追投标准）。

标准（用户定）：只有达标达人才追投 —— 达人**近 30 天**成交单量 ≥
三个店（业务1/2/3部）最高达人**近 30 天**销售单量 × 0.6。

用法：
  - webui 全部门扫描完成后每天自动评估一次（月度数据日内变化小，无需每轮扫）；
  - CLI 手动：python -m 巨量千川.qualified_chase（重新扫近30天，评估+追投）
             python -m 巨量千川.qualified_chase --dry-run（只评估不追投）

追投目标：该达人近 30 天单量最高的「投放中」计划（plan_id 精确搜索，
昵称搜索抖音号框不可靠）；达人没有投放中计划时记跳过（不追已暂停计划）。
平台对已有追投任务的计划返回「已有追投任务」，记为跳过，不重复扣费。
追投定额：100 元 / 24 小时（CHASE_BUDGET/CHASE_HOURS，与一键追投一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .chase_create import create_chase
from .daily_high_performer_monitor import CHASE_BUDGET, CHASE_HOURS, scan_all_plans
from .manual_whitelist import is_manual_whitelisted

QUALIFIED_RATIO = 0.6      # 达标线 = 三店最高达人月单量 × 0.6（2026-08-17 用户定）
MONTH_RANGE = "近30天"     # 总览页日期快捷选项原文（单量统计窗口，用户定：一个月内）


def evaluate_qualified(report: dict) -> dict:
    """从「近30天」全部门扫描报告计算达标线并挑出达标达人。

    返回 {max_orders, threshold, creator_orders: {"部门|达人": 单量},
          qualified: [{"department", "creator", "orders", "plan_id", "plan_name"}]}
    单量按达人汇总（一个达人可能有多条计划，含已暂停的——资格看历史单量）；
    跨部门取最高者 ×0.6 为线。追投目标只从「投放中」计划里选（单量最高者），
    没有投放中计划的达人 plan_id 为空，由调用方记跳过。
    """
    orders_by_creator: dict[tuple[str, str], int] = {}
    best_active: dict[tuple[str, str], dict] = {}
    for dep, rec in (report or {}).items():
        for p in (rec or {}).get("plans") or []:
            name = p.get("creator_name")
            if is_manual_whitelisted(
                creator=name or "",
                department=dep,
                plan_id=p.get("plan_id") or "",
                plan_name=p.get("plan_name") or "",
            ):
                continue
            orders = p.get("orders")
            if not name or orders is None:
                continue
            key = (dep, name)
            orders = int(orders)
            orders_by_creator[key] = orders_by_creator.get(key, 0) + orders
            if "投放中" not in (p.get("status") or ""):
                continue  # 追投目标只选投放中计划
            cur = best_active.get(key)
            if cur is None or orders > cur["orders"]:
                best_active[key] = {
                    "orders": orders,
                    "plan_id": p.get("plan_id") or "",
                    "plan_name": p.get("plan_name") or "",
                }

    max_orders = max(orders_by_creator.values(), default=0)
    threshold = max_orders * QUALIFIED_RATIO
    qualified = [
        {
            "department": dep,
            "creator": name,
            "orders": n,
            "plan_id": (best_active.get((dep, name)) or {}).get("plan_id", ""),
            "plan_name": (best_active.get((dep, name)) or {}).get("plan_name", ""),
        }
        for (dep, name), n in sorted(orders_by_creator.items(), key=lambda kv: -kv[1])
        if max_orders > 0 and n >= threshold and n > 0
    ]
    return {
        "max_orders": max_orders,
        "threshold": round(threshold, 2),
        "range": MONTH_RANGE,
        "creator_orders": {f"{dep}|{name}": n for (dep, name), n in orders_by_creator.items()},
        "qualified": qualified,
    }


async def chase_qualified(report: dict) -> dict:
    """对「近30天」扫描报告里的达标达人逐个建追投（100元/24h）。返回执行明细。"""
    ev = evaluate_qualified(report)
    chased: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for q in ev["qualified"]:
        label = f"{q['creator']}（{q['department']}，月{q['orders']}单）"
        if not q["plan_id"]:
            skipped.append(f"{label}（无投放中计划，未追投）")
            continue
        try:
            result = await create_chase(
                main_plan_id=q["plan_id"],
                department=q["department"],
                budget=CHASE_BUDGET,
                hours=CHASE_HOURS,
            )
            chased.append(f"{label} task_id={result.get('task_id')}")
        except Exception as exc:
            msg = str(exc)
            if "已有追投任务" in msg:
                skipped.append(f"{label}（已有追投任务）")
            else:
                failed.append(f"{label}: {msg[:120]}")

    return {
        "success": not failed,
        "threshold": ev["threshold"],
        "max_orders": ev["max_orders"],
        "range": MONTH_RANGE,
        "qualified_count": len(ev["qualified"]),
        "chased": chased,
        "skipped": skipped,
        "failed": failed,
        "message": (
            f"达标线≥{ev['threshold']:g}单（{MONTH_RANGE}三店最高{ev['max_orders']}单×{QUALIFIED_RATIO}），"
            f"达标{len(ev['qualified'])}人：追投{len(chased)}、跳过{len(skipped)}、失败{len(failed)}"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="达标达人自动追投（近30天 0.6×三店最高单量）")
    parser.add_argument("--dry-run", action="store_true", help="只评估不追投")
    args = parser.parse_args()

    async def _run() -> dict:
        report = await scan_all_plans(department="all", time_range=MONTH_RANGE)
        if args.dry_run:
            return {"success": True, "dry_run": True, **evaluate_qualified(report)}
        return await chase_qualified(report)

    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
