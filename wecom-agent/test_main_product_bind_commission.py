from pathlib import Path
import sys, unittest
from types import SimpleNamespace
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"投流"))
from 巨量千川.main_products import MAIN_PRODUCTS
A=MAIN_PRODUCTS[0]

class CommissionOptionTests(unittest.IsolatedAsyncioTestCase):
 async def run_toggle(self,checked=False,changed_params=False,stuck=False):
  from unittest.mock import AsyncMock,patch
  from 巨量千川 import main_product_bind as bind
  cb=SimpleNamespace(is_checked=AsyncMock(side_effect=[checked,checked if stuck else False]),evaluate=AsyncMock())
  dialogs=SimpleNamespace(all=AsyncMock(return_value=[]))
  page=SimpleNamespace(wait_for_timeout=AsyncMock(),locator=lambda _:dialogs)
  params=[{'placeholder':'ROI','value':'40'},{'placeholder':'预算','value':'1000'}]
  with patch.object(bind,'assert_editor',AsyncMock()),patch.object(bind,'commission_checkbox',AsyncMock(return_value=cb)),patch.object(bind,'parameter_snapshot',AsyncMock(return_value=[] if changed_params else params)):
   result=await bind.disable_commission_before_bind(page,A,params)
  return result,cb
 async def test_checked_option_is_disabled(self):
  changed,cb=await self.run_toggle(True)
  self.assertTrue(changed);cb.evaluate.assert_awaited_once()
 async def test_already_disabled_is_not_toggled_back_on(self):
  changed,cb=await self.run_toggle(False)
  self.assertFalse(changed);cb.evaluate.assert_not_awaited()
 async def test_failed_uncheck_blocks_save(self):
  with self.assertRaisesRegex(RuntimeError,'未取消成功'):await self.run_toggle(True,stuck=True)
 async def test_other_roi_or_budget_changes_block_save(self):
  with self.assertRaisesRegex(RuntimeError,'其他投放字段变化'):await self.run_toggle(True,changed_params=True)

