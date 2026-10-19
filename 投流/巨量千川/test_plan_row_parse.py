# -*- coding: utf-8 -*-
"""_parse_plan_row 吞列根治（2026-09-01）的单测。

复现 08-31 夜间档事故的畸形行样本（名称吞并 ID 列 / ID 粘连「1商品」 /
行被回收），验证：纯函数 _extract_row + _malformed_reason 正确判畸形，
_parse_plan_row 半渲染时重读自愈、持续畸形时返回 None 而不是脏数据。
"""
from __future__ import annotations

import unittest

from 巨量千川.plan_metrics import (
    _extract_row, _labels_usable, _malformed_reason, _parse_plan_row,
)

# 健康的 18 单元格行（legacy 列索引布局：1=计划 2=状态 3=设置 7=推广方式
# 9=综合营销ROI 13=整体消耗 14=整体成交订单数）
CLEAN_CELLS = [
    "",                                                        # 0 勾选框
    "0827_天妈轻生活_黄\nID：1874463158343303\n1商品 1抖音号 素材12",  # 1 计划
    "投放中",                                                   # 2 状态
    "日预算 300元",                                             # 3 设置
    "", "", "",                                                 # 4-6
    "全域推广",                                                 # 7 推广方式
    "",                                                         # 8
    "6.23",                                                     # 9 综合营销ROI
    "", "", "",                                                 # 10-12
    "335.92",                                                   # 13 整体消耗
    "12",                                                       # 14 整体成交订单数
    "", "", "",                                                 # 15-17
]

LABELS18 = [
    "", "计划", "状态", "设置", "", "", "", "推广方式", "",
    "综合营销ROI", "", "", "", "整体消耗", "整体成交订单数", "", "", "",
]

# 08-31 真实吞列样本 A：整格无换行，名称+ID+商品挤成一行
GLUED_ONELINE = list(CLEAN_CELLS)
GLUED_ONELINE[1] = "0827_天妈轻生活_黄 ID：18744631583433031商品1抖音号素材"

# 08-31 真实吞列样本 B：名称行干净，但 ID 行粘连后续字段（16 位真 ID 尾部粘「1商品」）
GLUED_IDLINE = list(CLEAN_CELLS)
GLUED_IDLINE[1] = "0827_天妈轻生活_黄\nID：18744631583433031商品1抖音号素材"


class ExtractRowTests(unittest.TestCase):
    def test_clean_row_legacy_indices(self) -> None:
        m = _extract_row(CLEAN_CELLS, None)
        self.assertEqual(m.plan_name, "0827_天妈轻生活_黄")
        self.assertEqual(m.plan_id, "1874463158343303")  # 16 位，不多吞
        self.assertEqual(m.status, "投放中")
        self.assertEqual(m.spent, 335.92)
        self.assertEqual(m.orders, 12)
        self.assertEqual(m.roi, 6.23)
        self.assertEqual(m.budget, 300.0)
        self.assertIsNone(_malformed_reason(CLEAN_CELLS, m))

    def test_clean_row_header_labels(self) -> None:
        m = _extract_row(CLEAN_CELLS, LABELS18)
        self.assertEqual(m.plan_name, "0827_天妈轻生活_黄")
        self.assertEqual(m.spent, 335.92)
        self.assertEqual(m.orders, 12)
        self.assertIsNone(_malformed_reason(CLEAN_CELLS, m))

    def test_badge_line_before_name(self) -> None:
        cells = list(CLEAN_CELLS)
        cells[1] = "扶持中\n0827_天妈轻生活_黄\nID：1874463158343303\n1商品"
        m = _extract_row(cells, None)
        self.assertEqual(m.plan_name, "0827_天妈轻生活_黄")
        self.assertIsNone(_malformed_reason(cells, m))

    def test_glued_oneline_rejected(self) -> None:
        m = _extract_row(GLUED_ONELINE, None)
        self.assertEqual(_malformed_reason(GLUED_ONELINE, m), "计划名吞并 ID 列")

    def test_glued_idline_rejected(self) -> None:
        """名称干净但 ID 行粘连：真 ID 1874463158343303 被粘成 17 位 +「商品」。"""
        m = _extract_row(GLUED_IDLINE, None)
        self.assertEqual(m.plan_name, "0827_天妈轻生活_黄")  # 名称门拦不住
        self.assertEqual(m.plan_id, "18744631583433031")     # 多吞一位
        self.assertEqual(_malformed_reason(GLUED_IDLINE, m), "ID 列粘连后续字段")

    def test_few_cells_rejected(self) -> None:
        cells = ["", "0827_天妈轻生活_黄\nID：1874463158343303", "投放中"]
        m = _extract_row(cells, None)
        self.assertIn("单元格仅 3 个", _malformed_reason(cells, m))

    def test_missing_id_rejected(self) -> None:
        cells = list(CLEAN_CELLS)
        cells[1] = "0827_天妈轻生活_黄\n1商品 1抖音号"
        m = _extract_row(cells, None)
        self.assertEqual(_malformed_reason(cells, m), "未解析到计划 ID")

    def test_misaligned_status_rejected(self) -> None:
        cells = list(CLEAN_CELLS)
        cells[2] = "综合营销ROI目标 4.5"  # 列错位：状态格装的是设置文本
        m = _extract_row(cells, None)
        self.assertIn("状态列异常", _malformed_reason(cells, m))

    def test_empty_row_rejected(self) -> None:
        m = _extract_row([], None)
        self.assertIn("单元格仅 0 个", _malformed_reason([], m))

    def test_paused_with_reason_is_normal(self) -> None:
        """「已暂停手动暂停」这类状态+原因粘连是正常形态，不得误杀。"""
        cells = list(CLEAN_CELLS)
        cells[2] = "已暂停手动暂停"
        m = _extract_row(cells, None)
        self.assertIsNone(_malformed_reason(cells, m))
        self.assertEqual(m.pause_category, "manual_pause")


class LabelsUsableTests(unittest.TestCase):
    def test_usable(self) -> None:
        self.assertTrue(_labels_usable(LABELS18))
        self.assertTrue(_labels_usable(["计划", "整体消耗"]))

    def test_unusable(self) -> None:
        self.assertFalse(_labels_usable(None))
        self.assertFalse(_labels_usable([]))
        self.assertFalse(_labels_usable(["计划", "状态", "设置"]))  # 无指标列


class _FakePage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


class _FakeTr:
    """模拟行句柄：每次 evaluate 弹出下一份单元格文本，用完保持最后一份。"""

    def __init__(self, texts_seq: list[list[str]]) -> None:
        self._seq = list(texts_seq)
        self.page = _FakePage()

    async def evaluate(self, script: str):
        if len(self._seq) > 1:
            return self._seq.pop(0)
        return self._seq[0]


class ParseRowRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_first_try(self) -> None:
        tr = _FakeTr([CLEAN_CELLS])
        m = await _parse_plan_row(tr, LABELS18)
        self.assertIsNotNone(m)
        self.assertEqual(m.plan_id, "1874463158343303")
        self.assertEqual(tr.page.waits, [])  # 无重读

    async def test_recovers_after_half_render(self) -> None:
        """第一次读到吞列行（半渲染），重读后恢复成干净行。"""
        tr = _FakeTr([GLUED_ONELINE, CLEAN_CELLS])
        m = await _parse_plan_row(tr, LABELS18)
        self.assertIsNotNone(m)
        self.assertEqual(m.plan_name, "0827_天妈轻生活_黄")
        self.assertEqual(m.plan_id, "1874463158343303")
        self.assertEqual(tr.page.waits, [600])  # 重读了一次

    async def test_persistent_garbage_returns_none(self) -> None:
        tr = _FakeTr([GLUED_ONELINE])
        m = await _parse_plan_row(tr, LABELS18)
        self.assertIsNone(m)
        self.assertEqual(tr.page.waits, [600, 900])  # 重读 2 次后放弃

    async def test_recycled_row_returns_none(self) -> None:
        tr = _FakeTr([[]])
        m = await _parse_plan_row(tr, LABELS18)
        self.assertIsNone(m)


if __name__ == "__main__":
    unittest.main()
