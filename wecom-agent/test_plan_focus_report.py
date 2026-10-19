# -*- coding: utf-8 -*-
"""plan_focus_report 潜力/重点 30 分钟独立播报的单测（纯函数部分）。"""
from __future__ import annotations

import datetime
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "投流")))

import plan_focus_report as pfr
import budget_watch as _bw
import logging as _logging

# 单测不污染生产日志（两个模块都有模块级 FileHandler）
for _lg in (pfr.log, _bw.log):
    _lg.handlers = [h for h in _lg.handlers
                    if not isinstance(h, _logging.FileHandler)]

# 状态/队列文件指向临时目录，防测试写真实文件（同 test_budget_watch）
import tempfile as _tempfile
_TEST_TMP = _tempfile.mkdtemp(prefix="pfr_test_")
_bw.STATE_FILE = os.path.join(_TEST_TMP, "state.json")
_bw.TOPUP_QUEUE_FILE = os.path.join(_TEST_TMP, "queue.json")

NOON = 14 * 60  # 潜力线 6.0 / 重点线 7.3


def _plan(pid, name, status="投放中", spent=100.0, orders=10, roi=8.0,
          budget=500.0):
    return SimpleNamespace(plan_id=pid, plan_name=name, status=status,
                           spent=spent, orders=orders, roi=roi, budget=budget)


class BuildRowsTests(unittest.TestCase):
    def test_filters_categories_and_whitelist(self) -> None:
        plans = {
            "业务2部": [
                _plan("1", "0708-恩豆妈咪-张"),           # 白名单（看板重点）
                _plan("2", "0830_芒果妈妈_张"),           # 看板潜力
                _plan("3", "0810_老计划_黄"),             # 看板老池 → 排除
                _plan("4", "无标签计划"),                 # 无标签 → 排除
            ],
            "业务3部": [_plan("5", "0901_新计划_赵", spent=0.0, orders=0)],
        }
        board = {"1": "key_plan", "2": "potential_plan", "3": "old_pool",
                 "5": "new_plan"}
        watched = {}
        rows = pfr.build_rows(plans, board, watched, NOON)
        self.assertEqual([r["plan_id"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["tag"], "重点计划")
        self.assertEqual(rows[1]["tag"], "潜力计划")

    def test_whitelist_included_without_board_label(self) -> None:
        plans = {"业务2部": [_plan("9", "0708-恩豆妈咪-张")]}
        rows = pfr.build_rows(plans, {}, {"9": {}}, NOON)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "白名单")

    def test_matrix_department_displayed_as_business1(self) -> None:
        """矩阵号（业务1部-1）播报行归入业务1部展示（2026-09-03 用户定）。"""
        plans = {"业务1部-1": [_plan("m1", "0904_牛仔骨_矩阵")]}
        rows = pfr.build_rows(plans, {"m1": "potential_plan"}, {}, NOON)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["department"], "业务1部")

    def test_suppressed_plan_stays_in_monitor_rows_without_budget_flag(self) -> None:
        orig = _bw.load_suppressed_alert_plan_ids
        _bw.load_suppressed_alert_plan_ids = lambda: {"2"}
        try:
            plans = {"业务2部": [_plan("2", "0830_芒果妈妈_张")]}
            rows = pfr.build_rows(plans, {"2": "potential_plan"}, {}, NOON)
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["budget_level"])
        finally:
            _bw.load_suppressed_alert_plan_ids = orig

    def test_paused_zero_spend_excluded_unless_watch(self) -> None:
        plans = {"业务3部": [
            _plan("6", "暂停零耗_黄", status="已暂停", spent=0.0),
            _plan("7", "暂停有耗_黄", status="已暂停", spent=88.0),
            _plan("8", "白名单暂停", status="已暂停", spent=0.0),
        ]}
        board = {"6": "potential_plan", "7": "potential_plan"}
        rows = pfr.build_rows(plans, board, {"8": {}}, NOON)
        self.assertEqual([r["plan_id"] for r in rows], ["8", "7"])  # 白名单排最前

    def test_flags_computed(self) -> None:
        plans = {"业务2部": [
            _plan("2", "芒果", spent=400.0, orders=40, roi=5.0, budget=450.0),
        ]}
        rows = pfr.build_rows(plans, {"2": "potential_plan"}, {}, NOON)
        self.assertEqual(rows[0]["budget_level"], "warn")       # 400/450=89%
        self.assertEqual(rows[0]["roi_level"], "roi_low")       # 5.0 < 6.0

    def test_sort_whitelist_key_potential(self) -> None:
        plans = {"业务2部": [
            _plan("p", "潜力", spent=900.0),
            _plan("k", "重点", spent=100.0),
            _plan("w", "白名单", spent=10.0),
        ]}
        board = {"p": "potential_plan", "k": "key_plan"}
        rows = pfr.build_rows(plans, board, {"w": {}}, NOON)
        self.assertEqual([r["plan_id"] for r in rows], ["w", "k", "p"])


class BuildMarkdownTests(unittest.TestCase):
    def test_layout_and_flags(self) -> None:
        rows = pfr.build_rows({"业务2部": [
            _plan("1", "0708-恩豆妈咪-张", spent=412.3, orders=35, roi=10.4,
                  budget=600.0),
            _plan("2", "0830_芒果妈妈_张", spent=705.2, orders=68, roi=5.8,
                  budget=99999.0),
        ]}, {"1": "key_plan", "2": "potential_plan"}, {}, NOON)
        md = pfr.build_markdown(rows, datetime.datetime(2026, 9, 1, 14, 25), NOON)
        self.assertIn("【潜力·重点监控】09月01日 14:25", md)
        self.assertIn("恩豆妈咪", md)
        self.assertIn("预算 69%", md)
        self.assertIn("预算不限", md)
        self.assertIn("⚠️ROI破线(6)", md)      # 5.8 < 潜力线 6.0
        self.assertIn("合计：2/2 条在投", md)
        self.assertLessEqual(len(md.encode("utf-8")), pfr.MAX_BYTES)

    def test_empty(self) -> None:
        md = pfr.build_markdown([], datetime.datetime(2026, 9, 1, 14, 25), NOON)
        self.assertIn("无在投或有消耗", md)

    def test_byte_cap_folds_tail(self) -> None:
        plans = {"业务2部": [
            _plan(str(100 + i), f"潜力计划名称很长很长_{i}_额外字符填充",
                  spent=float(1000 - i), budget=99999.0)
            for i in range(40)
        ]}
        board = {str(100 + i): "potential_plan" for i in range(40)}
        rows = pfr.build_rows(plans, board, {}, NOON)
        md = pfr.build_markdown(rows, datetime.datetime(2026, 9, 1, 14, 25), NOON)
        self.assertLessEqual(len(md.encode("utf-8")), pfr.MAX_BYTES + 200)  # 折叠行本身
        self.assertIn("其余", md)


class GuardTests(unittest.TestCase):
    def test_due(self) -> None:
        self.assertTrue(pfr._due({}))
        self.assertTrue(pfr._due({"last_push_ts": 0}))
        self.assertFalse(pfr._due({"last_push_ts": 9e18}))

    def test_night_window(self) -> None:
        self.assertTrue(pfr.in_night_window(datetime.datetime(2026, 9, 1, 23, 30)))
        self.assertTrue(pfr.in_night_window(datetime.datetime(2026, 9, 1, 3, 0)))
        self.assertFalse(pfr.in_night_window(datetime.datetime(2026, 9, 1, 12, 0)))
        self.assertFalse(pfr.in_night_window(datetime.datetime(2026, 9, 1, 8, 0)))


class SafetyNoticeTests(unittest.TestCase):
    """预算安全闸通告行（2026-09-15 费用安全件）。"""

    def test_empty_when_nothing_happened(self) -> None:
        self.assertEqual(pfr._safety_notice({"changed": [], "failed": []}), "")

    def test_intercept_and_over_cap_lines(self) -> None:
        notice = pfr._safety_notice({
            "changed": [
                {"plan_name": "A计划", "current_budget": 99999.0, "after": 300.0,
                 "label_budget": 300.0, "reason": "unlimited_intercept"},
                {"plan_name": "B计划", "current_budget": 500.0, "after": 300.0,
                 "label_budget": 300.0, "reason": "over_cap"},
            ],
            "failed": [{"plan_name": "C计划"}],
        })
        self.assertIn("99999拦截", notice)
        self.assertIn("A计划(99999→300)", notice)
        self.assertIn("漂移下压", notice)
        self.assertIn("B计划(500→300)", notice)
        self.assertIn("失败 1 条", notice)

    def test_notice_enters_markdown_and_counts_bytes(self) -> None:
        md = pfr.build_markdown([], datetime.datetime(2026, 9, 15, 14, 15), NOON,
                                notices=["> 🛡️ 预算安全闸已动作：99999拦截 X"])
        self.assertIn("🛡️", md)
        self.assertIn("无在投或有消耗", md)
        self.assertLessEqual(len(md.encode("utf-8")), pfr.MAX_BYTES)


class SafetyClampWiringTests(unittest.TestCase):
    """run() 内接线：扫描→检出→执行→通告进播报（浏览器侧全部打桩）。"""

    def test_clamp_runs_and_notice_pushed(self) -> None:
        import 巨量千川.plan_power as pp

        plans = {"业务1部": [
            _plan("9", "99999计划", budget=99999.0, spent=50.0, orders=2, roi=5.0),
        ]}
        board = {"9": "old_pool"}
        captured: dict = {}
        pushed: dict = {}

        async def fake_scan():
            return [], plans, board

        async def fake_clamp(targets, dry_run=False):
            captured["targets"] = targets
            return {"targets": targets,
                    "changed": [{**targets[0], "after": 300.0}], "failed": []}

        def fake_push(md):
            pushed["md"] = md
            return True

        with mock.patch.object(pfr, "_scan", fake_scan), \
             mock.patch.object(pfr, "_acquire_lock", return_value=object()), \
             mock.patch.object(pfr, "_release_lock"), \
             mock.patch.object(pfr, "_due", return_value=True), \
             mock.patch.object(pfr, "_load_state", return_value={}), \
             mock.patch.object(pfr, "_save_state"), \
             mock.patch.object(pfr, "_push", side_effect=fake_push), \
             mock.patch.object(pfr, "_fire_immediate_alerts"), \
             mock.patch.object(_bw, "_try_action_lock", return_value=object()), \
             mock.patch.object(_bw, "_busy_mark"), \
             mock.patch.object(_bw, "_busy_clear"), \
             mock.patch.object(_bw, "_release_action_lock"), \
             mock.patch.object(_bw, "in_report_window", return_value=False), \
             mock.patch.object(_bw, "report_busy", return_value=False), \
             mock.patch.object(pp, "load_state",
                               return_value={"new_plan_promotions": []}), \
             mock.patch.object(pp, "_ignored_resume_keys", return_value=set()), \
             mock.patch("巨量千川.manual_whitelist.match_manual_whitelist",
                        return_value=None), \
             mock.patch.object(pp, "clamp_budget_safety", side_effect=fake_clamp):
            result = pfr.run(push=True, force=True)

        self.assertEqual(result, "pushed")
        (t,) = captured["targets"]
        self.assertEqual(t["plan_id"], "9")
        self.assertEqual(t["reason"], "unlimited_intercept")
        self.assertEqual(t["label_budget"], 300.0)
        self.assertIn("🛡️ 预算安全闸", pushed["md"])
        self.assertIn("99999计划(99999→300)", pushed["md"])

    def test_no_targets_no_clamp_call(self) -> None:
        import 巨量千川.plan_power as pp

        plans = {"业务1部": [_plan("1", "正常", budget=300.0, spent=50.0)]}
        board = {"1": "old_pool"}

        async def fake_scan():
            return [], plans, board

        clamp = mock.Mock()
        with mock.patch.object(pfr, "_scan", fake_scan), \
             mock.patch.object(pfr, "_acquire_lock", return_value=object()), \
             mock.patch.object(pfr, "_release_lock"), \
             mock.patch.object(pfr, "_due", return_value=True), \
             mock.patch.object(pfr, "_load_state", return_value={}), \
             mock.patch.object(pfr, "_save_state"), \
             mock.patch.object(pfr, "_push", return_value=True), \
             mock.patch.object(pfr, "_fire_immediate_alerts"), \
             mock.patch.object(_bw, "_try_action_lock", return_value=object()), \
             mock.patch.object(_bw, "_busy_mark"), \
             mock.patch.object(_bw, "_busy_clear"), \
             mock.patch.object(_bw, "_release_action_lock"), \
             mock.patch.object(_bw, "in_report_window", return_value=False), \
             mock.patch.object(_bw, "report_busy", return_value=False), \
             mock.patch.object(pp, "load_state",
                               return_value={"new_plan_promotions": []}), \
             mock.patch.object(pp, "_ignored_resume_keys", return_value=set()), \
             mock.patch("巨量千川.manual_whitelist.match_manual_whitelist",
                        return_value=None), \
             mock.patch.object(pp, "clamp_budget_safety", clamp):
            result = pfr.run(push=True, force=True)

        self.assertEqual(result, "pushed")
        clamp.assert_not_called()

    def test_dry_run_detects_but_never_clamps(self) -> None:
        import 巨量千川.plan_power as pp

        plans = {"业务1部": [_plan("9", "99999计划", budget=99999.0)]}
        board = {"9": "old_pool"}

        async def fake_scan():
            return [], plans, board

        clamp = mock.Mock()
        with mock.patch.object(pfr, "_scan", fake_scan), \
             mock.patch.object(pfr, "_acquire_lock", return_value=object()), \
             mock.patch.object(pfr, "_release_lock"), \
             mock.patch.object(pfr, "_due", return_value=True), \
             mock.patch.object(pfr, "_load_state", return_value={}), \
             mock.patch.object(pfr, "_save_state"), \
             mock.patch.object(pfr, "_fire_immediate_alerts"), \
             mock.patch.object(_bw, "_try_action_lock", return_value=object()), \
             mock.patch.object(_bw, "_busy_mark"), \
             mock.patch.object(_bw, "_busy_clear"), \
             mock.patch.object(_bw, "_release_action_lock"), \
             mock.patch.object(_bw, "in_report_window", return_value=False), \
             mock.patch.object(_bw, "report_busy", return_value=False), \
             mock.patch.object(pp, "load_state",
                               return_value={"new_plan_promotions": []}), \
             mock.patch.object(pp, "_ignored_resume_keys", return_value=set()), \
             mock.patch("巨量千川.manual_whitelist.match_manual_whitelist",
                        return_value=None), \
             mock.patch.object(pp, "clamp_budget_safety", clamp):
            result = pfr.run(push=False, force=True)

        self.assertEqual(result, "dry-run")
        clamp.assert_not_called()

    def test_whitelisted_plan_never_clamped(self) -> None:
        """人工白名单（主管人工管理）安全闸绝对不碰（09-16 用户纠正：
        味蕾食光/恩豆/陈树莉被 22:47 轮误压）。"""
        import 巨量千川.plan_power as pp

        plans = {"业务1部": [
            _plan("wl", "0907_味蕾食光-郑", budget=900.0, spent=100.0),
        ]}
        board = {"wl": "potential_plan"}  # 盘中标签翻成潜力也不影响白名单跳过

        async def fake_scan():
            return [], plans, board

        clamp = mock.Mock()
        with mock.patch.object(pfr, "_scan", fake_scan), \
             mock.patch.object(pfr, "_acquire_lock", return_value=object()), \
             mock.patch.object(pfr, "_release_lock"), \
             mock.patch.object(pfr, "_due", return_value=True), \
             mock.patch.object(pfr, "_load_state", return_value={}), \
             mock.patch.object(pfr, "_save_state"), \
             mock.patch.object(pfr, "_push", return_value=True), \
             mock.patch.object(pfr, "_fire_immediate_alerts"), \
             mock.patch.object(_bw, "_try_action_lock", return_value=object()), \
             mock.patch.object(_bw, "_busy_mark"), \
             mock.patch.object(_bw, "_busy_clear"), \
             mock.patch.object(_bw, "_release_action_lock"), \
             mock.patch.object(_bw, "in_report_window", return_value=False), \
             mock.patch.object(_bw, "report_busy", return_value=False), \
             mock.patch.object(pp, "load_state",
                               return_value={"new_plan_promotions": []}), \
             mock.patch.object(pp, "_ignored_resume_keys", return_value=set()), \
             mock.patch("巨量千川.manual_whitelist.match_manual_whitelist",
                        return_value={"name": "味蕾食光"}), \
             mock.patch.object(pp, "clamp_budget_safety", clamp):
            result = pfr.run(push=True, force=True)

        self.assertEqual(result, "pushed")
        clamp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
