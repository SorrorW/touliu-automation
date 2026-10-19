# -*- coding: utf-8 -*-
"""盘中预算安全闸（2026-09-15 费用安全件）+ 回补存活校验/15天报告重跑闸 单测。"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..")))

from 巨量千川 import plan_power
from 巨量千川 import manual_whitelist

# 单测不污染生产日志（plan_power 有模块级 FileHandler；
# 09-15 三行「拉起定期重跑」WARNING 就是没剥时的测试写入）
import logging as _logging
plan_power.log.handlers = [h for h in plan_power.log.handlers
                           if not isinstance(h, _logging.FileHandler)]


def _plan(pid, name, budget, status="投放中"):
    return SimpleNamespace(plan_id=pid, plan_name=name, budget=budget, status=status)


class ClampTargetTests(unittest.TestCase):
    """99999 拦截 + 超档位下压（只下压不上抬；升档补足合法；读不出不动）。"""

    def test_unlimited_intercept_uses_label_budget(self):
        plans = {"业务1部": [_plan("k1", "重点A", 99999), _plan("o1", "日常B", 99999)]}
        board = {"k1": "key_plan", "o1": "old_pool"}
        got = {t["plan_id"]: t
               for t in plan_power.budget_safety_clamp_targets(plans, board, set())}
        self.assertEqual(got["k1"]["label_budget"], 1000.0)
        self.assertEqual(got["o1"]["label_budget"], 300.0)
        self.assertTrue(all(t["reason"] == "unlimited_intercept" for t in got.values()))

    def test_unlimited_without_label_falls_back_300(self):
        plans = {"业务1部": [_plan("x", "无标签", 99999)]}
        (t,) = plan_power.budget_safety_clamp_targets(plans, {}, set())
        self.assertEqual(t["label_budget"], 300.0)
        self.assertEqual(t["reason"], "unlimited_intercept")

    def test_over_cap_clamps_down_only(self):
        plans = {"业务1部": [
            _plan("over", "超档", 500),     # old_pool 档 300 → 下压
            _plan("under", "低于档", 200),  # 低于档位 → 不抬（抬是升档/夜归位的活）
            _plan("ok", "达标", 300),
            _plan("near", "零头", 300.9),   # 1 元容差内不动（与夜归位同口径）
        ]}
        board = {p: "old_pool" for p in ("over", "under", "ok", "near")}
        got = plan_power.budget_safety_clamp_targets(plans, board, set())
        self.assertEqual([t["plan_id"] for t in got], ["over"])
        self.assertEqual(got[0]["reason"], "over_cap")
        self.assertEqual(got[0]["label_budget"], 300.0)

    def test_key_plan_cap_is_1000(self):
        plans = {"业务1部": [_plan("k", "重点", 1300)]}
        (t,) = plan_power.budget_safety_clamp_targets(plans, {"k": "key_plan"}, set())
        self.assertEqual(t["label_budget"], 1000.0)

    def test_promoted_new_plan_skipped_only_while_new(self):
        plans = {"业务1部": [_plan("n1", "升档补足", 450)]}
        # 当天升档（仍 new_plan 标签）：补足 300+ 是合法超额，跳过
        self.assertEqual(
            plan_power.budget_safety_clamp_targets(plans, {"n1": "new_plan"}, {"n1"}), [])
        # 隔日落 old_pool 后不再豁免（23:00 已改写 300，再超=真漂移）
        got = plan_power.budget_safety_clamp_targets(plans, {"n1": "old_pool"}, {"n1"})
        self.assertEqual(len(got), 1)

    def test_unreadable_unknown_label_and_manual_review_skipped(self):
        plans = {"业务1部": [
            _plan("none", "读不出", None),
            _plan("zero", "零", 0),
            _plan("nolabel", "无标签超档", 500),   # 无标签且非 99999 → 不动
            _plan("mr", "人工审查", 5000),          # manual_review 不在档位表
        ]}
        got = plan_power.budget_safety_clamp_targets(plans, {"mr": "manual_review"}, set())
        self.assertEqual(got, [])

    def test_dict_rows_and_paused_accepted(self):
        plans = {"业务2部": [
            {"plan_id": "d1", "plan_name": "字典行", "budget": 99999, "status": "已暂停"},
        ]}
        (t,) = plan_power.budget_safety_clamp_targets(plans, {"d1": "potential_plan"}, set())
        self.assertEqual(t["label_budget"], 300.0)
        self.assertEqual(t["department"], "业务2部")
        self.assertEqual(t["status"], "已暂停")

    def test_ignored_plans_skipped(self):
        """ignored 名单（平台系统暂停+用户不处理）安全闸不碰——已暂停不烧钱，
        每 30 分钟白试编辑页只会撞平台阻断（09-15 铭妈 1500→300 两连败实证）。"""
        plans = {"业务2部": [
            _plan("ming", "铭妈美食记", 1500, status="已暂停"),
            _plan("ok", "正常超档", 1500),
        ]}
        board = {"ming": "old_pool", "ok": "old_pool"}
        got = plan_power.budget_safety_clamp_targets(plans, board, set(),
                                                     skip_ids={"ming"})
        self.assertEqual([t["plan_id"] for t in got], ["ok"])

    def test_sub_100_delta_skipped_platform_min_step(self):
        """平台「预算修改幅度不能小于 100 元」：差额 <100 的写回静默落空
        （09-15 我家厨房 308→300 三次「成功」纹丝不动实证），不纳入目标。"""
        plans = {"业务3部": [
            _plan("tiny", "我家厨房", 308, status="已暂停"),   # 差 8 元 → 跳过
            _plan("big", "真漂移", 400),                        # 差 100 → 压
        ]}
        board = {"tiny": "old_pool", "big": "old_pool"}
        got = plan_power.budget_safety_clamp_targets(plans, board, set())
        self.assertEqual([t["plan_id"] for t in got], ["big"])


class WhitelistLabelMatchTests(unittest.TestCase):
    def test_manual_plan_name_matches_when_separator_differs(self):
        """表格里的 0815_小果子-郑 必须保护实际 0815-小果子-郑。"""
        entry = {"name": "0815_小果子-郑", "aliases": [], "department": "业务1部",
                 "plan_ids": ["187359708618944"]}  # 人工表误录 ID 不应失去名称保护
        with mock.patch.object(manual_whitelist, "_load_entries", return_value=[entry]):
            self.assertIsNotNone(manual_whitelist.match_manual_whitelist(
                department="业务1部", plan_id="1873597086418944", plan_name="0815-小果子-郑"))


class ClampExecutorTests(unittest.TestCase):
    def test_dry_run_lists_without_browser(self):
        targets = [{"plan_id": "x", "plan_name": "n", "department": "业务1部",
                    "current_budget": 99999.0, "label_budget": 300.0,
                    "reason": "unlimited_intercept"}]
        summary = asyncio.run(plan_power.clamp_budget_safety(targets, dry_run=True))
        self.assertEqual(len(summary["targets"]), 1)
        self.assertEqual(summary["changed"], [])
        self.assertEqual(summary["failed"], [])

    def test_empty_targets_no_browser(self):
        summary = asyncio.run(plan_power.clamp_budget_safety([]))
        self.assertEqual(summary["changed"], [])


class GhostFilterTests(unittest.TestCase):
    """回补存活校验（幽灵票病根）：平台已删（catalog 无记录或 last_seen 冻结
    超 2 天）不再回补；catalog 读失败（None）不校验=放行。"""

    def test_ghost_detection(self):
        now = time.time()
        seen = {"fresh": now - 3600, "stale": now - 3 * 86400}
        self.assertFalse(plan_power._recent_order_ghost("fresh", seen, now_ts=now))
        self.assertTrue(plan_power._recent_order_ghost("stale", seen, now_ts=now))
        self.assertTrue(plan_power._recent_order_ghost("missing", seen, now_ts=now))

    def test_catalog_read_failure_passes_all(self):
        self.assertFalse(plan_power._recent_order_ghost("anything", None))


class OrderReportRefreshGateTests(unittest.TestCase):
    """15 天核验报告超龄（>6 天）由 close 尾巴 detached 拉起重跑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.report = Path(self._tmp.name) / "old_pool_15day_readonly_20990101.json"
        self.report.write_text("{}", encoding="utf-8")

    def _with_age(self, days):
        ts = time.time() - days * 86400
        os.utime(self.report, (ts, ts))

    def test_fresh_report_not_spawned(self):
        self._with_age(1)
        with mock.patch.object(plan_power, "_latest_valid_order_report",
                               return_value=self.report), \
             mock.patch.object(plan_power.subprocess, "Popen") as popen:
            result = plan_power._maybe_spawn_order_report_refresh()
        self.assertTrue(result.startswith("fresh:"))
        popen.assert_not_called()

    def test_stale_report_spawned_with_script_path(self):
        self._with_age(10)
        with mock.patch.object(plan_power, "_latest_valid_order_report",
                               return_value=self.report), \
             mock.patch.object(plan_power, "_LOG_DIR", Path(self._tmp.name)), \
             mock.patch.object(plan_power.subprocess, "Popen") as popen:
            result = plan_power._maybe_spawn_order_report_refresh()
        self.assertTrue(result.startswith("spawned:"))
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        self.assertTrue(cmd[-1].endswith("inspect_old_pool_15day.py"))

    def test_unreadable_report(self):
        with mock.patch.object(plan_power, "_latest_valid_order_report",
                               return_value=Path(self._tmp.name) / "nope.json"):
            self.assertEqual(plan_power._maybe_spawn_order_report_refresh(),
                             "report-unreadable")

    def test_age_by_as_of_not_mtime(self):
        """09-14 幽灵票清理编辑了报告文件把 mtime 洗新（16 天陈报告变 1.4 天），
        age-gate 因此失灵——年龄必须按内容 as_of 算。"""
        self.report.write_text(
            '{"as_of": "2026-08-31", "has_orders": [], "verified_zero_orders": []}',
            encoding="utf-8")
        self._with_age(1)  # mtime 很新，内容很旧
        with mock.patch.object(plan_power, "_latest_valid_order_report",
                               return_value=self.report), \
             mock.patch.object(plan_power, "_LOG_DIR", Path(self._tmp.name)), \
             mock.patch.object(plan_power.subprocess, "Popen") as popen:
            result = plan_power._maybe_spawn_order_report_refresh()
        self.assertTrue(result.startswith("spawned:"), result)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
