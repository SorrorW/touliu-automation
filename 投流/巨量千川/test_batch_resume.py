from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from 巨量千川 import plan_power as power, plan_metrics as metrics, session_prepare

power.log.handlers = [h for h in power.log.handlers if not isinstance(h, logging.FileHandler)]


class RestoreLockTests(unittest.TestCase):
    def test_action_lock_contention_times_out_and_closes_handle(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "action.lock"
            with patch.object(power, "_ACTION_LOCK_FILE", path):
                fd, held = power._acquire_action_lock(0)
                try:
                    with self.assertRaises(TimeoutError):
                        power._acquire_action_lock(0)
                finally:
                    power._release_action_lock(fd, held)
                fd, held = power._acquire_action_lock(0)
                power._release_action_lock(fd, held)
                path.unlink()  # Windows also verifies no leaked handles.

    def test_busy_clear_does_not_remove_another_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "busy.json"
            with patch.object(power, "_BUSY_FILE", path):
                path.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
                power._busy_clear()
                self.assertTrue(path.exists())
                power._busy_mark("test")
                power._busy_clear()
                self.assertFalse(path.exists())

    def test_wait_for_poll_never_forces_through(self):
        with patch.object(power, "_poll_round_running", return_value=True):
            with self.assertRaises(TimeoutError):
                power._wait_poll_round_idle(0)

    def test_wait_for_report_never_forces_through(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.json"
            path.write_text(json.dumps({"ts": power.time.time()}), encoding="utf-8")
            with patch.object(power, "_REPORT_BUSY_FILE", path):
                with self.assertRaises(TimeoutError):
                    power._wait_report_idle(0)

    def test_main_lock_timeout_does_not_claim_busy_or_run_action(self):
        with patch("sys.argv", ["plan_power", "resume-old-pool", "--force"]), \
             patch.object(power, "_wait_report_idle"), \
             patch.object(power, "_wait_poll_round_idle"), \
             patch.object(power, "_acquire_action_lock", side_effect=TimeoutError("busy")), \
             patch.object(power, "_busy_mark") as mark, \
             patch.object(power, "resume_old_pool", new_callable=AsyncMock) as action:
            self.assertEqual(power.main(), 2)
            mark.assert_not_called()
            action.assert_not_called()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_system_paused_and_scan_error_are_not_searched(self):
        details = ["target_not_found_after_full_scan", "target_not_found_scan_incomplete",
                   "unexpected_status:系统暂停", "batch_scan_error:navigation"]
        items = [{"plan_id": str(n)} for n in range(100)]
        batch = {"failed": [{**item, "detail": details[n % 4]} for n, item in enumerate(items)]}
        with patch.object(power, "_enable_one", new_callable=AsyncMock) as enable:
            singles, failed = await power._retry_resume_failures(None, items, batch)
        enable.assert_not_called()
        self.assertFalse(singles)
        self.assertEqual(len(failed), 100)
        self.assertEqual({v["plan_id"] for v in failed}, {v["plan_id"] for v in items})

    async def test_only_three_actual_failures_retry_using_history(self):
        items = [{"plan_id": str(n)} for n in range(10)]
        batch = {"failed": [{**item, "detail": "failed:checkbox"} for item in items]}
        with patch.object(power, "_enable_one", new_callable=AsyncMock, return_value="enabled") as enable:
            singles, failed = await power._retry_resume_failures(None, items, batch)
        self.assertEqual(enable.await_count, 3)
        self.assertEqual(len(singles), 3)
        self.assertEqual(len(failed), 7)
        for call in enable.await_args_list:
            self.assertEqual(call.args[1]["time_range"], "近180天含今天")

    async def test_successes_never_retried_and_timeout_remains_pending(self):
        items = [{"plan_id": "done"}, {"plan_id": "retry"}]
        batch = {"already": [items[0]], "failed": [{**items[1], "detail": "failed:checkbox"}]}
        with patch.object(power, "_enable_one", new_callable=AsyncMock, side_effect=TimeoutError):
            singles, failed = await power._retry_resume_failures(None, items, batch)
        self.assertEqual(singles, [(items[0], "already")])
        self.assertEqual(failed[0]["plan_id"], "retry")
        self.assertIn("unconfirmed", failed[0]["detail"])


class BatchRangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_includes_today_and_today_reports_keep_original_semantics(self):
        today = datetime.date(2026, 10, 21)
        dates = metrics._target_dates("近180天含今天", today)
        self.assertEqual(dates[1], "2026-10-21")
        self.assertEqual((today - datetime.date.fromisoformat(dates[0])).days, 179)
        self.assertTrue(metrics._range_matches("近180天含今天", dates, today))
        self.assertFalse(metrics._range_matches("近180天含今天", (dates[0], "2026-10-20"), today))
        self.assertEqual(metrics._target_dates("近180天", today)[1], "2026-10-20")
        self.assertEqual(metrics._target_dates("今天", today), ("2026-10-21", "2026-10-21"))

    async def _batch(self, statuses, turn_on=True, total=None, next_page=False, categories=None):
        events = []
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        rows = [MagicMock() for _ in statuses]
        for row in rows:
            row.is_visible = AsyncMock(return_value=True)
            row.locator.return_value.count = AsyncMock(return_value=23)
        table = MagicMock()
        table.count = AsyncMock(return_value=len(rows))
        table.nth.side_effect = lambda n: rows[n]
        page.locator.return_value = table
        async def parse(row, labels=None):
            events.append("read")
            n = rows.index(row)
            return SimpleNamespace(plan_id=str(n), plan_name=str(n), status=statuses[n], pause_category=categories[n] if categories else None)
        async def set_range(*args):
            events.append("range")
        with patch.object(power, "_open_overall", new_callable=AsyncMock, return_value=page), \
             patch.object(power, "_set_time_range", new_callable=AsyncMock, side_effect=set_range) as dates, \
             patch.object(power, "_wait_plan_table_ready", new_callable=AsyncMock, return_value=True), \
             patch.object(power, "_dismiss_promotion_modal", new_callable=AsyncMock, return_value=False), \
             patch.object(power, "_parse_plan_row", side_effect=parse), \
             patch.object(power, "_column_labels", new_callable=AsyncMock, return_value=["计划", "ROI", "消耗"]) as header, \
             patch.object(power, "_read_pagination_total", new_callable=AsyncMock, return_value=total), \
             patch.object(power, "_goto_next_page", new_callable=AsyncMock, return_value=next_page) as goto, \
             patch.object(power, "is_manual_whitelisted", return_value=False), \
             patch.object(power, "_batch_click", new_callable=AsyncMock) as click:
            result = await power._batch_power_targets(page, "业务2部", [{"plan_id": "0"}, {"plan_id": "missing"}], turn_on=turn_on)
        self.header_reads = header.await_count
        return result, events, dates, goto, click

    async def test_range_precedes_rows_and_system_pause_not_clicked(self):
        result, events, dates, _, click = await self._batch(["系统暂停"], total=1)
        self.assertEqual(events[:2], ["range", "read"])
        dates.assert_awaited_once()
        click.assert_not_awaited()
        self.assertIn("unexpected_status", result["failed"][0]["detail"])
        self.assertEqual(result["failed"][1]["detail"], "target_not_found_after_full_scan")

    async def test_system_pause_reason_below_first_status_line_is_skipped(self):
        result, _, _, _, click = await self._batch(["已暂停"], categories=["system_pause"], total=1)
        click.assert_not_awaited()
        self.assertEqual(result["failed"][0]["detail"], "unexpected_status:系统暂停")

    async def test_batch_reads_header_once_for_multiple_rows(self):
        await self._batch(["投放中", "投放中", "投放中"], total=3)
        self.assertEqual(self.header_reads, 1)

    async def test_repeated_page_is_incomplete_and_does_not_loop_fifty_times(self):
        result, _, _, goto, _ = await self._batch(["投放中"], total=10, next_page=True)
        self.assertEqual(goto.await_count, 1)
        self.assertEqual(len(result["already"]), 1)
        self.assertEqual(result["failed"][0]["detail"], "target_not_found_scan_incomplete")

    async def test_pause_scan_does_not_change_to_historical_spending(self):
        _, _, dates, _, _ = await self._batch(["已暂停"], turn_on=False, total=1)
        dates.assert_not_awaited()

    async def test_whitelisted_target_never_opens_browser(self):
        with patch.object(power, "is_manual_whitelisted", return_value=True), \
             patch.object(power, "_open_overall", new_callable=AsyncMock) as opened:
            result = await power._batch_enable_targets(None, "业务2部", [{"plan_id": "protected"}])
        opened.assert_not_awaited()
        self.assertEqual(result["failed"][0]["detail"], "manual_whitelist")


class ReconcileScanTests(unittest.TestCase):
    def _run(self, scans):
        target = {"department": "业务2部", "plan_id": "p1"}
        with patch.object(power, "monitor", new_callable=AsyncMock, side_effect=scans) as monitor, \
             patch.object(power, "_ignored_resume_keys", return_value=set()), \
             patch.object(power, "_load_old_pool_resume_queue", return_value={"pending": []}), \
             patch.object(power, "_verified_stale_zero_order_ids", return_value=set()), \
             patch("巨量千川.commission_gate.load_commission_blocked_ids", return_value=set()), \
             patch("巨量千川.reconcile.append_reconcile_report", return_value={"diff_count": 0, "diffs": []}) as append, \
             patch.object(power.time, "sleep"):
            power._post_action_reconcile("resume-old-pool", action_result={"enabled": [target]})
        return monitor, append

    def test_matching_readback_syncs_once(self):
        scan = {"plans": {"业务2部": [{"plan_id": "p1", "status": "投放中"}]}}
        monitor, append = self._run([scan])
        self.assertEqual(monitor.await_count, 1)
        append.assert_called_once()
        self.assertIs(append.call_args.args[0], scan)

    def test_mismatch_gets_another_scan_before_success(self):
        scans = [{"plans": {"业务2部": [{"plan_id": "p1", "status": status}]}}
                 for status in ("已暂停", "投放中")]
        monitor, append = self._run(scans)
        self.assertEqual(monitor.await_count, 2)
        self.assertIs(append.call_args.args[0], scans[-1])

    def test_wrong_department_is_not_accepted_as_success(self):
        scan = {"plans": {"业务1部": [{"plan_id": "p1", "status": "投放中"}]}}
        monitor, _ = self._run([scan, scan, scan])
        self.assertEqual(monitor.await_count, 3)


class PlanTabAfterDateTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_date_still_recovers_plan_tab(self):
        page = MagicMock()
        page.url = "https://example.test/overall-prom?dateRange=2026-10-21%2C2026-10-21"
        with patch.object(metrics, "_target_dates", return_value=("2026-10-21", "2026-10-21")), \
             patch.object(metrics, "_read_range_inputs", new_callable=AsyncMock, return_value=("2026-10-21", "2026-10-21")), \
             patch.object(metrics, "force_plans_tab", new_callable=AsyncMock) as force:
            await metrics._set_time_range(page, "今天")
        force.assert_awaited_once_with(page)

    async def test_date_navigation_recovers_tab_before_table_read(self):
        page = MagicMock()
        page.url = "https://example.test/overall-prom?dateRange=2026-10-20%2C2026-10-20"
        page.wait_for_timeout = AsyncMock()
        page.locator.return_value.first.inner_text = AsyncMock(return_value="old table")
        events = []
        async def goto(url, **kwargs):
            events.append("navigate")
            page.url = url
        async def force(p):
            events.append("plan_tab")
        async def settle(p, old):
            events.append("read_table")
        page.goto = AsyncMock(side_effect=goto)
        dates = ("2026-10-21", "2026-10-21")
        with patch.object(metrics, "_target_dates", return_value=dates), \
             patch.object(metrics, "_read_range_inputs", new_callable=AsyncMock, return_value=dates), \
             patch.object(metrics, "force_plans_tab", new_callable=AsyncMock, side_effect=force), \
             patch.object(metrics, "_wait_table_settled", new_callable=AsyncMock, side_effect=settle):
            await metrics._set_time_range(page, "今天")
        self.assertEqual(events, ["navigate", "plan_tab", "read_table"])


class PersistedPlanTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_tab_is_clicked_before_reading_plan_input(self):
        events = []
        page = MagicMock()
        tab = MagicMock()
        tab.count = AsyncMock(return_value=1)
        tab.is_visible = AsyncMock(return_value=True)
        tab.get_attribute = AsyncMock(return_value="false")
        async def click(**kwargs):
            events.append("click_product_self")
        tab.click = AsyncMock(side_effect=click)
        page.get_by_role.return_value = tab
        page.wait_for_timeout = AsyncMock()
        async def count():
            events.append("read_plan_input")
            return 1
        page.locator.return_value.count = AsyncMock(side_effect=count)
        await session_prepare.force_plans_tab(page)
        self.assertEqual(events, ["click_product_self", "read_plan_input"])
        page.get_by_role.assert_called_once_with("tab", name="商品自选", exact=True)

    async def test_selected_product_tab_is_not_clicked_again(self):
        page = MagicMock()
        tab = MagicMock()
        tab.count = AsyncMock(return_value=1)
        tab.is_visible = AsyncMock(return_value=True)
        tab.get_attribute = AsyncMock(return_value="true")
        tab.click = AsyncMock()
        page.get_by_role.return_value = tab
        page.locator.return_value.count = AsyncMock(return_value=1)
        await session_prepare.force_plans_tab(page)
        tab.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class PauseReconcileEarlyExitTests(unittest.TestCase):
    def run_scans(self, scans, **changes):
        target = {"department": "业务2部", "plan_id": "p1"}
        action = {"targets": [target], "paused": [target], "already": [], "failed": []}
        action.update(changes)
        with patch.object(power, "monitor", new_callable=AsyncMock, side_effect=scans) as monitor, patch.object(power, "_ignored_resume_keys", return_value=set()), patch.object(power, "_load_old_pool_resume_queue", return_value={"pending": []}), patch.object(power, "_verified_stale_zero_order_ids", return_value=set()), patch("巨量千川.commission_gate.load_commission_blocked_ids", return_value=set()), patch("巨量千川.reconcile.append_reconcile_report", return_value={"diff_count": 0, "diffs": []}) as append, patch.object(power.time, "sleep"):
            power._post_action_reconcile("pause-old-pool", action_result=action)
        return monitor, append
    def scan(self, status="已暂停", **extra):
        return {"plans": {"业务2部": [{"plan_id": "p1", "status": status}]}, "database_synced": True, **extra}
    def test_confirmed_pause_and_database_need_only_one_full_scan(self):
        result = self.scan(); monitor, append = self.run_scans([result])
        self.assertEqual(monitor.await_count, 1);self.assertIs(append.call_args.args[0], result)
    def test_lagging_platform_gets_another_scan(self):
        monitor, _ = self.run_scans([self.scan("投放中"), self.scan()])
        self.assertEqual(monitor.await_count, 2)
    def test_failed_database_sync_keeps_three_attempts(self):
        bad = self.scan(database_synced=False);monitor, _ = self.run_scans([bad]*3)
        self.assertEqual(monitor.await_count, 3)
    def test_wrong_department_cannot_prove_pause(self):
        bad = {"plans": {"业务3部": [{"plan_id": "p1", "status": "已暂停"}]}, "database_synced": True}
        monitor, _ = self.run_scans([bad]*3);self.assertEqual(monitor.await_count, 3)
    def test_any_scan_error_keeps_three_attempts(self):
        bad = self.scan();bad["plans"]["业务3部"] = [{"error": "unreadable"}]
        monitor, _ = self.run_scans([bad]*3);self.assertEqual(monitor.await_count, 3)
    def test_failed_action_target_keeps_three_attempts(self):
        monitor, _ = self.run_scans([self.scan()]*3, failed=[{"plan_id": "p2"}])
        self.assertEqual(monitor.await_count, 3)
    def test_unconfirmed_target_prevents_early_exit(self):
        monitor, _ = self.run_scans([self.scan()]*3, paused=[])
        self.assertEqual(monitor.await_count, 3)
    def test_empty_target_list_finishes_after_one_synced_scan(self):
        monitor, _ = self.run_scans([self.scan()], targets=[], paused=[])
        self.assertEqual(monitor.await_count, 1)
    def test_missing_target_identity_never_proves_completion(self):
        monitor, _ = self.run_scans([self.scan()]*3, targets=[{"department": "业务2部"}])
        self.assertEqual(monitor.await_count, 3)


class MonitorDatabaseSyncFlagTests(unittest.IsolatedAsyncioTestCase):
    async def run_monitor(self, fail=False):
        with patch.object(power, "DEPARTMENTS", []), patch.object(power, "_verified_stale_zero_order_ids", return_value=set()), patch.object(power, "_daytime_preserved_categories", return_value={}), patch.object(power, "_yesterday_final_categories", return_value={}), patch.object(power, "load_state", return_value={}), patch("webui.db.sync_lifecycle_catalog", side_effect=RuntimeError("db locked") if fail else None), patch("webui.db.record_plan_daily_labels") as labels:
            result = await power.monitor(dry_run=True, scan_slot="17:00", final_classification=False)
        return result, labels
    async def test_flag_requires_successful_catalog_and_label_sync(self):
        result, labels = await self.run_monitor()
        self.assertIs(result["database_synced"], True);labels.assert_called_once()
    async def test_catalog_failure_does_not_claim_sync_success(self):
        result, labels = await self.run_monitor(fail=True)
        self.assertIs(result["database_synced"], False);labels.assert_not_called()
