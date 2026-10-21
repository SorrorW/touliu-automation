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




class VisibleRowsBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os
        if os.environ.get("TOULIU_UI_FIXTURE_TESTS") != "1":self.skipTest("离线浏览器测试按需启用")
        from playwright.async_api import async_playwright
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(channel="msedge",headless=True)
        self.page=await self.browser.new_page()
    async def asyncTearDown(self):
        if hasattr(self,"browser"):await self.browser.close()
        if hasattr(self,"pw"):await self.pw.stop()
    async def test_hidden_duplicate_tables_and_rows_are_not_targets(self):
        await self.page.set_content('<table style="display:none"><tbody><tr id="hidden-table"><td>duplicate</td></tr></tbody></table><table><tbody><tr id="first"><td>visible</td></tr><tr id="hidden-row" style="display:none"><td>hidden</td></tr><tr id="invisible" style="visibility:hidden"><td>hidden</td></tr><tr id="last"><td>visible</td></tr></tbody></table>')
        expected=[]
        rows=self.page.locator("table tbody tr")
        for i in range(await rows.count()):
            row=rows.nth(i)
            if await row.is_visible():expected.append(await row.get_attribute("id"))
        actual=[await row.get_attribute("id") for row in await pm._visible_plan_rows(self.page)]
        self.assertEqual(actual,expected);self.assertEqual(actual,["first","last"])
    async def test_visible_rows_are_refreshed_after_table_changes(self):
        await self.page.set_content('<table><tbody><tr id="old"><td>old</td></tr><tr id="new" style="display:none"><td>new</td></tr></tbody></table>')
        before=await pm._visible_plan_rows(self.page)
        self.assertEqual([await row.get_attribute("id") for row in before],["old"])
        await self.page.evaluate("document.querySelector('#old').style.display='none';document.querySelector('#new').style.display='table-row'")
        after=await pm._visible_plan_rows(self.page)
        self.assertEqual([await row.get_attribute("id") for row in after],["new"])

if __name__ == "__main__":
    unittest.main()
