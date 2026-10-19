# -*- coding: utf-8 -*-
"""local_tools「佣金过高」fatal 识别单测。

运行：cd wecom-agent && ../投流/runtime/python.exe -X utf8 test_local_tools_commission.py
"""
import sys
import unittest

sys.path.insert(0, ".")

from local_tools import _is_fatal


class CommissionFatalTests(unittest.TestCase):
    def test_commission_message_is_fatal(self):
        """佣金闸建计划拦截的 message 必须识别为不可重试终态。"""
        detail = "佣金过高：dou20161121Z 带货佣金率6.5%＞上限5.0%，按规则不投放"
        self.assertTrue(_is_fatal(detail))

    def test_existing_keywords_still_fatal(self):
        for kw in ("企业机构号暂不支持", "合作码错误", "超48小时"):
            self.assertTrue(_is_fatal(kw), kw)

    def test_transient_message_not_fatal(self):
        self.assertFalse(_is_fatal("网络超时，请稍后重试"))
        self.assertFalse(_is_fatal(""))
        self.assertFalse(_is_fatal(None))


if __name__ == "__main__":
    unittest.main()
