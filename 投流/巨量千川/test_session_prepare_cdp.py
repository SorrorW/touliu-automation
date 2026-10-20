# -*- coding: utf-8 -*-
"""_ensure_cdp 重拉冷却（2026-09-14「窗口一直唤醒」连环杀拉事故）回归测试。

不碰真实浏览器：_probe_cdp/_launch_edge/_kill_port_owner 等全部打桩。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from 巨量千川 import session_prepare as sp


class _Base(unittest.TestCase):
    def setUp(self):
        # 默认：无现存 Edge 进程、无端口占用、杀进程动作空转
        self.patches = [
            mock.patch.object(sp, "_kill_port_owner"),
            mock.patch.object(sp, "_edge_process_exists", return_value=False),
            mock.patch.object(sp, "_stop_edge_processes"),
            mock.patch.object(sp, "_launch_edge"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.launch = sp._launch_edge

    def _run(self, probe_side_effect, last_relaunch_ts=0.0, deadline_seconds=5.0):
        with mock.patch.object(sp, "_probe_cdp",
                               side_effect=probe_side_effect), \
             mock.patch.object(sp, "_last_relaunch_ts",
                               return_value=last_relaunch_ts), \
             mock.patch.object(sp, "_mark_relaunch") as mark:
            deadline = time.monotonic() + deadline_seconds
            try:
                result = sp._ensure_cdp(deadline)
                return result, mark, None
            except sp.SessionPrepareError as exc:
                return None, mark, exc


class EnsureCdpTests(_Base):
    def test_healthy_probe_never_relaunches(self):
        (result, mark, exc), = [self._run(["Edg/152"])]
        self.assertEqual(result, ("Edg/152", False))
        self.assertIsNone(exc)
        self.launch.assert_not_called()
        mark.assert_not_called()

    def test_dead_probe_fails_without_restarting_user_browser(self):
        # 用户在专用 Edge 第一页手动操作时，CDP 不通也不准杀进程/重拉：
        # 本轮必须明确失败，等下轮或用户手动恢复浏览器。
        result, mark, exc = self._run([None] * 500, deadline_seconds=0.6)
        self.assertIsNone(result)
        self.assertIsNotNone(exc)
        self.assertIn("不自动重启", str(exc))
        self.launch.assert_not_called()
        mark.assert_not_called()

    def test_cooldown_skips_relaunch_and_fails_fast(self):
        # 2 分钟内刚重拉过：probe 全灭 → 不启动、不杀、快速报错
        recent = time.time() - 10  # 10s 前刚拉过
        result, mark, exc = self._run([None] * 500,
                                      last_relaunch_ts=recent,
                                      deadline_seconds=60.0)
        self.assertIsNone(result)
        self.assertIsNotNone(exc)
        self.assertIn("不自动重启", str(exc))
        self.launch.assert_not_called()
        mark.assert_not_called()

    def test_cooldown_probe_recovery_returns_without_relaunch(self):
        # 冷却期内探活第 3 次恢复 → 直接复用，不重拉
        recent = time.time() - 10
        result, mark, exc = self._run([None, None, "Edg/152"],
                                      last_relaunch_ts=recent,
                                      deadline_seconds=60.0)
        self.assertEqual(result, ("Edg/152", False))
        self.assertIsNone(exc)
        self.launch.assert_not_called()

    def test_dead_probe_after_old_cooldown_still_never_restarts(self):
        # 无论上次重启状态多旧，都不能再把用户浏览器杀掉重拉。
        old = time.time() - (sp.RELAUNCH_COOLDOWN_SECONDS + 10)
        result, mark, exc = self._run([None] * 500,
                                      last_relaunch_ts=old,
                                      deadline_seconds=0.6)
        self.assertIsNone(result)
        self.assertIsNotNone(exc)
        self.assertIn("不自动重启", str(exc))
        self.launch.assert_not_called()
        mark.assert_not_called()

    def test_websocket_failure_never_kills_or_restarts_browser(self):
        class _DeadChromium:
            async def connect_over_cdp(self, *_args, **_kwargs):
                raise RuntimeError("websocket hung")

        class _FakePlaywright:
            chromium = _DeadChromium()

        with mock.patch.object(sp, "_cdp_websocket_url", return_value="ws://dead"), \
             mock.patch.object(sp, "_another_task_busy", return_value=None), \
             mock.patch.object(sp, "_stop_edge_processes") as stop, \
             mock.patch.object(sp, "_kill_port_owner") as kill, \
             mock.patch.object(sp, "_launch_edge") as launch:
            with self.assertRaises(sp.SessionPrepareError) as caught:
                asyncio.run(sp.connect_browser(_FakePlaywright(), retries=1))
        self.assertIn("不自动重启", str(caught.exception))
        stop.assert_not_called()
        kill.assert_not_called()
        launch.assert_not_called()

    def test_tab_limit_never_closes_another_running_automation_page(self):
        class _Page:
            def __init__(self, url):
                self.url = url
                self.closed = False

            async def close(self):
                self.closed = True

        class _Context:
            def __init__(self):
                self.pages = [_Page("https://qianchuan.jinritemai.com/user-first")] + [
                    _Page(f"https://qianchuan.jinritemai.com/automation-{i}")
                    for i in range(sp.MAX_OTHER_TABS)
                ]
                self.new_calls = 0

            async def new_page(self):
                self.new_calls += 1
                return _Page("about:blank")

        context = _Context()
        with self.assertRaises(sp.SessionPrepareError) as caught:
            asyncio.run(sp.open_fresh_page(context))
        self.assertIn("标签上限", str(caught.exception))
        self.assertEqual(context.new_calls, 0)
        self.assertFalse(any(p.closed for p in context.pages))


class RetiredAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_account_rejected_before_browser_launch(self):
        with mock.patch.object(sp, "_ensure_cdp") as launch:
            with self.assertRaises(KeyError):
                await sp.prepare_session("业务1部-1", time.monotonic() + 5)
        launch.assert_not_called()

    async def test_retired_scan_rejected_before_navigation(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        with self.assertRaises(KeyError):
            await sp.ensure_qianchuan_page(page, "业务1部-1")
        page.goto.assert_not_awaited()

    async def test_main_and_three_departments_remain_available(self):
        self.assertEqual(set(sp.ACCOUNT_BY_DEPARTMENT), {"主号", "业务1部", "业务2部", "业务3部"})
        with self.assertRaises(KeyError):
            sp._account_id("鲜会鲜业务一部-1")


if __name__ == "__main__":
    unittest.main()
