"""素材资格与已在计划内判断的回归，不连接业务浏览器。"""
from pathlib import Path
import sys,unittest
from types import SimpleNamespace
HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'投流')]
from 巨量千川.main_products import MAIN_PRODUCTS
from 巨量千川.main_product_materials import checked_videos,choose_video,scoped_response,MaterialReadError
A=MAIN_PRODUCTS[0]
def video(vid='v1',uid='uid1',created=100,**extra):
 return {'videoId':vid,'itemId':'item-'+vid,'authorUid':uid,'createTime':str(created),**extra}
class MaterialSelectionTests(unittest.TestCase):
 def test_newest_by_actual_creation_time(self):
  videos=checked_videos([{'videoList':[video('old',created=10),video('new',created=20)],'hasMore':False}],'uid1')
  self.assertEqual(choose_video(videos,{'old':False,'new':False})['videos'][0]['videoId'],'new')
 def test_existing_video_prevents_adding_latest(self):
  v=[video('new',created=20),video('old',created=10)]
  self.assertEqual(choose_video(v,{'old':True,'new':False})['kind'],'existing')
 def test_incomplete_pages_cannot_mean_missing(self):
  with self.assertRaises(MaterialReadError):checked_videos([{'videoList':[],'hasMore':True}],'uid1')
 def test_wrong_creator_rejected(self):
  with self.assertRaises(MaterialReadError):checked_videos([{'videoList':[video(uid='other')],'hasMore':False}],'uid1')
 def test_unknown_usage_is_not_missing(self):
  with self.assertRaises(MaterialReadError):choose_video([video()],{})
 def test_string_false_not_accepted(self):
  with self.assertRaises(MaterialReadError):choose_video([video()],{'v1':'false'})
 def test_paging_detects_existing_older_material(self):
  v=checked_videos([{'videoList':[video('new',created=20)],'hasMore':True,'cursor':'one'},{'videoList':[video('old',created=10)],'hasMore':False,'cursor':'two'}],'uid1')
  self.assertEqual(choose_video(v,{'new':False,'old':True})['kind'],'existing')
 def test_no_video_is_waiting(self):
  self.assertEqual(choose_video([],{}),{'kind':'unavailable','videos':[]})
 def test_old_latest_video_is_still_eligible_for_main_product(self):
  self.assertEqual(choose_video([video(created=1)],{'v1':False})['kind'],'missing')
 def test_wrong_plan_usage_rejected(self):
  r=SimpleNamespace(url='https://example.test/check?adId=wrong',post_data_json=None)
  with self.assertRaises(MaterialReadError):scoped_response(r,A)
 def test_exact_plan_usage_allowed(self):
  scoped_response(SimpleNamespace(url='https://example.test/check?adId='+A.plan_id,post_data_json=None),A)
 def test_blocked_newest_skipped_for_next_available(self):
  x=choose_video([video('bad',reasonCode='disabled'),video('ok')],{'bad':False,'ok':False})
  self.assertEqual(x['videos'][0]['videoId'],'ok')


class MaterialResponseOrderingTests(unittest.IsolatedAsyncioTestCase):
 async def test_usage_response_can_arrive_before_video_response(self):
  from 巨量千川.main_product_materials import Journal,_read_videos
  journal=Journal.__new__(Journal)
  request=SimpleNamespace(url='https://example.test/check?adId='+A.plan_id+'&aavid='+A.account_id+'&productId='+A.product_id+'&userId=uid1&AwemeUID=uid1',post_data_json=None)
  journal.rows=[{'path':'/check-ad-material-usage','request':request,'body':{'status_code':0,'data':{'inUseMaterialMap':{'v1':True}}}},
                {'path':'/get-items-by-product','request':request,'body':{'status_code':0,'data':{'videoList':[video()],'hasMore':False}}}]
  videos,usage,_=await _read_videos(None,None,journal,A,'uid1',0)
  self.assertEqual(choose_video(videos,usage)['kind'],'existing')

class ScopedVideoResponseTests(unittest.TestCase):
 def test_wrong_or_missing_author_cannot_prove_no_videos(self):
  from 巨量千川.main_product_materials import video_request_matches
  base='https://example.test/videos?aavid='+A.account_id+'&productId='+A.product_id
  for suffix in ['', '&userId=other']:
   self.assertFalse(video_request_matches(SimpleNamespace(url=base+suffix),A,'uid1'))
 def test_correct_account_product_and_author_required(self):
  from 巨量千川.main_product_materials import video_request_matches
  url='https://example.test/videos?aavid='+A.account_id+'&productId='+A.product_id+'&userId=uid1'
  self.assertTrue(video_request_matches(SimpleNamespace(url=url),A,'uid1'))
  self.assertFalse(video_request_matches(SimpleNamespace(url=url.replace(A.product_id,'other')),A,'uid1'))


class MaterialPaginationTests(unittest.IsolatedAsyncioTestCase):
 async def test_complete_scan_reads_all_pages_and_sorts_latest(self):
  from unittest.mock import AsyncMock,patch
  from 巨量千川 import main_product_materials as m
  pages=[({'videoList':[video('old',created=10)],'hasMore':True,'cursor':'c1'},{'old':False}),
         ({'videoList':[video('new',created=20)],'hasMore':False,'cursor':'c2'},{'new':False})]
  journal=SimpleNamespace(rows=[])
  with patch.object(m,'_video_page',AsyncMock(side_effect=pages)),patch.object(m,'_advance_video_page',AsyncMock()) as advance:
   videos,usage,_=await m._read_videos(None,object(),journal,A,'uid1',0)
  self.assertEqual([v['videoId'] for v in videos],['new','old'])
  self.assertEqual(usage,{'old':False,'new':False});advance.assert_awaited_once()
 async def test_selection_reopens_from_first_page(self):
  from unittest.mock import AsyncMock,patch
  from 巨量千川 import main_product_materials as m
  picker=object();journal=SimpleNamespace(rows=[]);v=video('new')
  with patch.object(m,'_open_picker',AsyncMock(return_value=picker)) as reopen,patch.object(m,'_select_creator',AsyncMock(return_value=('uid1',0))),patch.object(m,'_video_page',AsyncMock(return_value=({'videoList':[v],'hasMore':False},{'new':False}))),patch.object(m,'_advance_video_page',AsyncMock()) as advance:
   got=await m._selection_page(None,A,'creator',journal,v)
  self.assertIs(got[0],picker);reopen.assert_awaited_once();advance.assert_not_awaited()
 async def test_newly_existing_other_video_stops_selection(self):
  from unittest.mock import AsyncMock,patch
  from 巨量千川 import main_product_materials as m
  existing=video('old');journal=SimpleNamespace(rows=[])
  with patch.object(m,'_open_picker',AsyncMock(return_value=object())),patch.object(m,'_select_creator',AsyncMock(return_value=('uid1',0))),patch.object(m,'_video_page',AsyncMock(return_value=({'videoList':[existing],'hasMore':True},{'old':True}))),patch.object(m,'_advance_video_page',AsyncMock()) as advance:
   _,videos,usage=await m._selection_page(None,A,'creator',journal,video('new'))
  self.assertEqual(videos,[existing]);self.assertIs(usage['old'],True);advance.assert_not_awaited()
 async def test_changed_video_cannot_silently_select_another(self):
  from unittest.mock import AsyncMock,patch
  from 巨量千川 import main_product_materials as m
  journal=SimpleNamespace(rows=[])
  with patch.object(m,'_open_picker',AsyncMock(return_value=object())),patch.object(m,'_select_creator',AsyncMock(return_value=('uid1',0))),patch.object(m,'_video_page',AsyncMock(return_value=({'videoList':[video('other')],'hasMore':False},{'other':False}))):
   with self.assertRaisesRegex(m.MaterialReadError,'已从当前可用列表消失'):await m._selection_page(None,A,'creator',journal,video('new'))

class ScopedUsageTests(unittest.TestCase):
 def test_usage_requires_same_account_product_plan_and_author(self):
  from 巨量千川.main_product_materials import usage_request_matches
  url='https://example.test/check?aavid='+A.account_id+'&adId='+A.plan_id+'&productId='+A.product_id+'&AwemeUID=uid1'
  self.assertTrue(usage_request_matches(SimpleNamespace(url=url),A,'uid1'))
  for value in [A.account_id,A.plan_id,A.product_id,'uid1']:
   self.assertFalse(usage_request_matches(SimpleNamespace(url=url.replace(value,'wrong')),A,'uid1'))


class DropdownResponseWindowTests(unittest.IsolatedAsyncioTestCase):
 async def test_enter_can_load_videos_before_the_option_click(self):
  from unittest.mock import AsyncMock,Mock
  from 巨量千川.main_product_materials import Journal,_select_creator,_video_page
  journal=Journal.__new__(Journal);journal.rows=[]
  req=SimpleNamespace(url='https://example.test/check?adId='+A.plan_id+'&aavid='+A.account_id+'&productId='+A.product_id+'&userId=uid1&AwemeUID=uid1',post_data_json=None)
  async def enter(*args):
   journal.rows.extend([
    {'path':'/aweme/get-aweme-list-v2','body':{'data':{'uniqueId':'creator01','id':'uid1'}}},
    {'path':'/get-items-by-product','request':req,'body':{'status_code':0,'data':{'videoList':[video()],'hasMore':False}}},
    {'path':'/check-ad-material-usage','request':req,'body':{'status_code':0,'data':{'inUseMaterialMap':{'v1':True}}}}
   ])
  search=SimpleNamespace(count=AsyncMock(return_value=1),fill=AsyncMock(),press=AsyncMock(side_effect=enter))
  option=SimpleNamespace(wait_for=AsyncMock(),count=AsyncMock(return_value=1),click=AsyncMock())
  options=SimpleNamespace(get_by_text=Mock(return_value=SimpleNamespace(filter=Mock(return_value=option))))
  page=SimpleNamespace(locator=Mock(side_effect=lambda selector: search if selector.startswith('input') else options))
  picker=SimpleNamespace(get_by_text=Mock(return_value=SimpleNamespace(filter=Mock(return_value=SimpleNamespace(click=AsyncMock())))))
  uid,start=await _select_creator(page,picker,journal,'creator01')
  self.assertEqual(uid,'uid1');self.assertEqual(start,0)
  data,usage=await _video_page(journal,A,uid,start)
  self.assertEqual(data['videoList'][0]['videoId'],'v1');self.assertTrue(usage['v1'])
  self.assertIn('.auto-aweme-filter-select-drop-item:visible',page.locator.call_args_list[-1].args[0])
