"""离线浏览器回归：只打开测试HTML，不连接业务浏览器。"""
import os
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'投流'))
from playwright.async_api import async_playwright
from 巨量千川.main_pull_scout import CREATOR_NEXT_SELECTOR

@unittest.skipUnless(os.environ.get('TOULIU_UI_FIXTURE_TESTS')=='1','按需启用本机Edge离线浏览器测试')
class MainLocatorBrowserTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.pw=await async_playwright().start()
  self.browser=await self.pw.chromium.launch(channel='msedge',headless=True)
  self.page=await self.browser.new_page()
 async def asyncTearDown(self):
  await self.browser.close();await self.pw.stop()
 async def test_ellipsis_is_never_the_next_page(self):
  await self.page.set_content('''<div class="ovui-pagination"><div class="ovui-page-turner__next-ellipsis" onclick="window.chosen='ellipsis'">...</div><div><div class="ovui-page-turner__next-icon" onclick="window.chosen='next'">next</div></div></div>''')
  choices=self.page.locator(CREATOR_NEXT_SELECTOR)
  self.assertEqual(await choices.count(),1)
  await choices.first.click()
  self.assertEqual(await self.page.evaluate('window.chosen'),'next')
 async def test_disabled_last_page_is_preserved(self):
  await self.page.set_content('''<div class="ovui-pagination"><div class="ovui-page-turner__next-ellipsis">...</div><div class="ovui-page-turner--disabled"><div class="ovui-page-turner__next-icon">next</div></div></div>''')
  self.assertTrue(await self.page.locator(CREATOR_NEXT_SELECTOR).first.evaluate("e=>!!e.closest('[disabled], [aria-disabled=\"true\"], [class*=\"disabled\"]')"))
