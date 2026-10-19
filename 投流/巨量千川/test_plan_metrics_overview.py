# -*- coding: utf-8 -*-
"""新版 /overall-prom 总览页复用回归测试。"""
import unittest

from 巨量千川 import plan_metrics as pm


class OverviewPageReuseTests(unittest.TestCase):
    def test_new_overall_prom_route_is_reused_for_same_department(self):
        class Page:
            def __init__(self, url):
                self.url = url

        account = "1861049594712072"
        expected = Page(
            f"https://qianchuan.jinritemai.com/overall-prom?aavid={account}&dateRange=2026-09-23%2C2026-09-23"
        )
        pages = [
            Page("https://qianchuan.jinritemai.com/uni-prom/overall?aavid=other"),
            expected,
        ]
        self.assertIs(pm._department_overview_page(pages, account), expected)


if __name__ == "__main__":
    unittest.main()
