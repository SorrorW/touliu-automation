# -*- coding: utf-8 -*-
"""budget_watch 预算监控（2026-09-01 恩豆妈咪全天跑）单测。"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

# 便携 runtime 的 ._pth 完全接管 sys.path，脚本目录不在其中，须手动加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "投流")))

import budget_watch

# 单测不污染生产日志（模块级 FileHandler 会写 logs/budget_watch_YYYYMMDD.log）
import logging as _logging
budget_watch.log.handlers = [
    h for h in budget_watch.log.handlers
    if not isinstance(h, _logging.FileHandler)]

# 状态/队列文件全部指向临时目录：任何测试类忘了打桩也碰不到真实文件
# （2026-09-01 教训：ReportHookTests 用恩豆真实 pid + 未打桩队列写入，
#  把 synthetic warn 写进真实 budget_topup_queue.json，13:44 被 poll 真执行了）
import tempfile as _tempfile
_TEST_TMP = _tempfile.mkdtemp(prefix="bw_test_")
budget_watch.STATE_FILE = os.path.join(_TEST_TMP, "state.json")
budget_watch.TOPUP_QUEUE_FILE = os.path.join(_TEST_TMP, "queue.json")


class EvaluateTests(unittest.TestCase):
    def test_normal_below_threshold(self) -> None:
        self.assertIsNone(budget_watch.evaluate(200.0, 300.0, "投放中"))

    def test_warn_at_80_percent(self) -> None:
        self.assertEqual(budget_watch.evaluate(240.0, 300.0, "投放中"), "warn")
        self.assertEqual(budget_watch.evaluate(299.9, 300.0, "投放中"), "warn")

    def test_exhausted_at_cap(self) -> None:
        self.assertEqual(budget_watch.evaluate(300.0, 300.0, "投放中"), "exhausted")
        self.assertEqual(budget_watch.evaluate(320.5, 300.0, "投放中"), "exhausted")

    def test_exhausted_by_status(self) -> None:
        """预算不足被平台暂停：哪怕消耗读数略低于预算也判断供。"""
        self.assertEqual(
            budget_watch.evaluate(298.0, 300.0, "已暂停预算不足"), "exhausted")

    def test_unlimited_or_missing_budget_skipped(self) -> None:
        self.assertIsNone(budget_watch.evaluate(500.0, 99999.0, "投放中"))
        self.assertIsNone(budget_watch.evaluate(500.0, None, "投放中"))
        self.assertIsNone(budget_watch.evaluate(500.0, 0, "投放中"))

    def test_bad_budget_text_skipped(self) -> None:
        self.assertIsNone(budget_watch.evaluate(100.0, "不限", "投放中"))


class ReportHookTests(unittest.TestCase):
    def _report(self, spent: float, budget: float = 300.0, status: str = "投放中") -> dict:
        return {"departments": {"业务2部": {"categories": {"key_plan": [{
            "plan_id": "test-budget-plan", "plan_name": "0708-恩豆妈咪-张",
            "status": status, "spent": spent, "orders": 11, "roi": 10.2,
            "budget": budget,
        }]}}}}

    def test_check_from_report_alerts_and_dedupes(self) -> None:
        entries = [{"name": "恩豆妈咪", "department": "业务2部",
                    "plan_id": "test-budget-plan"}]
        orig_entries = budget_watch.load_watch_entries
        orig_state = budget_watch._load_state
        orig_save = budget_watch._save_state
        orig_push = budget_watch._push_alert
        state_box: dict = {}
        budget_watch.load_watch_entries = lambda: entries
        budget_watch._load_state = lambda: dict(state_box)
        budget_watch._save_state = lambda st: state_box.update(st)
        budget_watch._push_alert = lambda md: True  # 推送成功才记状态
        try:
            # 80% → warn；同级别同一天不重复推
            hits = budget_watch.check_from_report(self._report(250.0))
            self.assertEqual(hits, ["warn:test-budget-plan"])
            hits = budget_watch.check_from_report(self._report(260.0))
            self.assertEqual(hits, [])
            # 到顶 → exhausted 升一级再推一次
            hits = budget_watch.check_from_report(self._report(310.0))
            self.assertEqual(hits, ["exhausted:test-budget-plan"])
            # 低水位不误报、无关计划不命中
            hits = budget_watch.check_from_report(self._report(10.0, budget=99999.0))
            self.assertEqual(hits, [])
        finally:
            budget_watch.load_watch_entries = orig_entries
            budget_watch._load_state = orig_state
            budget_watch._save_state = orig_save
            budget_watch._push_alert = orig_push

    def test_dry_run_does_not_mark_state(self) -> None:
        """dry-run 只报告不记去重状态，否则真实告警会被吞掉。"""
        entries = [{"name": "恩豆妈咪", "department": "业务2部",
                    "plan_id": "test-budget-plan"}]
        orig_entries = budget_watch.load_watch_entries
        orig_state = budget_watch._load_state
        orig_save = budget_watch._save_state
        orig_push = budget_watch._push_alert
        state_box: dict = {}
        budget_watch.load_watch_entries = lambda: entries
        budget_watch._load_state = lambda: dict(state_box)
        budget_watch._save_state = lambda st: state_box.update(st)
        pushed: list[str] = []
        budget_watch._push_alert = lambda md: pushed.append(md) or True
        try:
            hits = budget_watch.check_from_report(self._report(250.0), push=False)
            self.assertEqual(hits, ["warn:test-budget-plan"])  # 仍会报告命中
            self.assertEqual(pushed, [])                       # 但没推送
            self.assertEqual(state_box, {})                    # 也没记状态
            # 紧接着真实运行：同一级别仍能推出去
            hits = budget_watch.check_from_report(self._report(255.0))
            self.assertEqual(hits, ["warn:test-budget-plan"])
            self.assertEqual(len(pushed), 1)
        finally:
            budget_watch.load_watch_entries = orig_entries
            budget_watch._load_state = orig_state
            budget_watch._save_state = orig_save
            budget_watch._push_alert = orig_push

    def test_check_from_report_empty_watchlist(self) -> None:
        orig = budget_watch.load_watch_entries
        budget_watch.load_watch_entries = lambda: []
        try:
            self.assertEqual(budget_watch.check_from_report({}, push=False), [])
        finally:
            budget_watch.load_watch_entries = orig

    def test_excluded_plan_is_silent(self) -> None:
        orig_excluded = budget_watch.load_suppressed_alert_plan_ids
        budget_watch.load_suppressed_alert_plan_ids = lambda: {"test-budget-plan"}
        try:
            self.assertEqual(self._report(250.0)["departments"]["业务2部"]["categories"]["key_plan"][0]["plan_id"], "test-budget-plan")
            self.assertEqual(budget_watch.check_from_report(self._report(250.0), push=False), [])
        finally:
            budget_watch.load_suppressed_alert_plan_ids = orig_excluded


class WindowGuardTests(unittest.TestCase):
    def test_report_window(self) -> None:
        # 守卫窗 ±12 分钟（2026-09-01 收窄，放行每小时 :15/:45 监控）
        self.assertTrue(budget_watch.in_report_window(datetime.datetime(2026, 9, 1, 11, 50)))
        self.assertTrue(budget_watch.in_report_window(datetime.datetime(2026, 9, 1, 12, 11)))
        self.assertFalse(budget_watch.in_report_window(datetime.datetime(2026, 9, 1, 12, 15)))
        self.assertFalse(budget_watch.in_report_window(datetime.datetime(2026, 9, 1, 12, 45)))
        self.assertFalse(budget_watch.in_report_window(datetime.datetime(2026, 9, 1, 11, 47)))


class RoiFloorTests(unittest.TestCase):
    def test_potential_floors(self) -> None:
        self.assertEqual(budget_watch.roi_floor_for("potential_plan", 10 * 60), 6.0)
        self.assertEqual(budget_watch.roi_floor_for("potential_plan", 17 * 60), 7.3)
        self.assertEqual(budget_watch.roi_floor_for("potential_plan", 22 * 60 + 30), 7.3)

    def test_key_floors(self) -> None:
        self.assertEqual(budget_watch.roi_floor_for("key_plan", 8 * 60), 6.0)
        self.assertEqual(budget_watch.roi_floor_for("key_plan", 12 * 60), 7.3)
        self.assertEqual(budget_watch.roi_floor_for("key_plan", 17 * 60), 8.5)
        self.assertEqual(budget_watch.roi_floor_for("key_plan", 23 * 60), 10.0)

    def test_unknown_category(self) -> None:
        self.assertIsNone(budget_watch.roi_floor_for("new_plan", 12 * 60))
        self.assertIsNone(budget_watch.roi_floor_for("old_pool", 12 * 60))


class EvaluateRoiTests(unittest.TestCase):
    NOON = 12 * 60  # 潜力线 6.0 / 重点线 7.3

    def test_below_min_spend_skipped(self) -> None:
        self.assertIsNone(budget_watch.evaluate_roi("key_plan", 1.0, 0, 49.9, self.NOON))
        self.assertIsNone(budget_watch.evaluate_roi("potential_plan", 5.0, 3, 29.9, self.NOON))

    def test_roi_below_floor(self) -> None:
        self.assertEqual(
            budget_watch.evaluate_roi("key_plan", 7.0, 20, 200.0, self.NOON), "roi_low")
        self.assertIsNone(
            budget_watch.evaluate_roi("key_plan", 7.3, 20, 200.0, self.NOON))
        self.assertEqual(
            budget_watch.evaluate_roi("potential_plan", 5.9, 10, 60.0, self.NOON), "roi_low")

    def test_no_orders_high_spend(self) -> None:
        self.assertEqual(
            budget_watch.evaluate_roi("key_plan", None, 0, 100.0, self.NOON), "no_orders")
        self.assertIsNone(  # 消耗没到 2 倍门槛的零单不吵
            budget_watch.evaluate_roi("key_plan", None, 0, 80.0, self.NOON))

    def test_none_roi_with_orders_ignored(self) -> None:
        self.assertIsNone(budget_watch.evaluate_roi("key_plan", None, 5, 200.0, self.NOON))

    def test_unwatched_categories(self) -> None:
        self.assertIsNone(budget_watch.evaluate_roi("new_plan", 1.0, 0, 500.0, self.NOON))
        self.assertIsNone(budget_watch.evaluate_roi("old_pool", 1.0, 0, 500.0, self.NOON))


class CategoryReportTests(unittest.TestCase):
    """check_from_report 的分类监控：潜力/重点的预算+ROI，只报投放中。"""

    def _data(self, plans: list[dict], slot: str = "14:00") -> dict:
        cats: dict[str, list] = {}
        for p in plans:
            cats.setdefault(p.pop("_cat"), []).append(p)
        return {"scan_slot": slot, "departments": {"业务2部": {"categories": cats}}}

    def setUp(self) -> None:
        self._orig = (budget_watch.load_watch_entries,
                      budget_watch._load_state, budget_watch._save_state,
                      budget_watch._push_alert)
        self.state_box: dict = {}
        budget_watch.load_watch_entries = lambda: []
        budget_watch._load_state = lambda: dict(self.state_box)
        budget_watch._save_state = lambda st: self.state_box.update(st)
        budget_watch._push_alert = lambda md: True  # 推送成功才记状态

    def tearDown(self) -> None:
        (budget_watch.load_watch_entries, budget_watch._load_state,
         budget_watch._save_state, budget_watch._push_alert) = self._orig

    def test_key_plan_roi_low_fires_once(self) -> None:
        plan = {"_cat": "key_plan", "plan_id": "111", "plan_name": "0830_X_张",
                "status": "投放中", "spent": 368.5, "orders": 40, "roi": 5.81,
                "budget": 99999.0}
        hits = budget_watch.check_from_report(self._data([dict(plan)]))
        self.assertEqual(hits, ["roi_low:111"])  # 14:00 重点线 7.3，ROI 5.81 破线
        self.assertEqual(budget_watch.check_from_report(
            self._data([dict(plan)])), [])  # 当天不重复

    def test_paused_plan_not_reported(self) -> None:
        plan = {"_cat": "key_plan", "plan_id": "222", "plan_name": "X",
                "status": "已暂停", "spent": 300.0, "orders": 0, "roi": None,
                "budget": 300.0}
        # 已暂停：分类 ROI/预算均不报（白名单预算监控才不限状态，这里非白名单）
        self.assertEqual(budget_watch.check_from_report(
            self._data([dict(plan)]), push=False), [])

    def test_potential_budget_and_roi_together(self) -> None:
        plan = {"_cat": "potential_plan", "plan_id": "333", "plan_name": "Y",
                "status": "投放中", "spent": 250.0, "orders": 20, "roi": 5.0,
                "budget": 300.0}
        hits = budget_watch.check_from_report(self._data([dict(plan)]), push=False)
        self.assertEqual(sorted(hits), ["roi_low:333", "warn:333"])

    def test_new_plan_ignored(self) -> None:
        plan = {"_cat": "new_plan", "plan_id": "444", "plan_name": "Z",
                "status": "投放中", "spent": 500.0, "orders": 0, "roi": None,
                "budget": 300.0}
        self.assertEqual(budget_watch.check_from_report(
            self._data([dict(plan)]), push=False), [])


class TopupQueueTests(unittest.TestCase):
    """告警触发自动加预算的队列与执行器逻辑（浏览器调用全部打桩）。"""

    def setUp(self) -> None:
        self._orig = (budget_watch.load_watch_entries,
                      budget_watch._load_state, budget_watch._save_state,
                      budget_watch._push_alert,
                      budget_watch._load_topup_queue,
                      budget_watch._save_topup_queue,
                      budget_watch.in_report_window,
                      budget_watch.report_busy,
                      budget_watch._try_action_lock,
                      budget_watch._release_action_lock,
                      budget_watch._busy_mark, budget_watch._busy_clear,
                      budget_watch._topup_session)
        self.state_box: dict = {}
        self.queue_box: list[dict] = []
        self.pushed: list[str] = []
        budget_watch.load_watch_entries = lambda: []
        budget_watch._load_state = lambda: dict(self.state_box)
        budget_watch._save_state = lambda st: self.state_box.update(
            json.loads(json.dumps(st)))
        budget_watch._push_alert = lambda md: self.pushed.append(md) or True
        budget_watch._load_topup_queue = lambda: json.loads(
            json.dumps(self.queue_box))
        budget_watch._save_topup_queue = lambda q: (
            self.queue_box.clear(), self.queue_box.extend(
                json.loads(json.dumps(q))))
        budget_watch.in_report_window = lambda *a, **kw: False
        budget_watch.report_busy = lambda: False  # 播报忙标读真实文件，不打桩会随整点档抖动
        budget_watch._try_action_lock = lambda: object()  # 拿到锁
        budget_watch._release_action_lock = lambda fd: None
        budget_watch._busy_mark = lambda action: None
        budget_watch._busy_clear = lambda: None
        # 会话层打桩：不连真浏览器，直接跑逐条逻辑（_topup_one 再单独打桩）
        async def fake_session(pending, *, push):
            return await budget_watch._run_topup_items(None, pending, push=push)
        budget_watch._topup_session = fake_session

    def tearDown(self) -> None:
        (budget_watch.load_watch_entries, budget_watch._load_state,
         budget_watch._save_state, budget_watch._push_alert,
         budget_watch._load_topup_queue, budget_watch._save_topup_queue,
         budget_watch.in_report_window, budget_watch.report_busy,
         budget_watch._try_action_lock,
         budget_watch._release_action_lock, budget_watch._busy_mark,
         budget_watch._busy_clear, budget_watch._topup_session) = self._orig

    def _cat_report(self, spent: float, budget: float = 300.0) -> dict:
        return {"scan_slot": "14:00",
                "departments": {"业务3部": {"categories": {"potential_plan": [{
                    "plan_id": "777", "plan_name": "0901_X_赵",
                    "status": "投放中", "spent": spent, "orders": 20, "roi": 8.0,
                    "budget": budget,
                }]}}}}

    def test_budget_alert_does_not_enqueue(self) -> None:
        """2026-09-03 用户定：监控只监控，预算类告警只推送不再自动入加预算队列。"""
        hits = budget_watch.check_from_report(self._cat_report(250.0))
        self.assertEqual(hits, ["warn:777"])
        self.assertEqual(self.queue_box, [])

    def test_dynamic_topup_amount_examples(self) -> None:
        self.assertEqual(budget_watch.calculate_topup_amount(500, 380), 200.0)
        self.assertEqual(budget_watch.calculate_topup_amount(500, 250), 100.0)

    def test_remaining_at_or_above_target_does_not_topup(self) -> None:
        self.assertEqual(budget_watch.calculate_topup_amount(500, 200), 0.0)
        self.assertEqual(budget_watch.calculate_topup_amount(500, 100), 0.0)

    def test_non_round_deficit_rounds_up(self) -> None:
        # 剩余 199.9，缺口 100.1，按 100 元向上取整为 200。
        self.assertEqual(budget_watch.calculate_topup_amount(500, 300.1), 200.0)

    def test_execution_time_spend_changes_amount(self) -> None:
        # 告警时旧消耗可能只剩 250，但执行时已继续消耗到 380，需按执行时数据 +200。
        self.assertEqual(budget_watch.calculate_topup_amount(500, 250), 100.0)
        self.assertEqual(budget_watch.calculate_topup_amount(500, 380), 200.0)

    def test_queue_does_not_use_previous_topup_count(self) -> None:
        """告警入队不再按当天第几次告警预先决定固定金额。"""
        self.state_box.update({"date": datetime.date.today().isoformat(),
                               "alerts": {}, "topups": {"777": 1}})
        self.assertTrue(budget_watch._enqueue_topup(
            {"plan_id": "777", "name": "X", "department": "业务3部"}, "exhausted"))
        self.assertNotIn("amount", self.queue_box[0])

    def test_dry_run_does_not_enqueue(self) -> None:
        hits = budget_watch.check_from_report(self._cat_report(250.0), push=False)
        self.assertEqual(hits, ["warn:777"])
        self.assertEqual(self.queue_box, [])

    def test_pending_dedupe(self) -> None:
        self.assertTrue(budget_watch._enqueue_topup(
            {"plan_id": "777", "name": "X", "department": "业务3部"}, "warn"))
        self.assertFalse(budget_watch._enqueue_topup(
            {"plan_id": "777", "name": "X", "department": "业务3部"}, "exhausted"))
        self.assertEqual(len(self.queue_box), 1)

    def test_roi_alert_does_not_enqueue(self) -> None:
        data = {"scan_slot": "14:00",
                "departments": {"业务2部": {"categories": {"key_plan": [{
                    "plan_id": "888", "plan_name": "Y", "status": "投放中",
                    "spent": 368.5, "orders": 40, "roi": 5.0,
                    "budget": 99999.0}]}}}}
        hits = budget_watch.check_from_report(data)
        self.assertEqual(hits, ["roi_low:888"])
        self.assertEqual(self.queue_box, [])

    def test_executor_reads_today_spend_and_ignores_legacy_amount(self) -> None:
        import 巨量千川.budget_raise as budget_raise
        import 巨量千川.old_creator_retarget as retarget
        import 巨量千川.plan_metrics as plan_metrics
        import 巨量千川.plan_sweep as plan_sweep

        seen = {}
        saved = {}

        async def fake_search(page, keyword, department, by_creator=False, time_range=None):
            seen["time_range"] = time_range
            return ["row"]

        async def fake_parse(row):
            # 执行时读到今日已消耗 380，队列里的旧 amount=300 必须被忽略。
            return SimpleNamespace(plan_id="777", spent=380.0)

        async def fake_open(page, row):
            seen["opened"] = True

        async def fake_wait(page, seconds):
            pass

        async def fake_read(page):
            return 500.0

        async def fake_set(page, value):
            saved["budget"] = value

        async def fake_save(page):
            return True

        with mock.patch.object(plan_metrics, "_search_plans", fake_search), \
             mock.patch.object(plan_metrics, "_parse_plan_row", fake_parse), \
             mock.patch.object(retarget, "_open_edit_page", fake_open), \
             mock.patch.object(retarget, "_wait_stable", fake_wait), \
             mock.patch.object(budget_raise, "_read_budget", fake_read), \
             mock.patch.object(budget_raise, "_set_budget", fake_set), \
             mock.patch.object(plan_sweep, "_save_edit_page", fake_save):
            result = asyncio.run(budget_watch._topup_one(
                object(), {"plan_id": "777", "department": "业务3部", "amount": 300.0}))

        self.assertEqual(seen["time_range"], "today")
        self.assertEqual(saved["budget"], 700)
        self.assertEqual(result["amount"], 200.0)
        self.assertEqual(result["remaining_after"], 320.0)

    def test_execution_result_uses_actual_amount_and_remaining(self) -> None:
        result = {"before": 500.0, "after": 700.0, "spent": 380.0,
                  "amount": 200.0, "remaining_after": 320.0}
        md = budget_watch._topup_md(
            {"department": "业务3部", "plan_name": "X", "level": "warn"},
            result, 1)
        self.assertIn("+200", md)
        self.assertIn("320.00", md)
        self.assertNotIn("+300", md)

    def test_process_queue_success(self) -> None:
        self.queue_box.append({
            "plan_id": "777", "plan_name": "0901_X_赵", "department": "业务3部",
            "level": "warn",
            "enqueued_at": "2026-09-01T12:00:00", "status": "pending",
            "attempts": 0})

        async def fake_topup(page, item):
            return {"before": 500.0, "after": 700.0, "spent": 380.0,
                    "amount": 200.0, "remaining_after": 320.0}

        orig = budget_watch._topup_one
        budget_watch._topup_one = fake_topup
        try:
            results = budget_watch.process_topup_queue()
        finally:
            budget_watch._topup_one = orig
        self.assertEqual(results, ["done:777"])
        self.assertEqual(self.queue_box[0]["status"], "done")
        # 状态里记了今日加预算次数，且推了一条成功通知
        self.assertEqual(self.state_box.get("topups", {}).get("777"), 1)
        self.assertTrue(any("自动加预算" in md and "700" in md and
                            "+200" in md and "320.00" in md
                            for md in self.pushed))

    def test_process_queue_failure_retries_then_dead(self) -> None:
        self.queue_box.append({
            "plan_id": "777", "plan_name": "X", "department": "业务3部",
            "level": "warn",
            "enqueued_at": "2026-09-01T12:00:00", "status": "pending",
            "attempts": 0})

        async def boom(page, item):
            raise RuntimeError("计划行未找到")

        orig = budget_watch._topup_one
        budget_watch._topup_one = boom
        try:
            for expected in ("retry:777", "retry:777", "dead:777"):
                results = budget_watch.process_topup_queue()
                self.assertEqual(results, [expected])
        finally:
            budget_watch._topup_one = orig
        self.assertEqual(self.queue_box[0]["status"], "dead")
        self.assertTrue(any("自动加预算失败" in md for md in self.pushed))

    def test_process_queue_window_guard(self) -> None:
        self.queue_box.append({
            "plan_id": "777", "plan_name": "X", "department": "业务3部",
            "level": "warn",
            "enqueued_at": "2026-09-01T12:00:00", "status": "pending",
            "attempts": 0})
        budget_watch.in_report_window = lambda *a, **kw: True
        self.assertEqual(budget_watch.process_topup_queue(), [])
        self.assertEqual(self.queue_box[0]["status"], "pending")  # 原样留待下轮

    def test_process_queue_lock_busy(self) -> None:
        self.queue_box.append({
            "plan_id": "777", "plan_name": "X", "department": "业务3部",
            "level": "warn",
            "enqueued_at": "2026-09-01T12:00:00", "status": "pending",
            "attempts": 0})
        budget_watch._try_action_lock = lambda: None  # 锁被 plan_power 占用
        self.assertEqual(budget_watch.process_topup_queue(), [])
        self.assertEqual(self.queue_box[0]["status"], "pending")

    def test_unlimited_budget_skipped(self) -> None:
        self.queue_box.append({
            "plan_id": "777", "plan_name": "X", "department": "业务3部",
            "level": "warn",
            "enqueued_at": "2026-09-01T12:00:00", "status": "pending",
            "attempts": 0})

        async def fake_topup(page, item):
            return {"before": 99999.0, "after": 99999.0, "skipped": "已不限预算"}

        orig = budget_watch._topup_one
        budget_watch._topup_one = fake_topup
        try:
            results = budget_watch.process_topup_queue()
        finally:
            budget_watch._topup_one = orig
        self.assertEqual(results, ["skipped:777"])
        self.assertEqual(self.queue_box[0]["status"], "done")
        self.assertEqual(self.state_box.get("topups"), None)  # 未计数未推送


if __name__ == "__main__":
    unittest.main()
