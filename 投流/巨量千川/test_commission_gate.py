# -*- coding: utf-8 -*-
"""commission_gate 纯函数单测（无浏览器）。

运行：cd 投流 && runtime/python.exe -X utf8 巨量千川/test_commission_gate.py
阻断名单读写全部走 tempfile 传参，不碰生产 commission_gate_state.json。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from 巨量千川.commission_gate import (
    DEFAULT_COMMISSION_MAX_PCT,
    DEFAULT_PRODUCT_ID,
    CommissionBlockedError,
    _norm,
    build_creator_queries,
    creator_name_from_plan_name,
    group_plans_by_product,
    is_commission_blocked,
    load_commission_blocked_ids,
    load_commission_max_pct,
    match_plan_commission,
    parse_commission_from_card_text,
    parse_commission_from_row_text,
    pick_drawer_candidate,
    save_commission_blocked,
    select_pause_targets,
)


class ParseCommissionTests(unittest.TestCase):
    def test_drawer_row_shape(self):
        text = "肥妈美食\nID: 20161121Z\n自运营\n5%\n授权生效"
        self.assertEqual(parse_commission_from_row_text(text), 5.0)

    def test_decimal_and_label(self):
        self.assertEqual(parse_commission_from_row_text("5.5%"), 5.5)
        self.assertEqual(parse_commission_from_row_text("带货佣金率 6.25%"), 6.25)

    def test_full_width_percent(self):
        self.assertEqual(parse_commission_from_row_text("5.0％"), 5.0)

    def test_placeholder_and_missing(self):
        self.assertIsNone(parse_commission_from_row_text("--"))
        self.assertIsNone(parse_commission_from_row_text("—"))
        self.assertIsNone(parse_commission_from_row_text("没有任何百分比"))
        self.assertIsNone(parse_commission_from_row_text(""))

    def test_first_percent_wins(self):
        """行内多个百分比取第一个（列序语义=带货佣金率列）。"""
        self.assertEqual(parse_commission_from_row_text("5% 后续 20%"), 5.0)


class ParseCardCommissionTests(unittest.TestCase):
    """素材卡佣金率（用户 2026-09-08 梦妮 15% 截图实证：只认「佣金率:」标签）。"""

    def test_labeled_percent(self):
        text = "竖版 01:26\n首发素材\n番茄肥牛汤汁用来拌米…\n佣金率： 15%\n❤ 815"
        self.assertEqual(parse_commission_from_card_text(text), 15.0)

    def test_half_width_colon_and_decimal(self):
        self.assertEqual(parse_commission_from_card_text("佣金率:5.5%"), 5.5)
        self.assertEqual(parse_commission_from_card_text("佣金率： 5％"), 5.0)

    def test_no_label_other_percent_is_none(self):
        """标题含其它 % 文案不能误当佣金率。"""
        self.assertIsNone(parse_commission_from_card_text("竖版 01:26\n限时5折 50% off\n❤ 287"))
        self.assertIsNone(parse_commission_from_card_text("首发素材\n没有佣金标签"))
        self.assertIsNone(parse_commission_from_card_text(""))

    def test_card_gate_boundary(self):
        """=5% 仍投、>5% 拦，与达人维度同边界。"""
        self.assertFalse(is_commission_blocked(parse_commission_from_card_text("佣金率：5%"), 5.0))
        self.assertTrue(is_commission_blocked(parse_commission_from_card_text("佣金率：15%"), 5.0))
        self.assertFalse(is_commission_blocked(parse_commission_from_card_text("无标签卡"), 5.0))


class IsBlockedTests(unittest.TestCase):
    def test_equal_max_still_allowed(self):
        """=5% 仍投（用户 2026-09-07 拍板：5%以上才不投）。"""
        self.assertFalse(is_commission_blocked(5.0, 5.0))

    def test_above_max_blocked(self):
        self.assertTrue(is_commission_blocked(5.1, 5.0))

    def test_below_max_allowed(self):
        self.assertFalse(is_commission_blocked(4.99, 5.0))

    def test_unknown_rate_allowed(self):
        """读不到放行（COMMISSION_UNKNOWN_POLICY=allow）。"""
        self.assertFalse(is_commission_blocked(None, 5.0))

    def test_custom_threshold(self):
        self.assertFalse(is_commission_blocked(5.5, 6.0))
        self.assertTrue(is_commission_blocked(6.5, 6.0))


class LoadThresholdTests(unittest.TestCase):
    def test_missing_file_default(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_commission_max_pct(Path(td) / "nope.json"),
                             DEFAULT_COMMISSION_MAX_PCT)

    def test_missing_key_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"other": 1}), encoding="utf-8")
            self.assertEqual(load_commission_max_pct(p), 5.0)

    def test_bad_json_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_commission_max_pct(p), 5.0)

    def test_custom_value(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"commission_max_pct": 6.0}), encoding="utf-8")
            self.assertEqual(load_commission_max_pct(p), 6.0)


class CreatorNameTests(unittest.TestCase):
    def test_standard_new_plan_name(self):
        self.assertEqual(creator_name_from_plan_name("0907_肥妈美食_赵"), "肥妈美食")

    def test_legacy_dashed_date_name(self):
        """复刻 plan_metrics 命名正则：'07-18_洋琪妈妈的日记_张' 形态。"""
        self.assertEqual(creator_name_from_plan_name("07-18_洋琪妈妈的日记_张"),
                         "洋琪妈妈的日记")


class MatchTests(unittest.TestCase):
    BY_ID = {"20161121Z": 6.5}
    BY_NAME = {"肥妈美食": 6.5}

    def test_creator_id_hit(self):
        plan = {"creator_name": "20161121Z", "plan_name": "别的名字"}
        self.assertEqual(match_plan_commission(plan, self.BY_ID, self.BY_NAME), 6.5)

    def test_plan_name_hit_normalized(self):
        plan = {"creator_name": "", "plan_name": "0907_ 肥妈美食 _赵"}
        self.assertEqual(match_plan_commission(plan, self.BY_ID, self.BY_NAME), 6.5)

    def test_creator_display_name_hit(self):
        """monitor 来源的 creator_name 存显示名，规范化后也能命中 by_name。"""
        plan = {"creator_name": "肥妈美食 ", "plan_name": "无可用信息"}
        self.assertEqual(match_plan_commission(plan, self.BY_ID, self.BY_NAME), 6.5)

    def test_no_match_none(self):
        plan = {"creator_name": "999", "plan_name": "0907_陌生达人_赵"}
        self.assertIsNone(match_plan_commission(plan, self.BY_ID, self.BY_NAME))


def _plan(plan_id, status="投放中", name="0907_肥妈美食_赵", creator="20161121Z"):
    return {"plan_id": plan_id, "plan_name": name, "creator_name": creator,
            "plan_status": status, "department": "业务1部"}


class SelectTargetsTests(unittest.TestCase):
    BY_ID = {"20161121Z": 6.5, "equal5": 5.0}
    BY_NAME = {"肥妈美食": 6.5}

    def test_running_above_threshold_targeted(self):
        targets, wl, unknown = select_pause_targets(
            [_plan("p1")], self.BY_ID, self.BY_NAME, 5.0,
            whitelisted=lambda **kw: False)
        self.assertEqual([t["plan_id"] for t in targets], ["p1"])
        self.assertEqual(targets[0]["commission_rate"], 6.5)
        self.assertEqual(wl, [])
        self.assertEqual(unknown, [])

    def test_paused_plan_skipped(self):
        targets, _, _ = select_pause_targets(
            [_plan("p1", status="已暂停")], self.BY_ID, self.BY_NAME, 5.0,
            whitelisted=lambda **kw: False)
        self.assertEqual(targets, [])

    def test_whitelist_skipped(self):
        targets, wl, _ = select_pause_targets(
            [_plan("p1")], self.BY_ID, self.BY_NAME, 5.0,
            whitelisted=lambda **kw: True)
        self.assertEqual(targets, [])
        self.assertEqual([p["plan_id"] for p in wl], ["p1"])

    def test_unmatched_goes_unknown_bucket(self):
        targets, _, unknown = select_pause_targets(
            [_plan("p1", creator="999", name="0907_陌生达人_赵")],
            self.BY_ID, self.BY_NAME, 5.0, whitelisted=lambda **kw: False)
        self.assertEqual(targets, [])
        self.assertEqual([p["plan_id"] for p in unknown], ["p1"])

    def test_equal_5_not_targeted(self):
        """=5% 仍投：不进 targets。"""
        targets, _, _ = select_pause_targets(
            [_plan("p1", creator="equal5", name="无匹配名")],
            self.BY_ID, {"x": 6.5}, 5.0, whitelisted=lambda **kw: False)
        self.assertEqual(targets, [])


class BlockedListTests(unittest.TestCase):
    def test_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            save_commission_blocked({"p1": {"plan_name": "a", "rate": 6.5}}, p)
            save_commission_blocked({"p2": {"plan_name": "b", "rate": 7.0}}, p)
            self.assertEqual(load_commission_blocked_ids(p), {"p1", "p2"})

    def test_missing_file_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_commission_blocked_ids(Path(td) / "nope.json"), set())

    def test_bad_json_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("{bad", encoding="utf-8")
            self.assertEqual(load_commission_blocked_ids(p), set())


class ErrorContractTests(unittest.TestCase):
    def test_message_contains_keyword(self):
        """poll 侧 _is_fatal 靠「佣金过高」识别，message 必须含该词。"""
        err = CommissionBlockedError("佣金过高：douX 带货佣金率6.5%＞上限5.0%，按规则不投放")
        self.assertIn("佣金过高", str(err))
        self.assertIsInstance(err, RuntimeError)


class GroupByProductTests(unittest.TestCase):
    """2026-09-16：佣金率是（达人×商品）维度（小沈妈 肥牛10%/他品5% 实证），必须分组扫。"""

    def test_registry_product_wins(self):
        plans = [_plan("p1"), _plan("p2", name="0916_鸥米宝百货_黄")]
        registry = {"p2": {"creator_id": "79331714386", "product_id": "prodB"}}
        groups = group_plans_by_product(plans, registry)
        self.assertEqual([p["plan_id"] for p in groups["prodB"]], ["p2"])
        self.assertEqual([p["plan_id"] for p in groups[DEFAULT_PRODUCT_ID]], ["p1"])

    def test_empty_product_falls_back_default(self):
        registry = {"p1": {"creator_id": "x", "product_id": ""}}
        groups = group_plans_by_product([_plan("p1")], registry)
        self.assertIn(DEFAULT_PRODUCT_ID, groups)

    def test_no_registry_all_default(self):
        groups = group_plans_by_product([_plan("p1"), _plan("p2")], {})
        self.assertEqual(len(groups[DEFAULT_PRODUCT_ID]), 2)


class BuildQueriesTests(unittest.TestCase):
    def test_cid_preferred_over_name(self):
        plans = [_plan("p1")]
        registry = {"p1": {"creator_id": "20161121Z", "product_id": ""}}
        qs = build_creator_queries(plans, registry)
        self.assertEqual(qs, [{"key": "20161121Z", "cid": "20161121Z", "name": "肥妈美食"}])

    def test_name_fallback_from_plan_name(self):
        qs = build_creator_queries([_plan("p1")], {})
        self.assertEqual(qs[0]["cid"], "")
        self.assertEqual(qs[0]["name"], "肥妈美食")

    def test_dedupe_same_creator(self):
        plans = [_plan("p1"), _plan("p2")]  # 同名同达人两条计划
        qs = build_creator_queries(plans, {})
        self.assertEqual(len(qs), 1)


class NormTests(unittest.TestCase):
    """2026-09-16：抽屉显示名带 emoji，规范化必须剥掉非文字字符才能匹配计划名。"""

    def test_emoji_stripped(self):
        self.assertEqual(_norm("Yan🍓的美食日记"), _norm("Yan的美食日记"))

    def test_emoji_suffix_stripped(self):
        self.assertEqual(_norm("一勺可可酱🍏"), "一勺可可酱")

    def test_space_and_case(self):
        self.assertEqual(_norm(" 肥妈美食 "), "肥妈美食")
        self.assertEqual(_norm("Happy妈妈"), "happy妈妈")


class PickCandidateTests(unittest.TestCase):
    CANDS = [{"cid": "333696111", "name": "浦东猫蛋", "rate": 5.0},
             {"cid": "333696111a", "name": "南汇猫蛋", "rate": 10.0}]

    def test_id_exact_wins(self):
        pick = pick_drawer_candidate(self.CANDS, {"cid": "333696111a", "name": "南汇猫蛋"})
        self.assertEqual(pick["rate"], 10.0)

    def test_name_exact_when_no_cid(self):
        pick = pick_drawer_candidate(self.CANDS, {"cid": "", "name": "浦东猫蛋"})
        self.assertEqual(pick["cid"], "333696111")

    def test_single_candidate_fallback(self):
        pick = pick_drawer_candidate([{"cid": "x", "name": "任意", "rate": 6.0}],
                                     {"cid": "", "name": "搜的名对不上"})
        self.assertEqual(pick["rate"], 6.0)

    def test_multi_candidate_no_match_none(self):
        pick = pick_drawer_candidate(self.CANDS, {"cid": "", "name": "不存在的达人"})
        self.assertIsNone(pick)


if __name__ == "__main__":
    unittest.main()
