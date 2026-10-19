# -*- coding: utf-8 -*-
"""表格驱动白名单测试（2026-09-07）。

覆盖：_rows_to_cache 纯函数解析（启用/停用墓碑/状态/脏数据/空行/冲突）、
同步器容错（API 异常沿用旧缓存/无配置跳过/0 行合法）、manual_whitelist 合并判定
（无缓存退化/kill 移出/deny 前置否决/表格新增命中/坏缓存降级/部门过滤）。

运行：cd wecom-agent && ../投流/runtime/python.exe -X utf8 test_whitelist_sync.py
（不要 python -m unittest，便携 ._pth 找不到模块。）
模块级把文件路径重定向到 tmp 目录——09-01 测试污染生产队列的教训。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "投流"))

_TMP = tempfile.mkdtemp(prefix="wl_test_")
_TEST_BASE = os.path.join(_TMP, "base.json")     # 底账 JSON
_TEST_CACHE = os.path.join(_TMP, "cache.json")   # 表格缓存

import whitelist_sync  # noqa: E402
whitelist_sync.CACHE_FILE = _TEST_CACHE

import 巨量千川.manual_whitelist as mw  # noqa: E402
mw.WHITELIST_FILE = mw.Path(_TEST_BASE)
mw.SHEET_CACHE_FILE = mw.Path(_TEST_CACHE)

# 剥掉 logging FileHandler（whitelist_sync 只加 StreamHandler，无文件处理器，保险起见）
for h in list(whitelist_sync.log.handlers):
    if isinstance(h, logging.FileHandler):
        whitelist_sync.log.removeHandler(h)

BASE_CREATORS = {
    "creators": [
        {"name": "味蕾食光", "aliases": ["味蕾"], "department": "业务1部",
         "plan_ids": ["1873760988199226"], "reason": "人工白名单"},
        {"name": "大梦吴恙", "aliases": ["大梦"], "department": "业务3部",
         "plan_ids": ["1872757139280011", "1865146941503971"], "reason": "保护"},
        {"name": "钱钱在上海", "aliases": ["钱钱"], "department": "",
         "plan_ids": [], "reason": "保护"},
    ]
}


def _write(path: str, obj: dict) -> None:
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def _rec(values: dict, rid: str = "rX") -> dict:
    """构造企微原始记录：values 值为 [{"type":"text","text":...}] 结构。"""
    return {"record_id": rid,
            "values": {k: [{"type": "text", "text": v}] for k, v in values.items()}}


class RowsToCacheTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(_TEST_CACHE):
            os.remove(_TEST_CACHE)

    def test_1_enable_row_fields(self):
        rows = [_rec({"计划ID": "111", "计划名": "0906_vivi酱#美食_林", "达人名": "vivi酱#美食",
                      "部门": "业务2部", "备注": "用户指定", "状态": "启用"})]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        self.assertEqual(len(c["enable_entries"]), 1)
        e = c["enable_entries"][0]
        self.assertEqual(e["plan_ids"], ["111"])
        self.assertEqual(e["department"], "业务2部")
        self.assertEqual(e["reason"], "用户指定")
        self.assertEqual(e["name"], "vivi酱#美食")
        self.assertIn("0906_vivi酱#美食_林", e["aliases"])

    def test_2_disable_row_tombstones(self):
        # 有计划ID+达人名 → 双墓碑；只有计划ID → 只 deny；只有达人名 → 只 kill
        rows = [
            _rec({"计划ID": "222", "达人名": "味蕾食光", "状态": "停用"}),
            _rec({"计划ID": "333", "状态": "停用"}),
            _rec({"达人名": "钱钱在上海", "状态": "停用"}),
        ]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        self.assertIn("222", c["deny_plan_ids"])
        self.assertIn("333", c["deny_plan_ids"])
        self.assertIn("味蕾食光", c["kill_names"])
        self.assertIn("钱钱在上海", c["kill_names"])
        self.assertEqual(c["enable_entries"], [])

    def test_3_status_parsing(self):
        # 空/启用 → 启用；停用 → 停用
        rows = [_rec({"计划ID": "1", "状态": ""}),
                _rec({"计划ID": "2", "状态": "启用"}),
                _rec({"计划ID": "3", "状态": "停用"})]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        enabled_ids = [e["plan_ids"][0] for e in c["enable_entries"]]
        self.assertIn("1", enabled_ids)
        self.assertIn("2", enabled_ids)
        self.assertIn("3", c["deny_plan_ids"])

    def test_4_plan_id_dirty_data(self):
        rows = [_rec({"计划ID": "187xxx, ", "状态": "启用"}),
                _rec({"计划ID": "id1，id2、id3", "状态": "启用"})]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        ids = [pid for e in c["enable_entries"] for pid in e["plan_ids"]]
        self.assertIn("187xxx", ids)
        for x in ("id1", "id2", "id3"):
            self.assertIn(x, ids)

    def test_5_blank_row_dropped(self):
        rows = [_rec({"状态": "启用"}),  # 三标识全空
                _rec({"计划ID": "9", "状态": "启用"})]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        self.assertEqual(c["dropped"], 1)
        self.assertEqual(len(c["enable_entries"]), 1)

    def test_6_conflict_disable_wins(self):
        rows = [_rec({"计划ID": "555", "达人名": "某达人", "状态": "启用"}),
                _rec({"计划ID": "555", "达人名": "某达人", "状态": "停用"})]
        c = whitelist_sync._rows_to_cache(rows, "sid")
        self.assertIn("555", c["deny_plan_ids"])   # 停用优先
        self.assertIn("某达人", c["kill_names"])
        self.assertEqual(c["enable_entries"], [])   # 启用行被停用剔除
        self.assertTrue(c["conflicts"])


class SyncerTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(_TEST_CACHE):
            os.remove(_TEST_CACHE)

    def test_7_api_error_keeps_old_cache(self):
        _write(_TEST_CACHE, {"synced_at": "old", "enable_entries": [{"name": "x"}]})
        before = open(_TEST_CACHE, encoding="utf-8").read()

        class BadClient:
            cfg = {"whitelist_sheet_id": "sid", "docid": "d", "sheet_id": "s"}

            def get_all_records(self):
                raise RuntimeError("60020 ip 不在可信名单")

        r = whitelist_sync.sync_whitelist_from_sheet(BadClient())
        self.assertFalse(r["ok"])
        self.assertEqual(open(_TEST_CACHE, encoding="utf-8").read(), before)  # 字节不变

    def test_8_no_config_skips(self):
        class NoCfg:
            cfg = {"docid": "d", "sheet_id": "s"}  # 无 whitelist_sheet_id
        r = whitelist_sync.sync_whitelist_from_sheet(NoCfg())
        self.assertFalse(r["ok"])
        self.assertIn("skipped", r)
        self.assertFalse(os.path.exists(_TEST_CACHE))

    def test_9_success_writes_and_empty_is_legal(self):
        class OkClient:
            cfg = {"whitelist_sheet_id": "sid", "docid": "d", "sheet_id": "s"}

            def get_all_records(self):
                return [_rec({"计划ID": "777", "达人名": "测试", "状态": "启用"})]
        r = whitelist_sync.sync_whitelist_from_sheet(OkClient())
        self.assertTrue(r["ok"])
        self.assertEqual(r["enable"], 1)
        cache = json.load(open(_TEST_CACHE, encoding="utf-8"))
        self.assertIn("synced_at", cache)
        # 0 行合法
        class EmptyClient(OkClient):
            def get_all_records(self):
                return []
        r2 = whitelist_sync.sync_whitelist_from_sheet(EmptyClient())
        self.assertTrue(r2["ok"])
        cache2 = json.load(open(_TEST_CACHE, encoding="utf-8"))
        self.assertEqual(cache2["enable_entries"], [])


class MergeJudgementTests(unittest.TestCase):
    def setUp(self):
        _write(_TEST_BASE, BASE_CREATORS)
        if os.path.exists(_TEST_CACHE):
            os.remove(_TEST_CACHE)

    def test_10_no_cache_matches_today(self):
        entries = mw._load_entries()
        self.assertEqual(len(entries), 3)
        self.assertTrue(mw.is_manual_whitelisted(department="业务1部", plan_id="1873760988199226"))

    def test_11_kill_name_removes_json_entry(self):
        _write(_TEST_CACHE, {"enable_entries": [], "deny_plan_ids": [], "kill_names": ["味蕾食光"]})
        self.assertFalse(mw.is_manual_whitelisted(creator="味蕾食光", department="业务1部"))
        self.assertFalse(mw.is_manual_whitelisted(department="业务1部", plan_name="0817-味蕾食光-郑"))
        # 其它条目不受影响
        self.assertTrue(mw.is_manual_whitelisted(creator="钱钱在上海"))

    def test_12_deny_plan_id_absolute(self):
        # plan_id 进 deny：即使 plan_name 含别名子串也返回 None（防别名绕过删除）
        _write(_TEST_CACHE, {"enable_entries": [], "deny_plan_ids": ["1873760988199226"], "kill_names": []})
        self.assertFalse(mw.is_manual_whitelisted(department="业务1部",
                                                  plan_id="1873760988199226",
                                                  plan_name="0817-味蕾食光-郑"))

    def test_13_sheet_new_enable_hits(self):
        _write(_TEST_CACHE, {"enable_entries": [
            {"name": "新达人", "aliases": ["新达人"], "department": "业务1部",
             "plan_ids": ["999888"], "reason": "表格白名单", "record_id": "r1"}],
            "deny_plan_ids": [], "kill_names": []})
        self.assertTrue(mw.is_manual_whitelisted(department="业务1部", plan_id="999888"))
        self.assertEqual(mw.manual_label(department="业务1部", plan_id="999888"), "新达人")

    def test_14_corrupt_cache_degrades(self):
        open(_TEST_CACHE, "w", encoding="utf-8").write("{bad json")
        entries = mw._load_entries()  # 不抛异常，退化为纯底账
        self.assertEqual(len(entries), 3)

    def test_15_department_filter_no_regression(self):
        _write(_TEST_CACHE, {"enable_entries": [
            {"name": "限定部门", "aliases": [], "department": "业务2部",
             "plan_ids": ["555000"], "reason": "x", "record_id": "r2"}],
            "deny_plan_ids": [], "kill_names": []})
        # 部门不符 → 不命中
        self.assertFalse(mw.is_manual_whitelisted(department="业务1部", plan_id="555000"))
        self.assertTrue(mw.is_manual_whitelisted(department="业务2部", plan_id="555000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
