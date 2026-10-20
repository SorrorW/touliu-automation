# -*- coding: utf-8 -*-
"""生命周期规则的纯逻辑回归测试，不连接浏览器。"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from 巨量千川 import plan_power
from 巨量千川.lifecycle import classify_plan_lifecycle

# 单测不污染生产日志（plan_power 模块级 FileHandler 会写 logs/plan_power_YYYYMMDD.log，
# 2026-09-03 曾因批量暂停用例往生产日志写入「0824_新A_黄」等假条目造成误判）
plan_power.log.handlers = [
    h for h in plan_power.log.handlers
    if not isinstance(h, logging.FileHandler)
]


class LifecycleTests(unittest.TestCase):
    def test_key_plan_requires_roi_over_ten_and_orders_at_least_ten(self):
        result = classify_plan_lifecycle(roi=10.1, orders=10, is_today_new=False)
        self.assertEqual(result.category, "key_plan")
        self.assertTrue(result.keep_running_at_close)
        self.assertEqual(result.roi_target, 10.3)
        self.assertEqual(result.daily_budget, 1000)  # 2026-09-02 重点初始预算 1000

    def test_roi_over_ten_but_fewer_than_ten_orders_is_potential(self):
        result = classify_plan_lifecycle(roi=10.1, orders=9, is_today_new=False)
        self.assertEqual(result.category, "potential_plan")
        # 2026-08-21 起：潜力计划收口时改写为保 ROI 7.3、日预算 300
        self.assertEqual(result.roi_target, 7.3)
        self.assertEqual(result.daily_budget, 300)

    def test_six_to_ten_roi_with_at_least_ten_orders_is_potential(self):
        result = classify_plan_lifecycle(roi=6.1, orders=10, is_today_new=False)
        self.assertEqual(result.category, "potential_plan")
        self.assertEqual(result.roi_target, 7.3)
        self.assertEqual(result.daily_budget, 300)

    def test_six_to_ten_roi_with_four_orders_is_potential(self):
        # 2026-09-01 用户定：转潜力须 >= 4 单（边界值 4 有效）
        result = classify_plan_lifecycle(roi=6.1, orders=4, is_today_new=False)
        self.assertEqual(result.category, "potential_plan")
        self.assertEqual(result.roi_target, 7.3)
        self.assertEqual(result.daily_budget, 300)

    def test_six_to_ten_roi_without_orders_is_old_pool(self):
        # 2026-09-01 用户定：6<ROI<10 但订单 < 4 不转潜力，归老计划池
        #（废止 2026-08-24「进潜力不问订单数」口径）
        for zero_orders in (0, 1, 3):
            result = classify_plan_lifecycle(roi=6.1, orders=zero_orders,
                                             is_today_new=False)
            self.assertEqual(result.category, "old_pool", f"orders={zero_orders}")

    def test_roi_over_ten_with_three_orders_is_old_pool(self):
        # ROI>10 但订单 <4 同样不满足潜力门槛（>=4 才有效）
        result = classify_plan_lifecycle(roi=12.0, orders=3, is_today_new=False)
        self.assertEqual(result.category, "old_pool")
        # 边界：4 单即转潜力
        result = classify_plan_lifecycle(roi=12.0, orders=4, is_today_new=False)
        self.assertEqual(result.category, "potential_plan")

    def test_new_plan_without_effective_roi_stays_in_test(self):
        result = classify_plan_lifecycle(roi=None, orders=0, is_today_new=True)
        self.assertEqual(result.category, "new_plan")
        self.assertEqual(result.roi_target, 4.5)
        self.assertEqual(result.daily_budget, 300)

    def test_failed_new_plan_falls_into_old_pool(self):
        result = classify_plan_lifecycle(roi=4.5, orders=0, is_today_new=True)
        self.assertEqual(result.category, "old_pool")
        self.assertEqual(result.roi_target, 10.3)
        self.assertEqual(result.daily_budget, 300)

    def test_cross_day_roi_target_test_plan_still_uses_new_plan_rules(self):
        # 调用方将 ROI 目标 4.5 的跨日计划传为 is_today_new=True；有有效
        # 但不达标的数据时，应迁入老计划池，而不是按名称日期直接误分类。
        result = classify_plan_lifecycle(roi=4.5, orders=0, is_today_new=True)
        self.assertEqual(result.category, "old_pool")

    def test_cross_day_roi_target_test_plan_without_roi_stays_in_test(self):
        result = classify_plan_lifecycle(roi=None, orders=0, is_today_new=True)
        self.assertEqual(result.category, "new_plan")

    def test_manual_whitelist_never_enters_automatic_actions(self):
        result = classify_plan_lifecycle(
            roi=12.0, orders=20, is_today_new=False, manual_label="大梦吴恙"
        )
        self.assertEqual(result.category, "manual_review")
        self.assertFalse(result.keep_running_at_close)

    def test_a_key_label_does_not_override_live_decline(self):
        result = classify_plan_lifecycle(
            roi=8.0, orders=10, is_today_new=False, name_label="重点计划-测试"
        )
        self.assertEqual(result.category, "potential_plan")


class NightStartDateTests(unittest.TestCase):
    """_night_start_date：23:00 起算当天，其余时刻返回最近一夜的起点日期。"""

    def test_2300_belongs_to_today(self):
        self.assertEqual(
            plan_power._night_start_date(datetime.datetime(2026, 8, 21, 23, 0)),
            "2026-08-21",
        )

    def test_2259_belongs_to_previous_night(self):
        self.assertEqual(
            plan_power._night_start_date(datetime.datetime(2026, 8, 21, 22, 59)),
            "2026-08-20",
        )

    def test_midnight_to_morning_belongs_to_previous_night(self):
        for hour in (0, 3, 7):
            self.assertEqual(
                plan_power._night_start_date(datetime.datetime(2026, 8, 21, hour, 30)),
                "2026-08-20",
            )

    def test_daytime_returns_most_recent_night_start(self):
        # 白天不在建计划窗口内，轮询侧的窗口门会挡住；写门侧只在 23:00 调用
        self.assertEqual(
            plan_power._night_start_date(datetime.datetime(2026, 8, 21, 12, 0)),
            "2026-08-20",
        )


class MarkLifecycleReadyTests(unittest.TestCase):
    """23:00 收口写门：扫描错误挡门，暂停/恢复失败只登记名单、不挡门。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gate_file = Path(self._tmp.name) / "gate.json"
        self._orig_gate = plan_power.LIFECYCLE_GATE_FILE
        self._orig_nsd = plan_power._night_start_date
        plan_power.LIFECYCLE_GATE_FILE = self.gate_file
        plan_power._night_start_date = lambda now=None: "2026-08-21"
        # 2026-09-29 事故：测试触发 _alert_gate_blocked 把假「闸门未开」推进了真群
        # （门文件被替换到 tmp，但告警没 mock）——测试里告警一律拦截。
        self._orig_alert = plan_power._alert_gate_blocked
        plan_power._alert_gate_blocked = lambda errors: None
        self.addCleanup(self._restore)

    def _restore(self):
        plan_power.LIFECYCLE_GATE_FILE = self._orig_gate
        plan_power._night_start_date = self._orig_nsd
        plan_power._alert_gate_blocked = self._orig_alert

    def _read_gate(self):
        return json.loads(self.gate_file.read_text(encoding="utf-8"))

    def test_no_scan_errors_marks_ready_with_counts(self):
        plan_power._mark_lifecycle_ready({
            "plans": {"业务1部": [{"plan_name": "a"}, {"plan_name": "b"}]},
            "kept_key_plans": [{"plan_name": "k"}],
            "paused": [{"plan_name": "p1"}, {"plan_name": "p2"}],
            "failed": [{"plan_name": "f1", "department": "业务1部"}],
            "reopen_failed": [{"department": "业务2部"}],
        })
        gate = self._read_gate()
        self.assertEqual(gate["night_start_date"], "2026-08-21")
        self.assertTrue(gate["classification_ready"])
        self.assertEqual(gate["scan_errors"], [])
        self.assertEqual(gate["kept_key_plans"], 1)
        self.assertEqual(gate["closed_non_key"], 2)
        self.assertEqual(gate["close_failures"], 1)
        self.assertEqual(gate["reopen_failures"], 1)
        # 暂停/恢复失败不挡门，但名单必须可见：plan_name 优先，缺失用 department
        self.assertEqual(gate["close_failed_plans"], ["f1", "业务2部"])

    def test_scan_errors_block_the_gate(self):
        plan_power._mark_lifecycle_ready({
            "plans": {"业务3部": [{"error": "timeout", "department": "业务3部"}]},
        })
        gate = self._read_gate()
        self.assertFalse(gate["classification_ready"])
        self.assertEqual(len(gate["scan_errors"]), 1)

    def test_failed_plans_list_skips_non_dict_and_caps_at_50(self):
        failed = [{"plan_name": f"p{i}"} for i in range(55)] + ["oops", None]
        plan_power._mark_lifecycle_ready({"failed": failed})
        gate = self._read_gate()
        self.assertEqual(len(gate["close_failed_plans"]), 50)
        self.assertEqual(gate["close_failed_plans"][0], "p0")

    def test_unknown_entry_falls_back_to_question_mark(self):
        plan_power._mark_lifecycle_ready({"failed": [{}]})
        self.assertEqual(self._read_gate()["close_failed_plans"], ["?"])


class OldPoolResumeQueueTests(unittest.TestCase):
    """12:00 恢复链：登记去重、空队列兜底、到期/stale 过滤、全 stale 清队列。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.queue_file = tmp / "queue.json"
        self.state_file = tmp / "state.json"
        self._orig = {
            name: getattr(plan_power, name)
            for name in ("OLD_POOL_RESUME_FILE", "STATE_FILE", "_today",
                         "_verified_stale_zero_order_ids", "_adopt_manual_new_plans",
                         "demote_key_low_roi")
        }
        plan_power.OLD_POOL_RESUME_FILE = self.queue_file
        plan_power.STATE_FILE = self.state_file
        plan_power._today = lambda: "2026-08-21"
        plan_power._verified_stale_zero_order_ids = lambda: set()

        async def _fake_adopt():  # 收养会开浏览器，队列测试一律打桩
            return {"candidates": 0, "adopted": [], "failed": []}
        plan_power._adopt_manual_new_plans = _fake_adopt

        async def _fake_key_gate(*a, **kw):  # 重点盘中闸同理打桩
            return {"targets": [], "demoted": [], "failed": []}
        plan_power.demote_key_low_roi = _fake_key_gate
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._orig.items():
            setattr(plan_power, name, value)

    def _write_queue(self, pending):
        self.queue_file.write_text(
            json.dumps({"pending": pending}, ensure_ascii=False), encoding="utf-8")

    def _read_queue(self):
        return json.loads(self.queue_file.read_text(encoding="utf-8"))["pending"]

    def test_queue_defaults_resume_date_by_time_of_day(self):
        # 23:00 正常收口 → 次日恢复；白天补跑（12 点前）→ 当天恢复；12 点后 → 次日
        self.assertEqual(
            plan_power._default_resume_date(datetime.datetime(2026, 8, 21, 23, 0)),
            "2026-08-22")
        self.assertEqual(
            plan_power._default_resume_date(datetime.datetime(2026, 8, 21, 9, 42)),
            "2026-08-21")
        self.assertEqual(
            plan_power._default_resume_date(datetime.datetime(2026, 8, 21, 11, 59)),
            "2026-08-21")
        self.assertEqual(
            plan_power._default_resume_date(datetime.datetime(2026, 8, 21, 12, 0)),
            "2026-08-22")

    def test_queue_defaults_resume_to_tomorrow_and_dedupes(self):
        expected_default = plan_power._default_resume_date()
        item = {"plan_id": "1", "plan_name": "a", "department": "业务1部"}
        entries = plan_power._queue_old_pool_resume([item])
        self.assertEqual(entries[0]["resume_date"], expected_default)
        self.assertTrue(entries[0].get("queued_at"))
        # 同 plan_id 再登记不重复入队；显式日期优先
        plan_power._queue_old_pool_resume([item])
        plan_power._queue_old_pool_resume(
            [{"plan_id": "2", "plan_name": "b", "department": "业务2部"}],
            resume_date="2026-08-25")
        pending = self._read_queue()
        self.assertEqual([p["plan_id"] for p in pending], ["1", "2"])
        self.assertEqual(pending[1]["resume_date"], "2026-08-25")

    def test_merge_refreshes_entry_with_later_resume_date(self):
        # 2026-09-01：「建成即暂停挂08:00」的新计划当天12:00被恢复、12:28又被
        # 熔断暂停并挂次日12:00——旧去重直接丢新条目，计划滞留过期队列票。
        plan_power._queue_old_pool_resume(
            [{"plan_id": "1", "plan_name": "a", "department": "业务1部",
              "category": "new_plan"}], resume_date="2026-08-21", resume_time="08:00")
        plan_power._queue_old_pool_resume(
            [{"plan_id": "1", "plan_name": "a", "department": "业务1部",
              "category": "old_pool"}], resume_date="2026-08-22", resume_time="12:00")
        pending = self._read_queue()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["resume_date"], "2026-08-22")
        self.assertEqual(pending[0]["resume_time"], "12:00")
        self.assertEqual(pending[0]["category"], "old_pool")
        # 同日重复登记仍是去重 no-op；更早日期不覆盖更晚日期
        plan_power._queue_old_pool_resume(
            [{"plan_id": "1", "plan_name": "a", "department": "业务1部",
              "category": "new_plan"}], resume_date="2026-08-21", resume_time="08:00")
        pending = self._read_queue()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["resume_date"], "2026-08-22")

    def test_recover_falls_back_to_yesterday_state(self):
        self._write_queue([])  # 队列文件为空时，从昨日收口状态找回老池
        self.state_file.write_text(json.dumps({
            "date": "2026-08-20",
            "closed_non_key": [
                {"plan_id": "1", "plan_name": "old", "category": "old_pool",
                 "department": "业务1部"},
                {"plan_id": "2", "plan_name": "key", "category": "key_plan",
                 "department": "业务1部"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        pending = plan_power._recover_old_pool_resume_queue()
        self.assertEqual([p["plan_id"] for p in pending], ["1"])
        self.assertEqual(pending[0]["resume_date"], "2026-08-21")

    def test_recover_prefers_existing_queue(self):
        # 2026-08-21 起：队列非空也要用昨日收口状态补齐（队列是执行状态，
        # 不是完整业务名单），已有项保留在前、补入项去重追加。
        import webui.db as db
        orig_db = db.list_plan_daily_labels
        self.addCleanup(setattr, db, "list_plan_daily_labels", orig_db)
        db.list_plan_daily_labels = lambda _date: []  # 隔离真实库，走兼容路径
        self._write_queue([{"plan_id": "9", "plan_name": "q",
                            "department": "业务1部", "resume_date": "2026-08-21"}])
        self.state_file.write_text(json.dumps({
            "date": "2026-08-20",
            "closed_non_key": [{"plan_id": "1", "category": "old_pool"}],
        }, ensure_ascii=False), encoding="utf-8")
        pending = plan_power._recover_old_pool_resume_queue()
        self.assertEqual([p["plan_id"] for p in pending], ["9", "1"])

    def test_label_fallback_covers_paused_potential_plans(self):
        # 2026-09-01：23:00 收口只给「投放中」计划挂恢复队列，17:10 前已暂停的
        # 潜力计划既不在队列、兜底又只认老池标签 → 永久停放（0829_六六爱吃_黄）。
        # 兜底必须同时认 old_pool / potential_plan 标签，潜力挂 08:00 恢复。
        import webui.db as db
        labels = [
            {"plan_id": "p1", "plan_name": "潜力A", "department": "业务3部",
             "category": "potential_plan", "plan_status": "已暂停", "scan_slot": "23:00"},
            {"plan_id": "o1", "plan_name": "老池B", "department": "业务1部",
             "category": "old_pool", "plan_status": "已暂停", "scan_slot": "23:00"},
            {"plan_id": "k1", "plan_name": "重点C", "department": "业务1部",
             "category": "key_plan", "plan_status": "已暂停", "scan_slot": "23:00"},
            {"plan_id": "r1", "plan_name": "在投D", "department": "业务1部",
             "category": "old_pool", "plan_status": "投放中", "scan_slot": "23:00"},
            {"plan_id": "x1", "plan_name": "早间档E", "department": "业务1部",
             "category": "old_pool", "plan_status": "已暂停", "scan_slot": "08:00"},
        ]
        orig = {name: getattr(plan_power, name) for name in (
            "_load_verified_recent_order_ids", "_plan_created_within_15_days",
            "is_manual_whitelisted")}
        orig_db = db.list_plan_daily_labels

        def _restore_mocks():
            for name, value in orig.items():
                setattr(plan_power, name, value)
            db.list_plan_daily_labels = orig_db

        self.addCleanup(_restore_mocks)
        db.list_plan_daily_labels = lambda _date: labels
        plan_power._load_verified_recent_order_ids = lambda: {}
        plan_power._plan_created_within_15_days = lambda _pid, _today: True
        plan_power.is_manual_whitelisted = lambda **_kw: False

        cands = plan_power._database_old_pool_resume_candidates("2026-08-21")
        by_id = {c["plan_id"]: c for c in cands}
        self.assertEqual(sorted(by_id), ["o1", "p1"])
        self.assertEqual(by_id["p1"]["category"], "potential_plan")
        self.assertEqual(by_id["p1"]["resume_time"], "08:00")
        self.assertEqual(by_id["o1"]["resume_time"], "12:00")
        self.assertEqual(by_id["o1"]["resume_date"], "2026-08-21")

    def test_resume_dry_run_filters_by_due_date_and_stale(self):
        plan_power._verified_stale_zero_order_ids = lambda: {"3"}
        today = plan_power._today()
        yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
        tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
        self._write_queue([
            {"plan_id": "1", "plan_name": "due_today", "department": "业务1部",
             "resume_date": today, "resume_time": "00:00"},
            {"plan_id": "2", "plan_name": "overdue", "department": "业务1部",
             "resume_date": yesterday, "resume_time": "00:00"},
            {"plan_id": "3", "plan_name": "stale", "department": "业务2部",
             "resume_date": today, "resume_time": "00:00"},
            {"plan_id": "4", "plan_name": "future", "department": "业务1部",
             "resume_date": tomorrow, "resume_time": "00:00"},
        ])
        summary = asyncio.run(plan_power.resume_old_pool(dry_run=True))
        names = {t["plan_name"] for t in summary["targets"]}
        self.assertEqual(names, {"due_today", "overdue"})
        # 已暂停计划当天可能无消耗，恢复时不能带时间筛选，否则会找不到
        self.assertTrue(all(t["time_range"] is None for t in summary["targets"]))
        # dry-run 不动队列文件
        self.assertEqual(len(self._read_queue()), 4)

    def test_all_stale_due_items_are_removed_from_queue_without_browser(self):
        plan_power._verified_stale_zero_order_ids = lambda: {"3"}
        self._write_queue([
            {"plan_id": "3", "plan_name": "stale", "department": "业务2部",
             "resume_date": plan_power._today(), "resume_time": "00:00"},
        ])
        summary = asyncio.run(plan_power.resume_old_pool(dry_run=False))
        self.assertEqual(len(summary["skipped_stale_zero_order"]), 1)
        self.assertEqual(self._read_queue(), [])


class StaleLabelTests(unittest.TestCase):
    """无效计划标签（2026-08-24 白板策略⑤）：已核验 15 天 0 单 → invalid_plan。"""

    def test_stale_plan_marked_invalid(self):
        entry = {"plan_id": "123", "category": "old_pool",
                 "keep_running_at_close": False}
        plan_power._apply_stale_label(entry, {"123"})
        self.assertEqual(entry["category"], "invalid_plan")
        self.assertFalse(entry["keep_running_at_close"])

    def test_stale_overrides_even_key_plan(self):
        # 数据突变被分类成重点也不能留：防止 23:00 收口后重新开启
        entry = {"plan_id": "123", "category": "key_plan",
                 "keep_running_at_close": True}
        plan_power._apply_stale_label(entry, {"123"})
        self.assertEqual(entry["category"], "invalid_plan")
        self.assertFalse(entry["keep_running_at_close"])

    def test_whitelist_wins_over_stale(self):
        entry = {"plan_id": "123", "category": "manual_review",
                 "keep_running_at_close": False}
        plan_power._apply_stale_label(entry, {"123"})
        self.assertEqual(entry["category"], "manual_review")

    def test_new_material_test_wins_over_stale(self):
        # 2026-08-24 用户规则：有新素材加入（重进新计划测试）的标签跟随变化，不压无效
        entry = {"plan_id": "123", "category": "new_plan", "is_today_new": True,
                 "keep_running_at_close": False}
        plan_power._apply_stale_label(entry, {"123"})
        self.assertEqual(entry["category"], "new_plan")

    def test_non_stale_untouched(self):
        entry = {"plan_id": "456", "category": "old_pool"}
        plan_power._apply_stale_label(entry, {"123"})
        self.assertEqual(entry["category"], "old_pool")


class CloseOldPoolMigrationTests(unittest.TestCase):
    """23:00 收口：零数据/未达标新计划迁日常（10.3/300、次日 12:00 恢复）。

    2026-08-24 新策略：23:00 仍零数据的新计划同步降日常，不再保留到次日 08:00。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._orig = {name: getattr(plan_power, name)
                      for name in ("STATE_FILE", "OLD_POOL_RESUME_FILE",
                                   "LIFECYCLE_GATE_FILE", "monitor",
                                   "_pause_department", "_ensure_cdp",
                                   "_maybe_spawn_order_report_refresh",
                                   "_alert_gate_blocked")}
        plan_power.STATE_FILE = tmp / "state.json"
        plan_power.OLD_POOL_RESUME_FILE = tmp / "queue.json"
        plan_power.LIFECYCLE_GATE_FILE = tmp / "gate.json"
        # 收口尾巴的 15 天报告重跑闸：测试里绝不拉起真实核验进程
        plan_power._maybe_spawn_order_report_refresh = lambda: "test-skip"
        # 测试里绝不向群里推真实告警（同 GateTests 的 09-29 教训）
        plan_power._alert_gate_blocked = lambda errors: None
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._orig.items():
            setattr(plan_power, name, value)

    def _read_queue(self):
        if not plan_power.OLD_POOL_RESUME_FILE.exists():
            return []
        return json.loads(
            plan_power.OLD_POOL_RESUME_FILE.read_text(encoding="utf-8"))["pending"]

    def test_zero_data_new_plan_migrates_and_queues_noon(self):
        report = {"plans": {"业务1部": [
            # 23:00 仍零数据的新计划：降日常（10.3/300），次日 12:00 恢复
            {"department": "业务1部", "plan_id": "n1", "plan_name": "0824_新A_黄",
             "status": "投放中", "category": "new_plan", "is_today_new": True,
             "roi": None, "orders": 0, "spent": 0,
             "roi_target": 4.5, "daily_budget": 300},
            # 有数据未达标的新计划：同样迁日常
            {"department": "业务1部", "plan_id": "n2", "plan_name": "0824_新B_黄",
             "status": "投放中", "category": "old_pool", "is_today_new": True,
             "roi": 3.0, "orders": 0, "spent": 120,
             "roi_target": 4.5, "daily_budget": 300},
            # 潜力计划：不迁日常参数，挂次日 08:00
            {"department": "业务1部", "plan_id": "p1", "plan_name": "0823_潜C_黄",
             "status": "投放中", "category": "potential_plan", "is_today_new": False,
             "roi": 8.0, "orders": 3, "spent": 200,
             "roi_target": 7.3, "daily_budget": 300},
        ]}}
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

        async def fake_monitor(dry_run=False, scan_slot=None):
            return report

        async def fake_apply(plans, roi=10.3, budget=300):
            return {"changed": [p["plan_id"] for p in plans], "failed": [], "skipped": []}

        async def fake_pause_department(department, dry_run=False, record_midnight=False):
            return {"paused_plan_ids": ["n1", "n2", "p1"]}

        import 巨量千川.old_plan_pool as old_plan_pool
        orig_apply = old_plan_pool.apply_old_pool
        old_plan_pool.apply_old_pool = fake_apply
        self.addCleanup(lambda: setattr(old_plan_pool, "apply_old_pool", orig_apply))
        plan_power.monitor = fake_monitor
        plan_power._pause_department = fake_pause_department
        plan_power._ensure_cdp = lambda: None

        summary = asyncio.run(plan_power.close_old_pool(dry_run=False))

        # 两条新计划都迁日常参数成功
        self.assertEqual({i["plan_id"] for i in summary["migrated_to_old_pool"]},
                         {"n1", "n2"})
        # 迁移后身份改写为日常计划
        self.assertTrue(all(i["category"] == "old_pool"
                            for i in summary["migrated_to_old_pool"]))
        # 恢复队列：迁日常的挂次日 12:00，潜力挂次日 08:00
        queue = {p["plan_id"]: p for p in self._read_queue()}
        self.assertEqual(queue["n1"]["category"], "old_pool")
        self.assertEqual(queue["n1"]["resume_time"], "12:00")
        self.assertEqual(queue["n1"]["resume_date"], tomorrow)
        self.assertEqual(queue["n2"]["resume_time"], "12:00")
        self.assertEqual(queue["p1"]["category"], "potential_plan")
        self.assertEqual(queue["p1"]["resume_time"], "08:00")

    def test_recent_order_ghost_plans_not_requeued(self):
        """回补存活校验（2026-09-15 幽灵票病根）：15 天报告里已被平台删除的
        计划（catalog 无记录/last_seen 冻结超 2 天）不再回补挂票。"""
        report = {"plans": {}}
        now = time.time()

        async def fake_monitor(dry_run=False, scan_slot=None, final_classification=False):
            return report

        plan_power.monitor = fake_monitor
        orig = {name: getattr(plan_power, name) for name in (
            "_load_verified_recent_order_ids", "_verified_stale_zero_order_ids",
            "_catalog_last_seen_map", "is_manual_whitelisted")}
        self.addCleanup(lambda: [setattr(plan_power, k, v) for k, v in orig.items()])
        plan_power._load_verified_recent_order_ids = lambda: {
            "fresh": {"plan_id": "fresh", "plan_name": "活着的", "department": "业务1部",
                      "near_15_days_orders": 3},
            "stale": {"plan_id": "stale", "plan_name": "荔枝酱0814", "department": "业务3部",
                      "near_15_days_orders": 2},
            "gone": {"plan_id": "gone", "plan_name": "查无此计划", "department": "业务2部",
                     "near_15_days_orders": 5},
        }
        plan_power._verified_stale_zero_order_ids = lambda: set()
        plan_power._catalog_last_seen_map = lambda: {"fresh": now - 3600,
                                                     "stale": now - 3 * 86400}
        plan_power.is_manual_whitelisted = lambda **_kw: False

        summary = asyncio.run(plan_power.close_old_pool(dry_run=False))

        queued = {p["plan_id"] for p in self._read_queue()}
        self.assertEqual(queued, {"fresh"})
        ghosts = {g["plan_id"] for g in summary["recent_order_ghost_skipped"]}
        self.assertEqual(ghosts, {"stale", "gone"})
        # catalog 读失败（None）时不校验、全部放行
        plan_power._catalog_last_seen_map = lambda: None
        summary2 = asyncio.run(plan_power.close_old_pool(dry_run=False))
        self.assertEqual(summary2["recent_order_ghost_skipped"], [])
        self.assertEqual(len(summary2["recent_order_recovery_candidates"]), 3)


class BusyLockTests(unittest.TestCase):
    """浏览器互斥锁：启停任务持锁/释放、轮询轮次存活探测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._orig = {name: getattr(plan_power, name)
                      for name in ("_BUSY_FILE", "_POLL_LOCK_FILE", "_POLL_STATUS_FILE")}
        plan_power._BUSY_FILE = tmp / "busy.json"
        plan_power._POLL_LOCK_FILE = tmp / "poll.lock"
        plan_power._POLL_STATUS_FILE = tmp / "status.json"
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._orig.items():
            setattr(plan_power, name, value)

    def test_busy_mark_and_clear(self):
        plan_power._busy_mark("close-old-pool")
        payload = json.loads(plan_power._BUSY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["action"], "close-old-pool")
        self.assertGreater(payload["ts"], 0)
        plan_power._busy_clear()
        self.assertFalse(plan_power._BUSY_FILE.exists())
        plan_power._busy_clear()  # 重复清理不报错

    def test_poll_round_running_requires_lock_and_fresh_running_status(self):
        self.assertFalse(plan_power._poll_round_running())  # 无锁
        plan_power._POLL_LOCK_FILE.write_text("1", encoding="utf-8")
        self.assertFalse(plan_power._poll_round_running())  # 有锁但无状态文件
        plan_power._POLL_STATUS_FILE.write_text(
            json.dumps({"state": "running"}), encoding="utf-8")
        self.assertTrue(plan_power._poll_round_running())
        plan_power._POLL_STATUS_FILE.write_text(
            json.dumps({"state": "idle"}), encoding="utf-8")
        self.assertFalse(plan_power._poll_round_running())
        # 状态文件 5 分钟未更新视为崩溃残留，不误判轮询活着
        plan_power._POLL_STATUS_FILE.write_text(
            json.dumps({"state": "running"}), encoding="utf-8")
        stale = time.time() - 400
        os.utime(plan_power._POLL_STATUS_FILE, (stale, stale))
        self.assertFalse(plan_power._poll_round_running())


class OldPoolPauseTargetTests(unittest.TestCase):
    """17:10 老池停运的目标过滤：只打在投的老计划池，新计划测试留到 23:00。"""

    def _report(self, entries_by_dept):
        return {"plans": entries_by_dept}

    def test_picks_only_running_old_pool(self):
        report = self._report({
            "业务1部": [
                {"plan_id": "1", "plan_name": "老A", "status": "投放中", "category": "old_pool"},
                {"plan_id": "2", "plan_name": "老B", "status": "已暂停", "category": "old_pool"},
                {"plan_id": "3", "plan_name": "重点C", "status": "投放中", "category": "key_plan"},
                {"plan_id": "4", "plan_name": "潜力D", "status": "投放中", "category": "potential_plan"},
                # 当天新建的老池参数计划仍在测试，23:00 收口统一处理
                {"plan_id": "5", "plan_name": "新E", "status": "投放中",
                 "category": "old_pool", "is_today_new": True},
            ],
        })
        by_dept, errors = plan_power._old_pool_pause_targets(report)
        self.assertEqual(errors, [])
        self.assertEqual([i["plan_name"] for i in by_dept["业务1部"]], ["老A"])
        self.assertEqual(by_dept["业务1部"][0]["department"], "业务1部")

    def test_scan_error_reported_not_silently_skipped(self):
        report = self._report({
            "业务2部": [{"error": "页面崩溃"}],
            "业务3部": [
                {"plan_id": "9", "plan_name": "老F", "status": "投放中(预算不足)", "category": "old_pool"},
            ],
        })
        by_dept, errors = plan_power._old_pool_pause_targets(report)
        self.assertEqual(errors, [{"department": "业务2部", "error": "页面崩溃"}])
        # 状态含“投放中”即视为在投（预算不足仍在跑量）
        self.assertEqual([i["plan_name"] for i in by_dept["业务3部"]], ["老F"])

    def test_empty_report_yields_no_targets(self):
        by_dept, errors = plan_power._old_pool_pause_targets({"plans": {}})
        self.assertEqual(by_dept, {})
        self.assertEqual(errors, [])

    def test_dry_run_lists_targets_without_touching_queue(self):
        report = self._report({
            "业务1部": [
                {"plan_id": "1", "plan_name": "老A", "status": "投放中", "category": "old_pool"},
            ],
        })
        orig_monitor = plan_power.monitor
        orig_queue_file = plan_power.OLD_POOL_RESUME_FILE
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plan_power.OLD_POOL_RESUME_FILE = Path(tmp.name) / "queue.json"
        self.addCleanup(setattr, plan_power, "OLD_POOL_RESUME_FILE", orig_queue_file)

        async def fake_monitor(dry_run=False):
            return report

        plan_power.monitor = fake_monitor
        self.addCleanup(setattr, plan_power, "monitor", orig_monitor)

        summary = asyncio.run(plan_power.pause_old_pool(dry_run=True))
        self.assertEqual([t["plan_name"] for t in summary["targets"]], ["老A"])
        self.assertEqual(summary["paused"], [])
        # dry-run 不写恢复队列
        self.assertFalse(plan_power.OLD_POOL_RESUME_FILE.exists())


class NewPlanLowRoiTests(unittest.TestCase):
    """当日新计划熔断（2026-08-23 策略）：12:20 停 ROI<4.5、17:00 停 ROI<6。"""

    def _report(self, entries):
        return {"plans": {"业务1部": entries}}

    def _entry(self, **kw):
        base = {"plan_id": "1", "plan_name": "0823_达人_张", "status": "投放中",
                "category": "new_plan", "is_today_new": True, "spent": 50.0,
                "roi": 3.0, "orders": 1}
        base.update(kw)
        return base

    def test_pauses_low_roi_today_new_with_spend(self):
        by_dept, errors = plan_power._new_plan_low_roi_targets(
            self._report([self._entry()]), 4.5)
        self.assertEqual(errors, [])
        self.assertEqual(len(by_dept["业务1部"]), 1)

    def test_keeps_roi_at_or_above_threshold(self):
        by_dept, _ = plan_power._new_plan_low_roi_targets(
            self._report([self._entry(roi=4.5), self._entry(plan_id="2", roi=6.0)]), 4.5)
        self.assertEqual(by_dept, {})

    def test_pauses_spent_but_no_roi(self):
        # 有消耗无成交（ROI 空）= 必然低于线，「不管消耗多少都停」
        by_dept, _ = plan_power._new_plan_low_roi_targets(
            self._report([self._entry(roi=None, spent=0.01)]), 4.5)
        self.assertEqual(len(by_dept["业务1部"]), 1)

    def test_keeps_zero_spend_no_data(self):
        # 零消耗没有判定依据，继续跑
        by_dept, _ = plan_power._new_plan_low_roi_targets(
            self._report([self._entry(roi=None, spent=0)]), 4.5)
        self.assertEqual(by_dept, {})

    def test_skips_not_today_new_paused_and_whitelist(self):
        by_dept, _ = plan_power._new_plan_low_roi_targets(self._report([
            self._entry(plan_id="1", is_today_new=False),            # 非当日新计划
            self._entry(plan_id="2", status="已暂停"),                # 没在投
            self._entry(plan_id="3", category="manual_review"),      # 人工白名单
        ]), 4.5)
        self.assertEqual(by_dept, {})

    def test_scan_error_reported(self):
        by_dept, errors = plan_power._new_plan_low_roi_targets(
            {"plans": {"业务2部": [{"error": "timeout"}]}}, 4.5)
        self.assertEqual(by_dept, {})
        self.assertEqual(len(errors), 1)


class ResumeTimeTests(unittest.TestCase):
    """恢复时刻分流（2026-08-23 策略）：老池 12:00，新计划/潜力 08:00。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._orig = {name: getattr(plan_power, name)
                      for name in ("OLD_POOL_RESUME_FILE", "STATE_FILE", "_today",
                                   "_verified_stale_zero_order_ids")}
        plan_power.OLD_POOL_RESUME_FILE = tmp / "queue.json"
        plan_power.STATE_FILE = tmp / "state.json"
        plan_power._today = lambda: "2026-08-23"
        plan_power._verified_stale_zero_order_ids = lambda: set()
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._orig.items():
            setattr(plan_power, name, value)

    def test_default_resume_time_by_category(self):
        plan_power._queue_old_pool_resume([
            {"plan_id": "1", "plan_name": "老", "department": "d", "category": "old_pool"},
            {"plan_id": "2", "plan_name": "新", "department": "d", "category": "new_plan"},
            {"plan_id": "3", "plan_name": "潜", "department": "d", "category": "potential_plan"},
        ], resume_date="2026-08-23")
        pending = json.loads(plan_power.OLD_POOL_RESUME_FILE.read_text(encoding="utf-8"))["pending"]
        times = {p["plan_id"]: p["resume_time"] for p in pending}
        self.assertEqual(times, {"1": "12:00", "2": "08:00", "3": "08:00"})


    def test_retired_matrix_queue_is_never_a_restore_target(self):
        plan_power._queue_old_pool_resume([
            {"plan_id": "retired", "plan_name": "旧矩阵", "department": "业务1部-1",
             "category": "old_pool"},
            {"plan_id": "live", "plan_name": "普通部门", "department": "业务1部",
             "category": "old_pool"},
        ], resume_date="2026-08-23", resume_time="00:00")
        summary = asyncio.run(plan_power.resume_old_pool(dry_run=True))
        self.assertEqual({t["plan_id"] for t in summary["targets"]}, {"live"})

    def test_future_resume_time_not_due(self):
        # resume_time 23:59 的任务在白天跑恢复时不应被捞起；
        # resume_time 00:00 / 缺字段（旧队列按 12:00）必然已到点（测试不会跑在 0 点前）
        plan_power._queue_old_pool_resume([
            {"plan_id": "1", "plan_name": "今晚才恢复", "department": "d",
             "category": "new_plan"},
        ], resume_date="2026-08-23", resume_time="23:59")
        plan_power._queue_old_pool_resume([
            {"plan_id": "2", "plan_name": "永远到点", "department": "d",
             "category": "new_plan"},
        ], resume_date="2026-08-23", resume_time="00:00")
        summary = asyncio.run(plan_power.resume_old_pool(dry_run=True))
        names = {t["plan_name"] for t in summary["targets"]}
        self.assertEqual(names, {"0823_永远到点"})


class FreshManualCandidateTests(unittest.TestCase):
    """手动建计划收养判定（2026-09-03 用户定：矩阵人工建计划，系统接管生命周期）。

    收养条件：已暂停 + 零消耗 + 系统从未见过 + 无待执行恢复票 + 非白名单 + 非核验无效。
    """

    @staticmethod
    def _plan(pid, name, status="已暂停", spent=0):
        from types import SimpleNamespace
        return SimpleNamespace(plan_id=pid, plan_name=name, status=status, spent=spent)

    def _filter(self, plans, **kw):
        args = {"department": "业务1部-1", "seen_ids": set(),
                "pending_keys": set(), "stale_ids": set()}
        args.update(kw)
        return plan_power._fresh_manual_candidates(plans, **args)

    def test_paused_zero_spend_unseen_is_candidate(self):
        plans = [self._plan("m1", "矩阵手工计划")]
        got = self._filter(plans)
        self.assertEqual([p.plan_id for p in got], ["m1"])

    def test_running_or_spent_or_seen_excluded(self):
        plans = [
            self._plan("run", "在投不碰", status="投放中"),
            self._plan("spent", "有消耗被停不碰", spent=12.5),
            self._plan("seen", "系统见过"),
        ]
        got = self._filter(plans, seen_ids={"seen"})
        self.assertEqual(got, [])

    def test_pending_ticket_and_stale_excluded(self):
        plans = [self._plan("t1", "有恢复票"), self._plan("s1", "已核验无效")]
        got = self._filter(plans, pending_keys={"t1"}, stale_ids={"s1"})
        self.assertEqual(got, [])

    def test_whitelist_excluded(self):
        orig = plan_power.is_manual_whitelisted
        plan_power.is_manual_whitelisted = lambda **kw: kw.get("plan_id") == "wl1"
        self.addCleanup(lambda: setattr(plan_power, "is_manual_whitelisted", orig))
        plans = [self._plan("wl1", "人工白名单"), self._plan("ok1", "普通")]
        got = self._filter(plans)
        self.assertEqual([p.plan_id for p in got], ["ok1"])

    def test_unreadable_spend_and_blank_id_excluded(self):
        plans = [
            self._plan("bad", "消耗读不出", spent="--"),
            self._plan("", "没ID"),
        ]
        self.assertEqual(self._filter(plans), [])

    def test_seen_but_still_new_plan_shape_is_adopted(self):
        """2026-09-04：预约新计划曾被 23:00 误标老池,「见过」不再挡收养——
        但见过的必须仍是新计划形态(ROI 4.5)。"""
        from types import SimpleNamespace
        plans = [
            SimpleNamespace(plan_id="seen45", plan_name="0905_预约_矩阵",
                            status="已暂停", spent=0, roi_target=4.5),
        ]
        got = self._filter(plans, seen_ids={"seen45"})
        self.assertEqual([p.plan_id for p in got], ["seen45"])

    def test_seen_with_old_pool_roi_excluded(self):
        from types import SimpleNamespace
        plans = [
            SimpleNamespace(plan_id="seen103", plan_name="老池计划",
                            status="已暂停", spent=0, roi_target=10.3),
        ]
        self.assertEqual(self._filter(plans, seen_ids={"seen103"}), [])


class FutureScheduledPlanTests(unittest.TestCase):
    """预约新计划（名称日期在明天及以后、零消耗）：23:00 终分类不降日常。"""

    def _plan(self, name, **kw):
        from types import SimpleNamespace
        base = dict(plan_id="f1", status="已暂停", spent=0.0,
                    roi=None, orders=0, roi_target=4.5, budget=300)
        base.update(kw)
        return SimpleNamespace(plan_name=name, **base)

    def test_tomorrow_prefix_zero_spend_stays_new_plan_at_final(self):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%m%d")
        plan = self._plan(f"{tomorrow}_牛仔骨_矩阵")
        entry = plan_power._lifecycle_entry("业务1部-1", plan, final_classification=True)
        self.assertEqual(entry["category"], "new_plan")
        self.assertTrue(entry["future_scheduled"])
        self.assertFalse(entry["is_today_new"])
        self.assertIn("预约新计划", entry["reason"])

    def test_today_prefix_zero_spend_not_affected(self):
        """白板①既有规则不变：当天新计划 23:00 零数据走迁移段降日常,
        分类层保持 new_plan(由 close_old_pool 迁移段改写,不进预约分支)。"""
        today = datetime.date.today().strftime("%m%d")
        plan = self._plan(f"{today}_达人A_赵")
        entry = plan_power._lifecycle_entry("业务1部", plan, final_classification=True)
        self.assertNotIn("future_scheduled", entry)

    def test_future_plan_with_spend_classifies_normally(self):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%m%d")
        plan = self._plan(f"{tomorrow}_达人B_赵", spent=50.0, roi=12.0,
                          orders=15, status="投放中")
        entry = plan_power._lifecycle_entry("业务1部", plan, final_classification=True)
        self.assertEqual(entry["category"], "key_plan")


class PriorKeyLadderTests(unittest.TestCase):
    """阶梯降级（2026-09-04 用户定）：昨日重点破线先掉潜力，不直接掉老池。"""

    def _plan(self, **kw):
        from types import SimpleNamespace
        base = dict(plan_name="0815-小果子-郑", plan_id="k1", status="投放中",
                    spent=47.0, roi=8.87, orders=3, roi_target=9.0, budget=300)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_yesterday_key_falls_to_potential_not_old_pool(self):
        entry = plan_power._lifecycle_entry(
            "业务1部", self._plan(), final_classification=False,
            prior_category="key_plan")
        self.assertEqual(entry["category"], "potential_plan")
        self.assertEqual(entry["roi_target"], 7.3)
        self.assertEqual(entry["daily_budget"], 300)
        self.assertIn("阶梯", entry["reason"])

    def test_yesterday_key_ladder_also_applies_at_2300_final(self):
        entry = plan_power._lifecycle_entry(
            "业务1部", self._plan(), final_classification=True,
            prior_category="key_plan")
        self.assertEqual(entry["category"], "potential_plan")

    def test_yesterday_potential_falls_to_old_pool_normally(self):
        entry = plan_power._lifecycle_entry(
            "业务1部", self._plan(), final_classification=False,
            prior_category="potential_plan")
        self.assertEqual(entry["category"], "old_pool")

    def test_yesterday_key_with_passing_data_classifies_by_data(self):
        entry = plan_power._lifecycle_entry(
            "业务1部", self._plan(roi=11.5, orders=20), final_classification=True,
            prior_category="key_plan")
        self.assertEqual(entry["category"], "key_plan")

    def test_no_prior_falls_to_old_pool(self):
        entry = plan_power._lifecycle_entry(
            "业务1部", self._plan(), final_classification=False)
        self.assertEqual(entry["category"], "old_pool")


class YesterdayFinalCategoriesTests(unittest.TestCase):
    def test_reads_only_2300_slot(self):
        rows = [
            {"plan_id": "a", "category": "key_plan", "scan_slot": "23:00"},
            {"plan_id": "b", "category": "potential_plan", "scan_slot": "17:00"},
            {"plan_id": "c", "category": "old_pool", "scan_slot": "23:00"},
        ]
        from unittest.mock import patch
        with patch("webui.db.list_plan_daily_labels", return_value=rows):
            got = plan_power._yesterday_final_categories()
        self.assertEqual(got, {"a": "key_plan", "c": "old_pool"})


class KeyLowRoiGateTests(unittest.TestCase):
    """重点计划盘中降级闸（2026-09-03 用户定，白板③）：破线降潜力 7.3/300。"""

    @staticmethod
    def _report(*entries):
        return {"plans": {"业务1部": list(entries)}}

    def test_breaks_below_threshold(self):
        report = self._report(
            {"plan_id": "k1", "plan_name": "破线重点", "category": "key_plan",
             "status": "投放中", "spent": 500.0, "roi": 7.9},
            {"plan_id": "k2", "plan_name": "达标重点", "category": "key_plan",
             "status": "投放中", "spent": 800.0, "roi": 9.1},
        )
        by_dept, errors = plan_power._key_low_roi_targets(report, 8.5)
        self.assertEqual(errors, [])
        self.assertEqual([i["plan_id"] for i in by_dept["业务1部"]], ["k1"])
        self.assertIn("7.9<8.5", by_dept["业务1部"][0]["circuit_reason"])

    def test_skips_non_key_paused_and_no_evidence(self):
        report = self._report(
            {"plan_id": "p", "plan_name": "潜力不归我管", "category": "potential_plan",
             "status": "投放中", "spent": 100.0, "roi": 1.0},
            {"plan_id": "paused", "plan_name": "已停重点", "category": "key_plan",
             "status": "已暂停", "spent": 100.0, "roi": 1.0},
            {"plan_id": "zero", "plan_name": "零消耗无数据", "category": "key_plan",
             "status": "投放中", "spent": 0.0, "roi": None},
        )
        by_dept, _ = plan_power._key_low_roi_targets(report, 8.5)
        self.assertEqual(by_dept, {})

    def test_spent_without_roi_counts_as_breach(self):
        report = self._report(
            {"plan_id": "k", "plan_name": "有耗无ROI", "category": "key_plan",
             "status": "投放中", "spent": 210.0, "roi": None},
        )
        by_dept, _ = plan_power._key_low_roi_targets(report, 6.0)
        self.assertEqual([i["plan_id"] for i in by_dept["业务1部"]], ["k"])
        self.assertIn("无数据", by_dept["业务1部"][0]["circuit_reason"])

    def test_scan_error_surfaced(self):
        report = {"plans": {"业务2部": [{"error": "timeout"}]}}
        _, errors = plan_power._key_low_roi_targets(report, 8.5)
        self.assertEqual(errors, [{"department": "业务2部", "error": "timeout"}])


class PotentialParamAlignTests(unittest.TestCase):
    """潜力计划盘中参数对齐（2026-09-03 用户定：标了潜力就要是 7.3/300）。"""

    @staticmethod
    def _report(*entries):
        return {"plans": {"业务1部": list(entries)}}

    def test_drifted_potential_listed(self):
        report = self._report(
            {"plan_id": "m", "plan_name": "手动改成10.3的潜力", "category": "potential_plan",
             "status": "投放中", "current_roi_target": 10.3, "budget": 300},
            {"plan_id": "b", "plan_name": "预算漂", "category": "potential_plan",
             "status": "投放中", "current_roi_target": 7.3, "budget": 900},
        )
        got = plan_power._potential_param_align_targets(report, exclude_ids=set())
        self.assertEqual({t["plan_id"] for t in got}, {"m", "b"})

    def test_aligned_paused_demoted_unreadable_excluded(self):
        report = self._report(
            {"plan_id": "ok", "plan_name": "达标", "category": "potential_plan",
             "status": "投放中", "current_roi_target": 7.3, "budget": 300},
            {"plan_id": "off", "plan_name": "已停", "category": "potential_plan",
             "status": "已暂停", "current_roi_target": 10.3, "budget": 300},
            {"plan_id": "k", "plan_name": "重点", "category": "key_plan",
             "status": "投放中", "current_roi_target": 10.3, "budget": 1000},
            {"plan_id": "dem", "plan_name": "刚被降日常的", "category": "potential_plan",
             "status": "投放中", "current_roi_target": 10.3, "budget": 300},
            {"plan_id": "nr", "plan_name": "读不出", "category": "potential_plan",
             "status": "投放中", "current_roi_target": None, "budget": None},
        )
        got = plan_power._potential_param_align_targets(report, exclude_ids={"dem"})
        self.assertEqual(got, [])


class PausedOrdersReportTests(unittest.TestCase):
    """有出单的被暂停计划纳入播报（2026-09-03 用户定，含手动暂停）。"""

    def test_paused_with_orders_included_current_slot(self):
        from 巨量千川.daily_report import aggregate
        report = {
            "业务1部": {"plans": [
                {"plan_id": "run", "plan_name": "在投", "status": "投放中",
                 "spent": 100.0, "orders": 2, "roi": 5.0},
                {"plan_id": "mp", "plan_name": "手动暂停有单", "status": "已暂停",
                 "spent": 77.9, "orders": 3, "roi": 5.35},
                {"plan_id": "mz", "plan_name": "手动暂停零单", "status": "已暂停",
                 "spent": 50.0, "orders": 0, "roi": None},
            ], "total_plans": 3},
        }
        deps, _ = aggregate(report, include_started_today=False, scan_slot="17:00")
        ids = {p["plan_id"] for cat in deps["业务1部"]["categories"].values() for p in cat}
        self.assertIn("run", ids)
        self.assertIn("mp", ids)      # 有出单：播
        self.assertNotIn("mz", ids)   # 零单已暂停：不播
        self.assertEqual(deps["业务1部"]["orders"], 5)


class MatrixDeptMergeTests(unittest.TestCase):
    """矩阵号（业务1部-1）日报展示并入业务1部（2026-09-03 用户定「放到业务一部里」）。"""

    @staticmethod
    def _report():
        return {
            "业务1部": {"plans": [{"plan_id": "a", "plan_name": "主账号计划",
                                   "status": "投放中", "spent": 100.0, "orders": 2,
                                   "roi": 5.0}],
                        "total_plans": 10, "fresh_scan_total": 8},
            "业务1部-1": {"plans": [{"plan_id": "m1", "plan_name": "牛仔骨矩阵号",
                                     "status": "投放中", "spent": 50.0, "orders": 1,
                                     "roi": 4.0}],
                          "total_plans": 2, "fresh_scan_total": 2},
        }

    def test_merge_sums_counters_and_folds_plans(self):
        from 巨量千川.daily_report import _merge_matrix_department, aggregate
        merged = _merge_matrix_department(self._report())
        self.assertNotIn("业务1部-1", merged)
        self.assertEqual(len(merged["业务1部"]["plans"]), 2)
        self.assertEqual(merged["业务1部"]["total_plans"], 12)
        self.assertEqual(merged["业务1部"]["fresh_scan_total"], 10)
        deps, totals = aggregate(merged)
        self.assertEqual(set(deps), {"业务1部"})
        self.assertEqual(deps["业务1部"]["spent"], 150.0)
        self.assertEqual(deps["业务1部"]["orders"], 3)
        self.assertEqual(totals["spent"], 150.0)

    def test_child_error_not_merged(self):
        from 巨量千川.daily_report import _merge_matrix_department
        report = {"业务1部": {"plans": []}, "业务1部-1": {"error": "boom"}}
        merged = _merge_matrix_department(report)
        self.assertIn("业务1部-1", merged)
        self.assertEqual(merged["业务1部"]["plans"], [])


class BudgetDriftTests(unittest.TestCase):
    """预算归位（2026-09-02 用户定）：重点 1000、其余 300，偏离才动。"""

    @staticmethod
    def _report(*entries):
        return {"plans": {"业务1部": list(entries)}}

    def test_drift_detected_per_label(self):
        report = self._report(
            {"plan_id": "k", "plan_name": "重点", "category": "key_plan", "budget": 1300},
            {"plan_id": "p", "plan_name": "潜力", "category": "potential_plan", "budget": 1100},
            {"plan_id": "o", "plan_name": "日常", "category": "old_pool", "budget": 500},
            {"plan_id": "n", "plan_name": "新", "category": "new_plan", "budget": 850},
        )
        targets = plan_power.budget_drift_targets(report)
        got = {t["plan_id"]: (t["current_budget"], t["label_budget"]) for t in targets}
        self.assertEqual(got, {"k": (1300.0, 1000.0), "p": (1100.0, 300.0),
                               "o": (500.0, 300.0), "n": (850.0, 300.0)})
        self.assertTrue(all(t["department"] == "业务1部" for t in targets))

    def test_no_drift_or_unreadable_or_exempt_skipped(self):
        report = self._report(
            {"plan_id": "ok", "plan_name": "达标", "category": "key_plan", "budget": 1000},
            {"plan_id": "near", "plan_name": "零头", "category": "old_pool", "budget": 300.4},
            {"plan_id": "none", "plan_name": "读不出", "category": "old_pool", "budget": None},
            {"plan_id": "wl", "plan_name": "人工", "category": "manual_review", "budget": 5000},
            {"plan_id": "inv", "plan_name": "无效", "category": "invalid_plan", "budget": 5000},
            {"plan_id": "err", "plan_name": "错", "category": "old_pool", "budget": 5000,
             "error": "scan"},
        )
        self.assertEqual(plan_power.budget_drift_targets(report), [])

    def test_normalize_dry_run_lists_without_browser(self):
        report = self._report(
            {"plan_id": "k", "plan_name": "重点", "category": "key_plan", "budget": 1300},
        )
        import asyncio
        summary = asyncio.run(plan_power.normalize_budgets_by_label(report, dry_run=True))
        self.assertEqual(len(summary["targets"]), 1)
        self.assertEqual(summary["changed"], [])
        self.assertEqual(summary["failed"], [])

    def test_roi_drift_detected_per_label(self):
        """2026-09-03 用户定：ROI 目标也按标签归位（旧潜力 7.3 的日常应回 10.3）。"""
        report = self._report(
            {"plan_id": "o7", "plan_name": "旧潜力", "category": "old_pool",
             "budget": 300, "roi_target": 7.3},
            {"plan_id": "o86", "plan_name": "古早", "category": "old_pool",
             "budget": 300, "roi_target": 8.6},
            {"plan_id": "ok", "plan_name": "达标", "category": "old_pool",
             "budget": 300, "roi_target": 10.3},
            {"plan_id": "nor", "plan_name": "读不出", "category": "old_pool",
             "budget": 300, "roi_target": None},
            {"plan_id": "p", "plan_name": "潜力", "category": "potential_plan",
             "budget": 300, "roi_target": 7.3},
        )
        targets = plan_power.budget_drift_targets(report)
        got = {t["plan_id"]: t for t in targets}
        self.assertEqual(set(got), {"o7", "o86"})
        self.assertEqual(got["o7"]["label_roi"], 10.3)
        self.assertTrue(got["o7"]["roi_drift"])
        self.assertFalse(got["o7"]["budget_drift"])  # 预算没漂，执行时不写预算

    def test_roi_and_budget_drift_both_fixed_flags(self):
        report = self._report(
            {"plan_id": "both", "plan_name": "双漂", "category": "key_plan",
             "budget": 1300, "roi_target": 7.3},
        )
        (t,) = plan_power.budget_drift_targets(report)
        self.assertTrue(t["budget_drift"])
        self.assertTrue(t["roi_drift"])
        self.assertEqual(t["label_budget"], 1000.0)
        self.assertEqual(t["label_roi"], 10.3)


class NewVideoPickTests(unittest.TestCase):
    """72 小时素材硬闸：只可选择发布 ≤72h 的视频。"""

    def _pick(self, videos, now=1_000_000.0):
        from 巨量千川.plan_create import _pick_new_video_index
        return _pick_new_video_index(videos, now=now)

    def _v(self, age_hours):
        return {"createTime": str(int(1_000_000 - age_hours * 3600))}

    def test_first_video_within_72h_picks_index_zero(self):
        self.assertEqual(self._pick([self._v(1), self._v(100)]), 0)

    def test_skips_old_videos_and_picks_first_fresh_one(self):
        self.assertEqual(self._pick([self._v(96), self._v(73), self._v(2)]), 2)

    def test_exactly_72h_qualifies(self):
        self.assertEqual(self._pick([self._v(72)]), 0)

    def test_all_old_is_blocked(self):
        self.assertIsNone(self._pick([self._v(73), self._v(200)]))

    def test_missing_or_invalid_create_time_skipped(self):
        self.assertEqual(self._pick([{}, {"createTime": "abc"}, self._v(3)]), 2)

    def test_fresh_indexes_exclude_videos_over_72h(self):
        from 巨量千川.plan_create import _fresh_video_indexes
        self.assertEqual(_fresh_video_indexes([self._v(73), self._v(72), self._v(1)], now=1_000_000), [1, 2])

    def test_empty_list_returns_none(self):
        self.assertIsNone(self._pick([]))
        self.assertIsNone(self._pick(None))


class ReconcileTests(unittest.TestCase):
    """对账引擎：期望状态矩阵 + 差异计算（先报告后执行，2026-08-24 起）。"""

    def _at(self, hhmm):
        return datetime.datetime(2026, 8, 24, int(hhmm[:2]), int(hhmm[3:]))

    def _entry(self, category, status="投放中", **kw):
        base = {
            "department": "业务2部", "plan_id": "p1", "plan_name": "测试计划",
            "status": status, "category": category, "spent": 10.0,
            "roi": 5.0, "orders": 1,
        }
        base.update(kw)
        return base

    def test_desired_matrix_windows(self):
        from 巨量千川.reconcile import desired_state
        cases = [
            # (category, 时刻, 应投放)
            ("key_plan", "03:00", True),
            ("key_plan", "23:30", True),
            ("new_plan", "07:59", False),
            ("new_plan", "08:00", True),
            ("new_plan", "22:59", True),
            ("new_plan", "23:00", False),
            ("potential_plan", "08:00", True),
            ("potential_plan", "23:00", False),
            ("old_pool", "11:59", False),
            ("old_pool", "12:00", True),
            ("old_pool", "17:09", True),
            ("old_pool", "17:10", False),
        ]
        for category, hhmm, want in cases:
            got = desired_state(self._entry(category), self._at(hhmm))
            self.assertEqual(got["should_run"], want, f"{category}@{hhmm}")

    def test_desired_params_by_category(self):
        from 巨量千川.reconcile import desired_state
        noon = self._at("12:30")
        self.assertEqual(desired_state(self._entry("key_plan"), noon)["budget"], 1000)
        self.assertEqual(desired_state(self._entry("potential_plan"), noon)["roi_target"], 7.3)
        self.assertEqual(desired_state(self._entry("new_plan"), noon)["roi_target"], 4.5)
        self.assertEqual(desired_state(self._entry("old_pool"), noon)["roi_target"], 10.3)

    def test_manual_review_never_touched(self):
        from 巨量千川.reconcile import compute_diffs
        entries = [self._entry("manual_review", status="投放中"),
                   self._entry("manual_review", status="已暂停")]
        self.assertEqual(compute_diffs(entries, self._at("12:30")), [])

    def test_stale_zero_order_never_resumed(self):
        from 巨量千川.reconcile import compute_diffs
        entries = [self._entry("old_pool", status="已暂停")]
        diffs = compute_diffs(entries, self._at("12:30"), stale_zero_order_ids={"p1"})
        self.assertEqual(diffs, [])
        # 不在僵尸名单里就该恢复
        diffs = compute_diffs(entries, self._at("12:30"), stale_zero_order_ids=set())
        self.assertEqual([d["diff"] for d in diffs], ["enable"])

    def test_diff_pause_outside_window(self):
        from 巨量千川.reconcile import compute_diffs
        diffs = compute_diffs([self._entry("old_pool")], self._at("20:00"))
        self.assertEqual([d["diff"] for d in diffs], ["pause"])

    def test_no_diff_when_state_matches(self):
        from 巨量千川.reconcile import compute_diffs
        entries = [self._entry("old_pool", roi_target=10.3, budget=300)]
        self.assertEqual(compute_diffs(entries, self._at("12:30")), [])

    def test_param_drift_reported_only_when_roi_target_known(self):
        from 巨量千川.reconcile import compute_diffs
        drifted = [self._entry("old_pool", roi_target=4.5, budget=300)]
        diffs = compute_diffs(drifted, self._at("12:30"))
        self.assertEqual([d["diff"] for d in diffs], ["rewrite"])
        self.assertEqual(diffs[0]["roi_target_want"], 10.3)
        # 老数据 roi_target 未入库 → 不报漂移，避免误报
        legacy = [self._entry("old_pool", roi_target=None, budget=300)]
        self.assertEqual(compute_diffs(legacy, self._at("12:30")), [])

    def test_append_report_writes_jsonl(self):
        from 巨量千川 import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            reconcile._LOG_DIR = Path(tmp)
            report = {
                "counts": {"old_pool": 1},
                "plans": {"业务2部": [self._entry("old_pool", status="已暂停")]},
            }
            body = reconcile.append_reconcile_report(report, source_action="resume-old-pool",
                                                     now=self._at("12:30"))
            self.assertEqual(body["diff_count"], 1)
            lines = (Path(tmp) / "reconcile_20260824.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["mode"], "report-only")

    def test_sync_lifecycle_catalog_roundtrip(self):
        import shutil

        from webui import db
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        original = db.DB_FILE
        db.DB_FILE = Path(tmp) / "webui.db"
        try:
            db.init_db()
            report = {"业务2部": [self._entry("old_pool", roi_target=10.3)]}
            self.assertEqual(db.sync_lifecycle_catalog(report), 1)
            rows = db.list_plan_catalog()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "old_pool")
            self.assertEqual(rows[0]["roi_target"], 10.3)
            # 再同步一次分类变更：应更新而不是插新行
            report["业务2部"][0]["category"] = "potential_plan"
            db.sync_lifecycle_catalog(report)
            rows = db.list_plan_catalog()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "potential_plan")
        finally:
            db.DB_FILE = original


if __name__ == "__main__":
    unittest.main()
