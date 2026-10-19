# -*- coding: utf-8 -*-
"""投流生命周期判定。

这里只定义业务分类和计划动作，不连接浏览器、不修改千川计划。实际执行端必须
额外检查自动化总开关和人工白名单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


NEW_PLAN_ROI_TARGET = 5.0
NEW_PLAN_DAILY_BUDGET = 300
# 2026-10-09 用户规则：旧素材通道计划初始 ROI 6.5，但全程按新计划节奏走
# （08:00 开、盘中闸、升档链路一样，只是初始 ROI 不同，label=旧素材补投）。
# 判定「新计划形态」时必须同时认 4.5 和 6.5，否则 6.5 计划次日被误判出
# 新计划生命周期（盘中闸/升档全漏）。
# 2026-10-13 用户令：新建计划/老计划新增素材的创建 ROI 4.5→5.0。
# 识别集合保留 4.5=存量计划（按 4.5 建的还没跑完生命周期），新建只写 5.0。
NEW_PLAN_ROI_TARGETS = (NEW_PLAN_ROI_TARGET, 4.5, 6.5)  # 5.0 即 NEW_PLAN_ROI_TARGET（2026-10-13 用户令）；4.5=存量，6.5=旧素材


def is_new_plan_roi(roi_target) -> bool:
    """该 ROI 目标值是否属于「新计划形态」（4.5 或旧素材通道 6.5）。"""
    try:
        v = float(roi_target)
    except (TypeError, ValueError):
        return False
    return any(abs(v - t) < 0.001 for t in NEW_PLAN_ROI_TARGETS)
OLD_POOL_ROI_TARGET = 10.3
OLD_POOL_DAILY_BUDGET = 300
KEY_PLAN_ROI_TARGET = 10.3
# 2026-09-02 用户定：重点计划初始日预算 1000（原 99999 不限——ROI>10 改不限的
# growth 开关已关闭废止；告警加钱只在当天有效，23:00 收口归位到标签预算）。
KEY_PLAN_DAILY_BUDGET = 1000
KEY_MIN_ROI = 10.0
KEY_MIN_ORDERS = 10
# 2026-09-01 用户定：转换成潜力计划也要看订单数，>= 4 单才有效
#（此前 2026-08-24 策略是 6<ROI<10 进潜力不问订单数，已废止）。
POTENTIAL_MIN_ORDERS = 4
# 2026-08-21 起：潜力计划收口时改写为保 ROI 7.3、日预算 300（此前不改参数）。
POTENTIAL_PLAN_ROI_TARGET = 7.3
POTENTIAL_PLAN_DAILY_BUDGET = 300

PlanCategory = Literal[
    "new_plan", "old_pool", "key_plan", "potential_plan", "manual_review",
    # invalid_plan 不由本函数判定：plan_power.monitor 对已核验 15 天 0 单计划的覆盖标签
    "invalid_plan",
]


@dataclass(frozen=True)
class LifecycleDecision:
    category: PlanCategory
    reason: str
    roi_target: float | None
    daily_budget: int | None
    keep_running_at_close: bool


def classify_plan_lifecycle(
    *,
    roi: float | None,
    orders: int | None,
    is_today_new: bool,
    manual_label: str | None = None,
    name_label: str | None = None,
) -> LifecycleDecision:
    """按当前流程图对单条计划分类。

    今日新计划在尚无有效 ROI 时继续测试；一旦有可判定的表现数据，按结果进入
    重点、潜力或老计划池。订单数 ``>= 10`` 用于重点计划判定；潜力计划要求
    订单数 ``>= 4``（2026-09-01 用户定，此前 6<ROI<10 不问订单数的口径废止）。
    """
    if manual_label:
        return LifecycleDecision(
            "manual_review", f"人工白名单：{manual_label}，AI 不改计划", None, None, False
        )

    safe_orders = max(int(orders or 0), 0)
    if roi is not None and roi > KEY_MIN_ROI and safe_orders >= KEY_MIN_ORDERS:
        return LifecycleDecision(
            "key_plan", "ROI > 10 且订单数 >= 10，转入重点计划",
            KEY_PLAN_ROI_TARGET, KEY_PLAN_DAILY_BUDGET, True,
        )
    if (roi is not None and roi > KEY_MIN_ROI
            and POTENTIAL_MIN_ORDERS <= safe_orders < KEY_MIN_ORDERS):
        return LifecycleDecision(
            "potential_plan", "ROI > 10 且 4 <= 订单数 < 10，转入潜力计划",
            POTENTIAL_PLAN_ROI_TARGET, POTENTIAL_PLAN_DAILY_BUDGET, False,
        )
    if roi is not None and 6 < roi < KEY_MIN_ROI and safe_orders >= POTENTIAL_MIN_ORDERS:
        return LifecycleDecision(
            "potential_plan", "6 < ROI < 10 且订单数 >= 4，转入潜力计划",
            POTENTIAL_PLAN_ROI_TARGET, POTENTIAL_PLAN_DAILY_BUDGET, False,
        )

    # 00:00-08:00 创建的新计划，尚未产生有效 ROI 时保留到当天监控时段测试。
    if is_today_new and roi is None:
        return LifecycleDecision(
            "new_plan", "今日新计划尚无有效 ROI，继续按 ROI 4.5、预算 300 测试",
            NEW_PLAN_ROI_TARGET, NEW_PLAN_DAILY_BUDGET, False,
        )

    return LifecycleDecision(
        "old_pool", "未达到重点或潜力条件，归入老计划池",
        OLD_POOL_ROI_TARGET, OLD_POOL_DAILY_BUDGET, False,
    )
