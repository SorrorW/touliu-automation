from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from 巨量千川.daily_report import (
    _is_reportable,
    _merge_started_today_history,
    aggregate,
    _plan_category,
    CURRENT_ONLY_REPORT_SLOTS,
    FULL_REPORT_SLOTS,
    _report_history_slot,
)


class DailyReportCategoryTests(unittest.TestCase):
    def test_today_7_3_stage_remains_new_plan_until_close(self) -> None:
        name = f"{datetime.date.today():%m%d}_达人A_黄"
        self.assertEqual(_plan_category({
            "plan_name": name, "roi_target": 7.3, "roi": 5.5, "orders": 7,
        }), "new_plan")

    def test_previous_day_7_3_plan_uses_final_lifecycle_category(self) -> None:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        name = f"{yesterday:%m%d}_达人A_黄"
        self.assertEqual(_plan_category({
            "plan_name": name, "roi_target": 7.3, "roi": 5.5, "orders": 7,
        }), "old_pool")

    def test_today_old_pool_target_is_not_mistaken_for_new_plan(self) -> None:
        name = f"{datetime.date.today():%m%d}_达人A_黄"
        self.assertEqual(_plan_category({
            "plan_name": name, "roi_target": 10.3, "roi": 5.5, "orders": 7,
        }), "old_pool")

    def test_cross_day_4_5_test_stays_new_plan(self) -> None:
        self.assertEqual(_plan_category({
            "plan_name": "0810_达人A_黄", "roi_target": 4.5, "roi": None, "orders": 0,
        }), "new_plan")


    def test_report_schedule_uses_current_only_then_full_slots(self) -> None:
        self.assertEqual(
            CURRENT_ONLY_REPORT_SLOTS,
            {"08:00", "10:00", "12:00", "14:00", "16:00"},
        )
        self.assertEqual(FULL_REPORT_SLOTS, {"17:00", "22:30"})
        self.assertEqual(_report_history_slot("（截至 10:00）", None), "10:00")
        self.assertEqual(_report_history_slot("（截至 16:00）", None), "16:00")

    def test_night_aggregate_includes_plan_paused_after_running(self) -> None:
        report = {"业务3部": {"plans": [{
            "plan_id": "p1", "plan_name": f"{datetime.date.today():%m%d}_达人A_黄", "status": "已暂停",
            "started_today": True, "spent": 213.84, "orders": 7, "roi": 5.5,
            "roi_target": 7.3, "budget": 400,
        }]}}
        self.assertTrue(_is_reportable(report["业务3部"]["plans"][0], include_started_today=True))
        deps, totals = aggregate(report, include_started_today=True)
        self.assertEqual(deps["业务3部"]["plans"], 1)
        self.assertEqual(totals["plans"], 1)
        self.assertEqual(deps["业务3部"]["categories"]["new_plan"][0]["plan_id"], "p1")

    def test_daytime_aggregate_includes_scanned_paused_plan(self) -> None:
        report = {"业务3部": {"plans": [{
            "plan_id": "p1", "plan_name": f"{datetime.date.today():%m%d}_达人A_黄", "status": "已暂停",
            "started_today": True, "spent": 213.84, "orders": 7, "roi": 5.5,
            "roi_target": 7.3, "budget": 400,
        }]}}
        report["业务3部"]["plans"][0]["scanned_today"] = True
        deps, totals = aggregate(report, include_started_today=True)
        self.assertEqual(deps["业务3部"]["plans"], 1)
        self.assertEqual(totals["plans"], 1)

    def test_no_spend_plans_are_hidden_and_17_uses_active_denominator(self) -> None:
        report = {"业务1部": {"total_plans": 4, "plans": [
            {"plan_id": "active-spent", "plan_name": "0827_有消耗_林", "status": "投放中",
             "spent": 20, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "active-zero", "plan_name": "0827_无消耗_林", "status": "投放中",
             "spent": 0, "orders": 0, "roi": None, "roi_target": 4.5},
            {"plan_id": "paused-spent", "plan_name": "0827_已暂停_林", "status": "已暂停",
             "spent": 10, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "paused-zero", "plan_name": "0827_暂停无消耗_林", "status": "已暂停",
             "spent": 0, "orders": 0, "roi": None, "roi_target": 4.5},
        ]}}
        deps, totals = aggregate(report, include_started_today=False, scan_slot="17:00")
        # 2026-09-03 用户定：有出单的被暂停计划任何档位都播（含手动暂停），
        # 故 paused-spent（1 单）计入；无消耗的两条始终隐藏。
        self.assertEqual(deps["业务1部"]["spent_plans"], 2)
        self.assertEqual(deps["业务1部"]["plans"], 2)
        self.assertEqual(deps["业务1部"]["plan_denominator"], 2)
        self.assertEqual(totals["spent_plans"], 2)

    def test_2230_uses_spend_numerator_and_fresh_scan_denominator(self) -> None:
        # 2026-09-01 用户定：22:30 分母 = 当次扫描数（fresh_scan_total，由合并函数
        # 在追加历史行前记录）；分子仍是当天全量有消耗计划。
        report = {"业务1部": {"fresh_scan_total": 2, "plans": [
            {"plan_id": "running", "plan_name": "0827_在投_林", "status": "投放中",
             "spent": 20, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "paused", "plan_name": "0827_已暂停_林", "status": "已暂停",
             "started_today": True, "spent": 10, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "history-only", "plan_name": "0827_历史_林", "status": "已暂停（今日曾投放）",
             "started_today": True, "spent": 5, "orders": 1, "roi": 5, "roi_target": 4.5},
        ]}}
        deps, totals = aggregate(report, include_started_today=True, scan_slot="22:30")
        self.assertEqual(deps["业务1部"]["spent_plans"], 3)
        self.assertEqual(deps["业务1部"]["plans"], 3)
        self.assertEqual(deps["业务1部"]["plan_denominator"], 2)
        self.assertEqual(totals["plan_denominator"], 2)

    def test_2230_denominator_falls_back_to_scanned_total_without_fresh_count(self) -> None:
        # 未经合并函数的报告（重建/单测直调）没有 fresh_scan_total，回退当天全量。
        report = {"业务1部": {"plans": [
            {"plan_id": "running", "plan_name": "0827_在投_林", "status": "投放中",
             "spent": 20, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "paused", "plan_name": "0827_已暂停_林", "status": "已暂停",
             "started_today": True, "spent": 10, "orders": 1, "roi": 5, "roi_target": 4.5},
            {"plan_id": "zero", "plan_name": "0827_无消耗_林", "status": "投放中",
             "spent": 0, "orders": 0, "roi": None, "roi_target": 4.5},
        ]}}
        deps, totals = aggregate(report, include_started_today=True, scan_slot="22:30")
        self.assertEqual(deps["业务1部"]["spent_plans"], 2)
        self.assertEqual(deps["业务1部"]["plan_denominator"], 3)

    def test_history_marks_live_paused_row_and_adds_missing_started_plan(self) -> None:
        live = {"业务3部": {"plans": [{
            "plan_id": "p1", "plan_name": "0825_天妈轻生活_黄", "status": "已暂停",
            "spent": 213.84, "orders": 7, "roi": 5.5, "roi_target": 7.3,
            "budget": 400,
        }]}}
        history = [
            {"plan_id": "p1", "department": "业务3部", "plan_name": "0825_天妈轻生活_黄",
             "plan_status": "投放中", "scan_slot": "17:00", "spent": 213.84,
             "orders": 7, "roi": 5.5, "roi_target": 7.3, "daily_budget": 400},
            {"plan_id": "p2", "department": "业务2部", "plan_name": "0825_另一条_林",
             "plan_status": "投放中", "scan_slot": "12:00", "spent": 90,
             "orders": 2, "roi": 4.8, "roi_target": 4.5, "daily_budget": 300},
        ]
        with patch("webui.db.list_plan_daily_labels", return_value=history) as list_labels:
            merged, added = _merge_started_today_history(live, label_date="2026-08-25")
        list_labels.assert_called_once_with("2026-08-25", active_only=False)
        self.assertEqual(added, 1)
        self.assertTrue(merged["业务3部"]["plans"][0]["started_today"])
        self.assertEqual(merged["业务2部"]["plans"][0]["plan_id"], "p2")
        self.assertTrue(merged["业务2部"]["plans"][0]["started_today"])


if __name__ == "__main__":
    unittest.main()
