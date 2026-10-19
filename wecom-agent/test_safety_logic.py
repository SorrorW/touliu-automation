#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化安全回归测试：不启动浏览器、不访问企微、不修改生产状态文件。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "投流"))

import local_tools
from 巨量千川 import daily_high_performer_monitor as high_performer
from 巨量千川 import plan_power


class _Plan:
    plan_name = "test-plan"
    plan_id = "plan-1"
    roi = 12.0
    orders = 6
    spent = 1.0


class SafetyLogicTests(unittest.TestCase):
    def test_plan_power_state_resets_on_new_day(self):
        original = plan_power.STATE_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            plan_power.STATE_FILE = Path(temp_dir) / "state.json"
            plan_power.STATE_FILE.write_text(
                '{"date":"2000-01-01","midnight_paused":[{"plan_id":"old"}],'
                '"night_created":[{"plan_id":"old"}],"enabled":["old"]}',
                encoding="utf-8",
            )
            state = plan_power.load_state()
        plan_power.STATE_FILE = original

        self.assertEqual(state["date"], plan_power._today())
        self.assertEqual(state["midnight_paused"], [])
        self.assertEqual(state["night_created"], [])
        self.assertEqual(state["enabled"], [])

    def test_high_performer_counter_resets_on_new_local_day(self):
        async def metrics_reader(**_kwargs):
            return [_Plan()]

        async def chase_creator(**_kwargs):
            return {"task_id": "task-1"}

        state = high_performer.HighPerformerState(
            last_chase_date="2000-01-01", chase_count_today=7
        )
        asyncio.run(high_performer.scan_and_chase(
            "creator", state, department="业务1部",
            metrics_reader=metrics_reader, chase_creator=chase_creator,
        ))
        self.assertEqual(state.last_chase_date, high_performer._today_str())
        self.assertEqual(state.chase_count_today, 1)

    def test_automatic_growth_guard_never_runs_a_subprocess_when_disabled(self):
        original = local_tools.automated_growth_enabled
        local_tools.automated_growth_enabled = lambda: False
        try:
            chase_ok, chase = local_tools.run_qualified_chase()
            budget_ok, budget = local_tools.run_budget_raise()
        finally:
            local_tools.automated_growth_enabled = original

        self.assertTrue(chase_ok)
        self.assertTrue(chase["skipped"])
        self.assertTrue(budget_ok)
        self.assertTrue(budget["skipped"])

    def test_common_surname_xiao_is_read_from_seller_column(self):
        # 备注可以只有「8/26投」，姓名来自独立的业务员字段。
        self.assertEqual(local_tools.extract_person_name("肖靖"), "肖靖")
        self.assertEqual(local_tools.resolve_seller("肖靖", "8/26投"), ("肖靖", ""))
        self.assertTrue(local_tools.resolve_seller("", "8/26投")[1].startswith("业务员/备注只写了日期"))

    def test_lifecycle_gate_spans_the_same_night(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            gate_file = Path(temp_dir) / "nightly_lifecycle_gate.json"
            gate_file.write_text(json.dumps({
                "night_start_date": "2026-08-20", "classification_ready": True,
            }), encoding="utf-8")
            self.assertTrue(local_tools.lifecycle_gate_ready(
                __import__("datetime").datetime(2026, 8, 20, 23, 30), str(gate_file)
            ))
            self.assertTrue(local_tools.lifecycle_gate_ready(
                __import__("datetime").datetime(2026, 8, 21, 3, 30), str(gate_file)
            ))
            self.assertFalse(local_tools.lifecycle_gate_ready(
                __import__("datetime").datetime(2026, 8, 21, 8, 0), str(gate_file)
            ))


if __name__ == "__main__":
    unittest.main()
