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
 async def test_creator_scan_waits_for_delayed_total(self):
  from 巨量千川.main_pull_scout import wait_creator_list_ready
  await self.page.set_content('<table><tbody><tr><td>ID：creator_1</td></tr></tbody></table>')
  await self.page.evaluate("setTimeout(()=>{const p=document.createElement('p');p.textContent='共 1 条记录';document.body.append(p)},150)")
  await wait_creator_list_ready(self.page,timeout=2000)
  self.assertIn('共 1 条记录',await self.page.locator('body').inner_text())
 async def test_creator_scan_zero_total_needs_explicit_empty_state(self):
  from 巨量千川.main_pull_scout import wait_creator_list_ready
  from playwright.async_api import TimeoutError
  await self.page.set_content('<p>共 0 条记录</p>')
  with self.assertRaises(TimeoutError):await wait_creator_list_ready(self.page,timeout=200)
  await self.page.set_content('<p>共 0 条记录</p><p>暂无数据</p>')
  await wait_creator_list_ready(self.page,timeout=1000)
 async def test_ellipsis_is_never_the_next_page(self):
  await self.page.set_content('''<div class="ovui-pagination"><div class="ovui-page-turner__next-ellipsis" onclick="window.chosen='ellipsis'">...</div><div><div class="ovui-page-turner__next-icon" onclick="window.chosen='next'">next</div></div></div>''')
  choices=self.page.locator(CREATOR_NEXT_SELECTOR)
  self.assertEqual(await choices.count(),1)
  await choices.first.click()
  self.assertEqual(await self.page.evaluate('window.chosen'),'next')
 async def test_disabled_last_page_is_preserved(self):
  await self.page.set_content('''<div class="ovui-pagination"><div class="ovui-page-turner__next-ellipsis">...</div><div class="ovui-page-turner--disabled"><div class="ovui-page-turner__next-icon">next</div></div></div>''')
  self.assertTrue(await self.page.locator(CREATOR_NEXT_SELECTOR).first.evaluate("e=>!!e.closest('[disabled], [aria-disabled=\"true\"], [class*=\"disabled\"]')"))

 async def test_main_creator_drawer_opens_under_plugin_overlay(self):
  from 巨量千川.main_product_bind import open_main_creator_drawer
  await self.page.set_content('''<button onclick="document.querySelector('.ovui-drawer').style.display='block'">管理手动添加抖音号</button><div class="ovui-drawer" style="display:none"><input placeholder="请输入抖音号"></div><div style="position:fixed;inset:0;background:transparent;z-index:9999"></div>''')
  await open_main_creator_drawer(self.page)
  self.assertEqual(await self.page.locator('.ovui-drawer:visible input').count(),1)
 async def test_ambiguous_main_creator_entries_are_never_clicked(self):
  from 巨量千川.main_product_bind import open_main_creator_drawer
  await self.page.set_content('''<button onclick="window.clicked=true">管理手动添加抖音号</button><button onclick="window.clicked=true">管理手动添加抖音号</button>''')
  with self.assertRaisesRegex(RuntimeError,'入口不唯一'):await open_main_creator_drawer(self.page)
  self.assertIsNone(await self.page.evaluate('window.clicked'))

 async def test_confirming_creator_rechecks_commission_then_save_disables_it(self):
  from types import SimpleNamespace
  from unittest.mock import AsyncMock, patch
  from 巨量千川 import main_product_bind as bind
  from 巨量千川.main_products import MAIN_PRODUCTS
  page=await self.browser.new_page()
  await page.set_content('''<input placeholder="请输入计划名称" value="existing-plan"><input placeholder="ROI" value="40"><input placeholder="预算" value="1000"><label><input type="checkbox" id="commission" checked><span>含达人佣金出价</span></label><button onclick="document.querySelector('.ovui-drawer').style.display='block'">管理手动添加抖音号</button><div class="ovui-drawer" style="display:none"><input placeholder="请输入抖音号"><table><tbody><tr><td>ID：creator_1</td><td>佣金率5%</td><td><label class="ovui-checkbox"><input type="checkbox"></label></td></tr></tbody></table><button onclick="document.querySelector('#commission').checked=true;this.parentElement.style.display='none'">确定</button></div><button onclick="window.savedCommission=document.querySelector('#commission').checked">保存</button>''')
  binding={'creator_id':'creator_1','bind_status':'在投'}
  async def reopen(p,route):
   saved=await p.evaluate('window.savedCommission')
   if saved is not None:
    self.assertFalse(saved)
  with patch.object(bind,'connect_browser',AsyncMock(return_value=SimpleNamespace(contexts=[None]))),patch.object(bind,'open_main_account_page',AsyncMock(return_value=page)),patch.object(bind,'open_editor',reopen),patch.object(bind,'assert_editor',AsyncMock()),patch.object(bind,'read_target',AsyncMock(side_effect=[{'rows':[]},{'rows':[binding]}])):
   outcome=await bind.bind('creator_1',MAIN_PRODUCTS[0])
  self.assertEqual(outcome['outcome'],'added')
  self.assertFalse(outcome['commission_included'])

 async def test_manual_creator_tab_can_be_selected_under_navigation_arrow(self):
  from types import SimpleNamespace
  from unittest.mock import AsyncMock
  from 巨量千川.main_product_materials import _select_manual_creator
  await self.page.set_content('<div class="ovui-tabs__tab-btn"><div class="tab-content-item" onclick="window.selected=true">测试达人</div></div><div style="position:fixed;inset:0;z-index:9">navigation arrow</div>')
  journal=SimpleNamespace(rows=[{'path':'/ad/get_ad_aweme_list','body':{'data':{'uniqueId':'creator_1','id':'uid_1','name':'测试达人'}}}],wait_material_idle=AsyncMock())
  selected=await _select_manual_creator(self.page,journal,'creator_1')
  self.assertEqual(selected,('uid_1',1));self.assertTrue(await self.page.evaluate('window.selected'))
 async def test_material_confirmation_tracks_submitted_drawer_not_visible_parent(self):
  from 巨量千川.main_product_materials import _confirm_selection
  await self.page.set_content('''<div class="ovui-drawer" id="parent">plan details</div><div class="ovui-drawer" id="picker"><button onclick="document.querySelector('#picker').style.display='none'">确定</button></div>''')
  await _confirm_selection(self.page.locator('.ovui-drawer:visible').last)
  self.assertTrue(await self.page.locator('#parent').is_visible())
  self.assertFalse(await self.page.locator('#picker').is_visible())
