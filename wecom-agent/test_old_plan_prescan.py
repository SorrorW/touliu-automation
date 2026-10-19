# -*- coding: utf-8 -*-
"""旧计划预检前置测试（2026-09-04 用户定「先查老计划」）。

r033Yl 案例：达人已有在投老计划，但授权模块排在旧计划检查之前，
授权被平台新规拒 → 记录标死，永远走不到「查老计划」。
修复：模块0 只读预检，有老计划则绕过授权整段。
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
import local_tools


def _sweep_stdout(matched: int, chase: str | None = None) -> str:
    import json
    payload = {"matched": matched, "plans": [], "deleted": [], "material_added": []}
    if chase:
        payload["chase_plan_id"] = chase
    return "扫描日志行\n" + json.dumps(payload, ensure_ascii=False) + "\n"


class OldPlanPrescanTests(unittest.TestCase):
    def _run_flow(self, sweep_results):
        """sweep_results: list of (rc, stdout) 依次给预检/模块3。"""
        calls = {"exec": [], "sweep": 0, "auth_check": 0}
        sweep_iter = iter(sweep_results)

        def fake_run(cmd, timeout=None):
            joined = " ".join(str(c) for c in cmd)
            if "plan_sweep" in joined:
                calls["sweep"] += 1
                rc, out = next(sweep_iter)
                return rc, out, ""
            raise AssertionError(f"unexpected _run: {joined[:120]}")

        def fake_exec(name, cmd, step_cb, **kw):
            calls["exec"].append(name)
            return True, "两项授权均已生效"

        def fake_auth_check(creator, department, **kw):
            calls["auth_check"] += 1
            return "ok", "两项授权均已生效"

        with (
            patch.object(local_tools, "_run", fake_run),
            patch.object(local_tools, "_exec_step", fake_exec),
            patch.object(local_tools, "_auth_check", fake_auth_check),
            patch.object(local_tools, "_sync_poll_plan_catalog", lambda *a, **k: None),
            patch.object(local_tools, "delivery_window_open", lambda *a, **k: False),
        ):
            status, msg = local_tools.process_douyin_task(
                "139428248", code="88159941637", product="3735391724588106065",
                seller="张丽伟", department="业务2部",
            )
        return status, msg, calls

    def test_existing_old_plan_bypasses_authorization(self):
        """有老计划：授权申请/确认整段跳过，不再撞平台授权新规。"""
        # 预检 matched=1 带 chase；模块3 再搜（白天只读）同样 chase → wait 等夜间
        status, msg, calls = self._run_flow([
            (0, _sweep_stdout(1, chase="1875233905096713")),
            (0, _sweep_stdout(1, chase="1875233905096713")),
        ])
        self.assertEqual(calls["exec"], [], f"授权模块不应执行: {calls['exec']}")
        self.assertEqual(calls["auth_check"], 0)
        self.assertEqual(calls["sweep"], 2)  # 预检 + 模块3 各一次
        self.assertEqual(status, "wait")  # 白天已有旧计划 → 等夜间窗口加素材
        self.assertIn("已有旧计划", msg)

    def test_no_old_plan_runs_authorization_as_before(self):
        """没有老计划：照旧走授权申请 → 授权确认 → 旧计划处理。"""
        status, msg, calls = self._run_flow([
            (0, _sweep_stdout(0)),  # 预检 0 条
            (0, _sweep_stdout(0)),  # 模块3 再搜仍 0 条
        ])
        self.assertEqual(calls["exec"], ["授权申请"])
        self.assertEqual(calls["auth_check"], 1)
        self.assertEqual(calls["sweep"], 2)
        # 白天窗口关：建计划等待
        self.assertEqual(status, "wait")
        self.assertIn("等夜间窗口建计划", msg)

    def test_prescan_failure_does_not_block_authorization(self):
        """预检本身失败（浏览器故障/无 JSON）：不阻塞，照旧走授权流程。"""
        status, msg, calls = self._run_flow([
            (1, "browser exploded"),       # 预检失败
            (0, _sweep_stdout(0)),          # 模块3
        ])
        self.assertEqual(calls["exec"], ["授权申请"])
        self.assertEqual(calls["sweep"], 2)

    def test_skipped_old_plan_module_also_skips_prescan(self):
        """断点续跑（旧计划处理已完成）时不做预检，避免重复搜索。"""
        with (
            patch.object(local_tools, "_run", side_effect=AssertionError("不应再搜索")),
            patch.object(local_tools, "_exec_step", return_value=(True, "ok")) as exec_mock,
            patch.object(local_tools, "_auth_check", return_value=("ok", "ok")),
            patch.object(local_tools, "_sync_poll_plan_catalog", lambda *a, **k: None),
            patch.object(local_tools, "delivery_window_open", lambda *a, **k: False),
        ):
            status, msg = local_tools.process_douyin_task(
                "139428248", code="88159941637", product="3735",
                seller="张丽伟", department="业务2部",
                skip_steps=("授权申请", "授权确认", "旧计划处理"),
            )
        self.assertEqual(status, "wait")  # 建计划等夜间窗口
        self.assertEqual(exec_mock.call_count, 0)  # 授权/建计划全跳过


if __name__ == "__main__":
    unittest.main()
