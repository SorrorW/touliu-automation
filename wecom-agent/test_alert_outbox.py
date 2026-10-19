# -*- coding: utf-8 -*-
"""alert_outbox 告警发件箱（2026-09-14 告警一致性）单测。"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 便携 runtime 的 ._pth 完全接管 sys.path，脚本目录不在其中，须手动加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alert_outbox
import budget_watch
import plan_focus_report

# 单测不污染生产日志
import logging as _logging
for _mod in (alert_outbox, budget_watch, plan_focus_report):
    _mod.log.handlers = [
        h for h in _mod.log.handlers
        if not isinstance(h, _logging.FileHandler)]

NOW = datetime.datetime(2026, 9, 14, 13, 30, 0)


class OutboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "outbox.json"

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return alert_outbox._load(self.state_file)["entries"]


class EnqueueTests(OutboxBase):
    def test_enqueue_new_entry(self):
        self.assertTrue(alert_outbox.enqueue(
            "监控播报", "内容A", "http://hook", now=NOW, state_file=self.state_file))
        items = self.entries()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "监控播报")
        self.assertEqual(items[0]["attempts"], 0)

    def test_dedupe_same_content_within_2h(self):
        alert_outbox.enqueue("监控播报", "内容A", "http://hook",
                             now=NOW, state_file=self.state_file)
        later = NOW + datetime.timedelta(minutes=30)
        self.assertFalse(alert_outbox.enqueue(
            "监控播报", "内容A", "http://hook", now=later, state_file=self.state_file))
        self.assertEqual(len(self.entries()), 1)

    def test_same_content_after_2h_re_enqueues(self):
        alert_outbox.enqueue("监控播报", "内容A", "http://hook",
                             now=NOW, state_file=self.state_file)
        later = NOW + datetime.timedelta(hours=3)
        self.assertTrue(alert_outbox.enqueue(
            "监控播报", "内容A", "http://hook", now=later, state_file=self.state_file))
        self.assertEqual(len(self.entries()), 2)

    def test_cap_drops_oldest(self):
        base = NOW
        for i in range(alert_outbox.MAX_ENTRIES):
            alert_outbox.enqueue("监控播报", f"内容{i}", "http://hook",
                                 now=base + datetime.timedelta(minutes=i * 3),
                                 state_file=self.state_file)
        alert_outbox.enqueue("预算监控", "新内容", "http://hook",
                             now=base + datetime.timedelta(hours=9),
                             state_file=self.state_file)
        items = self.entries()
        self.assertEqual(len(items), alert_outbox.MAX_ENTRIES)
        self.assertEqual(items[-1]["kind"], "预算监控")
        self.assertNotIn("内容0", [i["content"] for i in items])

    def test_empty_content_or_webhook_ignored(self):
        self.assertFalse(alert_outbox.enqueue("x", "", "http://hook",
                                              now=NOW, state_file=self.state_file))
        self.assertFalse(alert_outbox.enqueue("x", "内容", "",
                                              now=NOW, state_file=self.state_file))
        self.assertEqual(len(self.entries()), 0)


class FlushTests(OutboxBase):
    def test_flush_success_clears_box(self):
        alert_outbox.enqueue("监控播报", "内容A", "http://hook",
                             now=NOW, state_file=self.state_file)
        with mock.patch.object(alert_outbox, "_post", return_value=(True, "")) as p:
            sent, left = alert_outbox.flush(
                now=NOW + datetime.timedelta(minutes=1), state_file=self.state_file)
        self.assertEqual((sent, left), (1, 0))
        self.assertEqual(self.entries(), [])
        # 10 分钟内补发不加前缀
        self.assertEqual(p.call_args[0][1], "内容A")

    def test_flush_old_entry_gets_resend_prefix(self):
        alert_outbox.enqueue("预算监控", "内容A", "http://hook",
                             now=NOW, state_file=self.state_file)
        with mock.patch.object(alert_outbox, "_post", return_value=(True, "")) as p:
            alert_outbox.flush(now=NOW + datetime.timedelta(hours=2),
                               state_file=self.state_file)
        sent_content = p.call_args[0][1]
        self.assertIn("补发", sent_content)
        self.assertIn("预算监控", sent_content)
        self.assertTrue(sent_content.endswith("内容A"))

    def test_transport_error_stops_flush_and_keeps_all(self):
        alert_outbox.enqueue("监控播报", "A", "http://hook",
                             now=NOW, state_file=self.state_file)
        alert_outbox.enqueue("监控播报", "B", "http://hook2",
                             now=NOW + datetime.timedelta(minutes=3),
                             state_file=self.state_file)
        err = "<urlopen error [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。>"
        with mock.patch.object(alert_outbox, "_post", return_value=(False, err)) as p:
            sent, left = alert_outbox.flush(
                now=NOW + datetime.timedelta(minutes=5), state_file=self.state_file)
        self.assertEqual((sent, left), (0, 2))
        self.assertEqual(p.call_count, 1)  # 通道断=第一条就停，不浪费第二次
        self.assertEqual(self.entries()[0]["attempts"], 1)
        self.assertEqual(self.entries()[1]["attempts"], 0)

    def test_platform_reject_retries_then_drops_at_max(self):
        alert_outbox.enqueue("监控播报", "坏内容", "http://hook",
                             now=NOW, state_file=self.state_file)
        with mock.patch.object(alert_outbox, "_post",
                               return_value=(False, "errcode=40058: invalid content")):
            for i in range(alert_outbox.MAX_ATTEMPTS):
                sent, left = alert_outbox.flush(
                    now=NOW + datetime.timedelta(hours=3 + i),
                    state_file=self.state_file)
                self.assertEqual(sent, 0)
        self.assertEqual(self.entries(), [])  # 到顶丢弃，不再无限重试

    def test_platform_reject_does_not_block_later_entries(self):
        alert_outbox.enqueue("监控播报", "坏内容", "http://hook",
                             now=NOW, state_file=self.state_file)
        alert_outbox.enqueue("监控播报", "好内容", "http://hook2",
                             now=NOW + datetime.timedelta(minutes=3),
                             state_file=self.state_file)

        def fake_post(url, content):
            return (False, "errcode=40058: bad") if url == "http://hook" else (True, "")
        with mock.patch.object(alert_outbox, "_post", side_effect=fake_post):
            sent, left = alert_outbox.flush(
                now=NOW + datetime.timedelta(minutes=5), state_file=self.state_file)
        self.assertEqual((sent, left), (1, 1))
        self.assertEqual(self.entries()[0]["content"], "坏内容")

    def test_empty_box_noop(self):
        sent, left = alert_outbox.flush(now=NOW, state_file=self.state_file)
        self.assertEqual((sent, left), (0, 0))


class WiringTests(unittest.TestCase):
    """两个监控推送入口：失败入箱、成功顺手 flush。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Path(self.tmp.name) / "outbox.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_budget_watch_push_failure_enqueues(self):
        with mock.patch("daily_report_push.load_monitor_webhook",
                        return_value="http://hook"), \
             mock.patch("daily_report_push.push_missing",
                        return_value=([], ["汇总"])), \
             mock.patch.object(alert_outbox, "_STATE_FILE", self.box), \
             mock.patch.object(alert_outbox, "flush") as fl:
            ok = budget_watch._push_alert("预算告警内容")
        self.assertFalse(ok)
        fl.assert_not_called()
        items = alert_outbox._load(self.box)["entries"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "预算监控")

    def test_budget_watch_push_success_flushes(self):
        with mock.patch("daily_report_push.load_monitor_webhook",
                        return_value="http://hook"), \
             mock.patch("daily_report_push.push_missing",
                        return_value=(["汇总"], [])), \
             mock.patch.object(alert_outbox, "flush") as fl:
            ok = budget_watch._push_alert("预算告警内容")
        self.assertTrue(ok)
        fl.assert_called_once()

    def test_plan_focus_push_failure_enqueues(self):
        with mock.patch("daily_report_push.load_monitor_webhook",
                        return_value="http://hook"), \
             mock.patch("daily_report_push.push_missing",
                        return_value=([], ["汇总"])), \
             mock.patch.object(alert_outbox, "_STATE_FILE", self.box):
            ok = plan_focus_report._push("监控播报内容")
        self.assertFalse(ok)
        items = alert_outbox._load(self.box)["entries"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "监控播报")

    def test_push_without_webhook_never_enqueues(self):
        with mock.patch("daily_report_push.load_monitor_webhook",
                        return_value=""), \
             mock.patch.object(alert_outbox, "_STATE_FILE", self.box):
            ok = budget_watch._push_alert("内容")
        self.assertFalse(ok)
        self.assertEqual(alert_outbox._load(self.box)["entries"], [])


if __name__ == "__main__":
    unittest.main()
