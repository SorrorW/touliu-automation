import logging
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

import noon_report_resume

# 单测不污染生产日志（模块级 FileHandler 会写 logs/noon_report_resume_YYYYMMDD.log）
noon_report_resume.log.handlers = [
    h for h in noon_report_resume.log.handlers
    if not isinstance(h, logging.FileHandler)
]


class NoonReportResumeTests(unittest.TestCase):
    def test_report_runs_before_old_pool_resume(self):
        """2026-09-04 用户定：12点先播报，然后开启老计划；09-07 链尾加佣金闸。"""
        calls = []

        def fake_step(name, command, **kwargs):
            calls.append((name, command))
            return 0, "", ""

        with patch.object(noon_report_resume, "run_step", side_effect=fake_step):
            self.assertEqual(noon_report_resume.run_noon_chain(now_hhmm="12:00"), 0)

        self.assertEqual([name for name, _ in calls],
                         ["12:00午间播报", "老计划池12:00恢复", "佣金闸（>5%存量停投）"])
        self.assertIn("daily_report_push.py", calls[0][1][1])
        self.assertIn("--slot", calls[0][1])
        self.assertIn("noon", calls[0][1])
        self.assertIn("resume-old-pool", calls[1][1])
        self.assertEqual(calls[1][1][-1], "--force")
        self.assertIn("commission-gate", calls[2][1])
        self.assertEqual(calls[2][1][-1], "--force")

    def test_report_failure_still_resumes_old_pool(self):
        """恢复是开通动作不是止损：播报失败不阻断老池恢复与佣金闸。"""
        calls = []

        def fake_step(name, command, **kwargs):
            calls.append(name)
            return (1, "", "push failed") if "播报" in name else (0, "", "")

        with patch.object(noon_report_resume, "run_step", side_effect=fake_step):
            self.assertEqual(noon_report_resume.run_noon_chain(now_hhmm="12:00"), 0)

        self.assertEqual(calls, ["12:00午间播报", "老计划池12:00恢复", "佣金闸（>5%存量停投）"])

    def test_resume_failure_returns_nonzero(self):
        def fake_step(name, command, **kwargs):
            return (0, "", "") if "播报" in name else (1, "", "cdp down")

        with patch.object(noon_report_resume, "run_step", side_effect=fake_step):
            self.assertNotEqual(noon_report_resume.run_noon_chain(now_hhmm="12:00"), 0)

    def test_commission_gate_failure_returns_nonzero(self):
        """佣金闸是末步：失败语义同恢复失败，链返回非 0。"""
        def fake_step(name, command, **kwargs):
            return (1, "", "gate down") if "佣金闸" in name else (0, "", "")

        with patch.object(noon_report_resume, "run_step", side_effect=fake_step):
            self.assertNotEqual(noon_report_resume.run_noon_chain(now_hhmm="12:00"), 0)

    def test_stale_replay_skips_report_but_still_resumes(self):
        """StartWhenAvailable 重放到 12:20 之后：跳过播报，仍跑恢复+佣金闸。"""
        calls = []

        def fake_step(name, command, **kwargs):
            calls.append(name)
            return 0, "", ""

        with patch.object(noon_report_resume, "run_step", side_effect=fake_step):
            self.assertEqual(noon_report_resume.run_noon_chain(now_hhmm="12:35"), 0)

        self.assertEqual(calls, ["老计划池12:00恢复", "佣金闸（>5%存量停投）"])

    def test_dry_run_never_resumes(self):
        with patch.object(noon_report_resume, "run_step", return_value=(0, "", "")) as step:
            self.assertEqual(noon_report_resume.run_noon_chain(dry_run=True, now_hhmm="12:00"), 0)
        self.assertEqual(step.call_count, 1)


if __name__ == "__main__":
    unittest.main()
