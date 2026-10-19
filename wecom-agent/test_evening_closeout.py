import logging
import unittest
from unittest.mock import patch

import evening_closeout

# 单测不污染生产日志（模块级 FileHandler 会写 logs/evening_closeout_YYYYMMDD.log，
# 2026-09-03 曾因测试写入假「收口完成」行造成误判）
evening_closeout.log.handlers = [
    h for h in evening_closeout.log.handlers
    if not isinstance(h, logging.FileHandler)
]


class EveningCloseoutTests(unittest.TestCase):
    def test_report_must_finish_before_any_pause(self):
        calls = []

        def fake_step(name, command, **kwargs):
            calls.append((name, command))
            return 0, "", ""

        with patch.object(evening_closeout, "run_step", side_effect=fake_step):
            self.assertEqual(evening_closeout.run_closeout(), 0)

        self.assertEqual(
            [name for name, _ in calls],
            ["17:00日报（四群推送）", "17:00老计划池批量暂停",
             "17:00新计划ROI<6暂停",
             "17:00潜力降级闸(订单<4或ROI<7.3转日常并立即暂停)",
             "17:00重点降级闸(0-17ROI<8.5降潜力7.3/300继续投放)"],
        )
        self.assertIn("daily_report_push.py", calls[0][1][1])
        # 2026-09-03 用户定「先停后调」：老池批量停紧跟日报，先止血再调参数
        self.assertEqual(calls[1][1][-1], "--force")
        self.assertIn("pause-old-pool", calls[1][1])
        self.assertEqual(calls[2][1][-1], "--force")
        self.assertEqual(calls[3][1][-1], "--force")
        # 重点降级闸：阈值 8.5、归 17:00 槽位
        self.assertIn("demote-key-low-roi", calls[4][1])
        self.assertIn("8.5", calls[4][1])
        self.assertEqual(calls[4][1][-1], "--force")

    def test_report_failure_blocks_both_pause_steps(self):
        with patch.object(evening_closeout, "run_step", return_value=(1, "", "push failed")) as step:
            self.assertNotEqual(evening_closeout.run_closeout(), 0)
        self.assertEqual(step.call_count, 1)

    def test_dry_run_never_pauses(self):
        with patch.object(evening_closeout, "run_step", return_value=(0, "", "")) as step:
            self.assertEqual(evening_closeout.run_closeout(dry_run=True), 0)
        self.assertEqual(step.call_count, 1)


if __name__ == "__main__":
    unittest.main()
