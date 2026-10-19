from __future__ import annotations

import asyncio
import datetime
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from 巨量千川 import plan_power, reconcile

# 单测不污染生产日志（plan_power 模块级 FileHandler 会写 logs/plan_power_YYYYMMDD.log）
plan_power.log.handlers = [
    h for h in plan_power.log.handlers
    if not isinstance(h, logging.FileHandler)
]


class NoonNewPlanStageTests(unittest.TestCase):
    def _entry(self, **overrides):
        item = {
            "plan_id": "p1", "plan_name": "0825_达人A_赵", "department": "业务1部",
            "status": "投放中", "category": "new_plan", "is_today_new": True,
            "current_roi_target": 4.5, "roi_target": 4.5,
            "spent": 120.0, "roi": 4.5, "orders": 1,
        }
        item.update(overrides)
        return item


    def test_post_action_scan_slot_mapping(self) -> None:
        self.assertEqual(plan_power._post_action_scan_slot("resume-old-pool"), "12:00")
        self.assertEqual(plan_power._post_action_scan_slot("pause-new-low-roi"), "12:00")
        self.assertEqual(plan_power._post_action_scan_slot("pause-old-pool"), "17:00")
        self.assertEqual(plan_power._post_action_scan_slot("close-old-pool"), "23:00")
        self.assertEqual(plan_power._post_action_scan_slot("pause-one"), "08:00")

    def test_budget_top_up_leaves_300_remaining_without_fixed_increment(self):
        result = plan_power._top_up_budget_to_remaining(120, 300)
        self.assertEqual(result["desired_budget"], 420)
        self.assertEqual(result["increase"], 120)
        self.assertEqual(result["remaining_after"], 300)

    def test_budget_is_not_reduced_when_already_has_300_remaining(self):
        result = plan_power._top_up_budget_to_remaining(120, 500)
        self.assertEqual(result["desired_budget"], 500)
        self.assertEqual(result["increase"], 0)
        self.assertEqual(result["remaining_after"], 380)

    def test_noon_zero_spend_is_closed_as_not_qualified(self):
        report = {"plans": {"业务1部": [self._entry(spent=0, roi=None)]}}
        targets, errors = plan_power._new_plan_low_roi_targets(
            report, 4.5, close_zero_spend=True
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(targets["业务1部"]), 1)
        self.assertIn("消耗为0", targets["业务1部"][0]["circuit_reason"])

    def test_zero_spend_remains_outside_the_17pm_roi_only_selector(self):
        report = {"plans": {"业务1部": [self._entry(spent=0, roi=None)]}}
        targets, _ = plan_power._new_plan_low_roi_targets(report, 6.0)
        self.assertEqual(targets, {})

    def test_afternoon_promotion_accepts_14_and_16_slots(self):
        entry = self._entry(plan_id="pass")
        report = {"plans": {"业务1部": [entry]}}
        self.assertEqual([i["plan_id"] for i in plan_power._new_plan_noon_promotion_targets(report, scan_slot="14:00")["业务1部"]], ["pass"])
        self.assertEqual(plan_power._new_plan_noon_promotion_targets(report, scan_slot="16:00")["业务1部"][0]["promotion_reason"].startswith("16:00"), True)

    def test_noon_promotion_requires_current_target_4_5_and_passing_roi(self):
        report = {"plans": {"业务1部": [
            self._entry(plan_id="pass"),
            self._entry(plan_id="already", current_roi_target=7.3, roi_target=7.3, roi=7.0),
            self._entry(plan_id="low", roi=4.49),
        ]}}
        targets = plan_power._new_plan_noon_promotion_targets(report)
        self.assertEqual([i["plan_id"] for i in targets["业务1部"]], ["pass"])

    def test_1700_pauses_advanced_7_3_new_plan_but_keeps_final_category_deferred(self):
        report = {"plans": {"业务1部": [
            self._entry(plan_id="advanced", current_roi_target=7.3, roi_target=7.3, roi=5.2),
            self._entry(plan_id="base", current_roi_target=4.5, roi_target=4.5, roi=5.2),
        ]}}
        targets, _ = plan_power._new_plan_low_roi_targets(report, 6.0)
        self.assertEqual(
            [i["plan_id"] for i in targets["业务1部"]], ["advanced", "base"]
        )

    def test_1700_paused_new_plan_is_not_an_old_pool_reconcile_target(self):
        entry = self._entry(
            current_roi_target=7.3, roi_target=7.3, roi=5.2,
            paused_at_17=True, status="已暂停", new_plan_stage="roi_7_3",
            daily_budget=400,
        )
        desired = reconcile.desired_state(entry, datetime.datetime(2026, 8, 25, 17, 10))
        self.assertFalse(desired["should_run"])
        self.assertEqual(desired["category"], "new_plan")
        self.assertIn("23:00", desired["note"])

    def test_daytime_advanced_plan_is_still_new_plan(self):
        today = datetime.date.today().strftime("%m%d")
        plan = SimpleNamespace(
            plan_name=f"{today}_达人A_赵", plan_id="p1", status="投放中",
            spent=120.0, roi=7.0, orders=2, roi_target=7.3, budget=420,
        )
        entry = plan_power._lifecycle_entry("业务1部", plan, final_classification=False)
        self.assertEqual(entry["category"], "new_plan")
        self.assertEqual(entry["new_plan_stage"], "roi_7_3")
        self.assertEqual(entry["roi_target"], 7.3)
        self.assertEqual(entry["daily_budget"], 420)

    def test_actual_old_pool_roi_overrides_stale_potential_preservation(self):
        plan = SimpleNamespace(
            plan_name="0901_达人A_赵", plan_id="daily", status="投放中",
            spent=50.0, roi=5.5, orders=2, roi_target=10.3, budget=300,
        )
        entry = plan_power._lifecycle_entry(
            "业务3部", plan, final_classification=False,
            preserved_category="potential_plan",
        )
        self.assertEqual(entry["category"], "old_pool")
        self.assertEqual(entry["roi_target"], 10.3)
        self.assertEqual(entry["daily_budget"], 300)

    def test_2300_potential_label_preserves_promoted_budget(self):
        today = datetime.date.today().strftime("%m%d")
        plan = SimpleNamespace(
            plan_name=f"{today}_达人A_赵", plan_id="p1", status="投放中",
            spent=120.0, roi=7.0, orders=4, roi_target=7.3, budget=420,
        )
        entry = plan_power._lifecycle_entry("业务1部", plan, final_classification=True)
        self.assertEqual(entry["category"], "potential_plan")
        self.assertTrue(entry["preserve_promoted_budget"])
        desired = reconcile.desired_state(entry, datetime.datetime(2026, 8, 25, 12, 0))
        self.assertEqual(desired["roi_target"], 7.3)
        self.assertEqual(desired["budget"], 420)

    def test_2300_promoted_plan_with_under_4_orders_falls_to_old_pool(self):
        # 2026-09-01 用户定：转潜力须 >=4 单。午间升档计划若只有 2-3 单，
        # 23:00 终分类落老池、不保留升档预算（按老池口径改写 10.3/300）。
        today = datetime.date.today().strftime("%m%d")
        plan = SimpleNamespace(
            plan_name=f"{today}_达人A_赵", plan_id="p1", status="投放中",
            spent=120.0, roi=7.0, orders=2, roi_target=7.3, budget=420,
        )
        entry = plan_power._lifecycle_entry("业务1部", plan, final_classification=True)
        self.assertEqual(entry["category"], "old_pool")
        self.assertFalse(entry["preserve_promoted_budget"])

    def test_dry_run_reports_both_zero_spend_closure_and_promotion(self):
        report = {"plans": {"业务1部": [
            self._entry(plan_id="zero", spent=0, roi=None),
            self._entry(plan_id="pass", spent=120, roi=4.5),
        ]}}

        async def fake_monitor(*_args, **_kwargs):
            return report

        with (
            patch.object(plan_power, "monitor", fake_monitor),
            patch("webui.db.record_plan_daily_labels"),
        ):
            result = asyncio.run(plan_power.pause_new_low_roi(4.5, dry_run=True))
        self.assertEqual([i["plan_id"] for i in result["targets"]], ["zero"])
        self.assertEqual([i["plan_id"] for i in result["promotion_targets"]], ["pass"])


class PotentialGateTests(unittest.TestCase):
    """17:00/12:00 潜力降级闸（白板② + 2026-09-01 订单>=4 新规）选择器测试。"""

    def _pot(self, **overrides):
        item = {
            "plan_id": "pp1", "plan_name": "0830_潜A_黄", "department": "业务3部",
            "status": "投放中", "category": "potential_plan", "is_today_new": False,
            "spent": 100.0, "roi": 8.0, "orders": 5,
        }
        item.update(overrides)
        return item

    def _run(self, plans, threshold, slot):
        report = {"plans": {"业务3部": plans}}

        async def fake_monitor(*_args, **_kwargs):
            return report

        with patch.object(plan_power, "monitor", fake_monitor):
            return asyncio.run(plan_power.pause_potential_low_roi(
                threshold, dry_run=True, scan_slot=slot))

    def test_1700_gate_hits_under_4_orders_and_below_line(self):
        result = self._run([
            self._pot(plan_id="a", orders=1),                     # 订单<4
            self._pot(plan_id="b", roi=6.05, orders=39),          # 破 7.3 线
            self._pot(plan_id="c", roi=8.0, orders=5),            # 合格
            self._pot(plan_id="d", category="key_plan", roi=12, orders=20),
            self._pot(plan_id="e", status="已暂停"),              # 已停不动
            self._pot(plan_id="f", category="manual_review"),     # 白名单不动
            self._pot(plan_id="g", spent=0, orders=0),            # 0 单 → 命中
        ], 7.3, "17:00")
        self.assertEqual(sorted(i["plan_id"] for i in result["targets"]),
                         ["a", "b", "g"])
        self.assertEqual(result["scan_slot"], "17:00")

    def test_noon_gate_uses_6_line(self):
        result = self._run([
            self._pot(plan_id="a", roi=5.5, orders=8),
            self._pot(plan_id="b", roi=6.5, orders=8),
        ], 6.0, "12:00")
        self.assertEqual([i["plan_id"] for i in result["targets"]], ["a"])
        self.assertEqual(result["scan_slot"], "12:00")

    def test_roi_none_with_spend_counts_as_below_line(self):
        result = self._run([
            self._pot(plan_id="a", roi=None, orders=9, spent=50.0),
            self._pot(plan_id="b", roi=None, orders=9, spent=0.0),
        ], 7.3, "17:00")
        self.assertEqual([i["plan_id"] for i in result["targets"]], ["a"])

    def test_1700_gate_demotes_then_pauses_and_queues_next_day_resume(self):
        """2026-09-03 用户定「先暂停老计划，别浪费钱」：17:00 档降级即停+挂票。"""
        broken = self._pot(plan_id="broken", roi=5.59, orders=2)
        report = {"plans": {"业务3部": [broken]}}

        async def fake_monitor(*_args, **_kwargs):
            return report

        fake_rewrite = AsyncMock(return_value={
            "success": True,
            "targets": 1,
            "changed": ["broken"],
            "failed": [],
            "skipped": [],
            "roi": 10.3,
            "budget": 300,
        })

        class _FakePW:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *args):
                return False

        fake_batch = AsyncMock(return_value={
            "paused": [broken], "already": [], "failed": [],
        })
        with (
            patch.object(plan_power, "monitor", fake_monitor),
            patch("巨量千川.old_plan_pool.apply_old_pool", fake_rewrite),
            patch("webui.db.record_plan_daily_labels") as record_labels,
            patch.object(plan_power, "_queue_old_pool_resume") as queue_resume,
            patch.object(plan_power, "_ensure_cdp"),
            patch.object(plan_power, "async_playwright", lambda: _FakePW()),
            patch.object(plan_power, "connect_browser",
                         AsyncMock(return_value=SimpleNamespace(contexts=[object()]))),
            patch.object(plan_power, "_department_page", AsyncMock(return_value="page")),
            patch.object(plan_power, "_batch_pause_targets", fake_batch),
            patch.object(plan_power, "pause_plan") as pause_plan,
        ):
            result = asyncio.run(plan_power.pause_potential_low_roi(
                7.3, dry_run=False, scan_slot="17:00"))

        self.assertEqual(result["demoted_keep_running"], ["broken"])
        self.assertEqual([i["plan_id"] for i in result["demoted_paused"]], ["broken"])
        self.assertEqual(result["failed"], [])
        fake_rewrite.assert_awaited_once()
        fake_batch.assert_awaited_once()
        pause_plan.assert_not_called()  # 批量停无漏行，不触发逐条兜底
        queue_resume.assert_called_once()
        ticket = queue_resume.call_args.args[0][0]
        self.assertEqual(ticket["plan_id"], "broken")
        self.assertEqual(ticket["category"], "old_pool")
        self.assertEqual(ticket["status"], "已暂停")
        self.assertIn("暂停止血", ticket["reason"])
        labels = record_labels.call_args.args[0]["业务3部"]
        self.assertEqual(labels[0]["category"], "old_pool")
        self.assertEqual(labels[0]["status"], "已暂停")
        self.assertIn("暂停止血", labels[0]["reason"])
        self.assertEqual(labels[0]["roi_target"], 10.3)
        self.assertEqual(labels[0]["daily_budget"], 300)

    def test_noon_gate_demotes_and_keeps_running_without_pause_or_resume_queue(self):
        """12:00 档维持旧语义：转日常继续投放，17:00 老池停收编。"""
        report = {"plans": {"业务3部": [
            self._pot(plan_id="broken", roi=5.59, orders=2),
        ]}}

        async def fake_monitor(*_args, **_kwargs):
            return report

        fake_rewrite = AsyncMock(return_value={
            "success": True,
            "targets": 1,
            "changed": ["broken"],
            "failed": [],
            "skipped": [],
            "roi": 10.3,
            "budget": 300,
        })
        with (
            patch.object(plan_power, "monitor", fake_monitor),
            patch("巨量千川.old_plan_pool.apply_old_pool", fake_rewrite),
            patch("webui.db.record_plan_daily_labels") as record_labels,
            patch.object(plan_power, "_queue_old_pool_resume") as queue_resume,
            patch.object(plan_power, "_batch_pause_targets") as batch_pause,
            patch.object(plan_power, "pause_plan") as pause_plan,
        ):
            result = asyncio.run(plan_power.pause_potential_low_roi(
                6.0, dry_run=False, scan_slot="12:00"))

        self.assertEqual(result["demoted_keep_running"], ["broken"])
        self.assertEqual(result["demoted_paused"], [])
        self.assertEqual(result["failed"], [])
        fake_rewrite.assert_awaited_once()
        batch_pause.assert_not_called()
        pause_plan.assert_not_called()
        queue_resume.assert_not_called()
        labels = record_labels.call_args.args[0]["业务3部"]
        self.assertEqual(labels[0]["category"], "old_pool")
        self.assertEqual(labels[0]["status"], "投放中")
        self.assertIn("继续投放", labels[0]["reason"])
        self.assertEqual(labels[0]["roi_target"], 10.3)
        self.assertEqual(labels[0]["daily_budget"], 300)

    def test_old_pool_restore_window_is_limited_to_midday_delivery(self):
        self.assertFalse(plan_power._old_pool_delivery_window("11:59"))
        self.assertTrue(plan_power._old_pool_delivery_window("12:00"))
        self.assertTrue(plan_power._old_pool_delivery_window("17:09"))
        self.assertFalse(plan_power._old_pool_delivery_window("17:10"))

    def test_restore_pause_exceptions_keeps_manual_pause_untouched(self):
        report = {"plans": {"业务3部": [
            self._pot(plan_id="auto-paused", status="已暂停"),
            self._pot(plan_id="manual-paused", status="已暂停", category="manual_review"),
        ]}}

        async def fake_monitor(*_args, **_kwargs):
            return report

        with (
            patch.object(plan_power, "monitor", fake_monitor),
            patch.object(plan_power, "_save_pause_snapshot", return_value="snapshot.json"),
            patch.object(plan_power, "is_manual_whitelisted", side_effect=lambda **kw: kw["plan_id"] == "manual-paused"),
        ):
            result = asyncio.run(plan_power.restore_pause_exceptions(dry_run=True))

        self.assertEqual([item["plan_id"] for item in result["targets"]], ["auto-paused"])
        self.assertEqual([item["plan_id"] for item in result["skipped"]], ["manual-paused"])
        self.assertEqual(result["skipped"][0]["detail"], "人工白名单，不自动恢复")

    def test_post_action_scan_slot_for_potential_gate(self):
        self.assertEqual(plan_power._post_action_scan_slot(
            "pause-potential-low-roi", "17:00"), "17:00")
        self.assertEqual(plan_power._post_action_scan_slot(
            "pause-potential-low-roi", "12:00"), "12:00")


if __name__ == "__main__":
    unittest.main()
