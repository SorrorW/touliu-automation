"""四主品授权/绑定实质回归：使用临时 SQLite 和模拟平台，不触及业务环境。"""
import copy
from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parent/'投流')]
import main_account_flow as flow
from 巨量千川 import main_product_store as store
from 巨量千川.main_products import MAIN_PRODUCTS, MAIN_AAVID, WORKFLOW_VERSION, product_route, binding_key, check_identity
from wecom_client import simplify_record
A,B,C,D=MAIN_PRODUCTS

def simple(creator='creator01',route=A,progress='待授权',**extra):
    return {'抖音号':creator,'商品ID':route.product_id,'进度':progress,'合作码':'fixture-only','业务部':'业务1部-1',**extra}

def binding(route=A,creator='creator01',kind='手动授权',status='在投'):
    return {'account_id':MAIN_AAVID,'product_id':route.product_id,'plan_id':route.plan_id,'plan_key':route.name,
            'creator_id':creator,'creator_name':'测试达人','bind_type':kind,'bind_status':status}

class Platform:
    def __init__(self):
        self.rows={r.plan_id:[] for r in MAIN_PRODUCTS}
        self.calls=[];self.complete=True;self.rate=5.0;self.authorized=True;self.pending=False
        self.add_then_timeout=False;self.on_scan=None;self.invalid_account=False
        self.materials={};self.material_error=None;self.material_wait=False;self.material_response_lost=False;self.material_wrong_target=False
    def scan(self,route):
        self.calls.append(('scan',route.plan_id))
        if self.on_scan:self.on_scan(self,route)
        return {'rows':copy.deepcopy(self.rows[route.plan_id]),'complete':self.complete,
                'account_id':'wrong' if self.invalid_account else route.account_id,
                'product_id':route.product_id,'plan_id':route.plan_id}
    def probe(self,creator,route):
        self.calls.append(('probe',creator,route.plan_id))
        return {'commission_pct':self.rate,'account_id':route.account_id,'product_id':route.product_id,'plan_id':route.plan_id}
    def auth_check(self,creator):
        self.calls.append(('check',creator))
        return {'authorized':self.authorized,'douyin_auth':'授权生效' if self.authorized else '待达人确认','toufang_auth':'授权生效' if self.authorized else '未申请'}
    def authorize(self,creator,code):
        self.calls.append(('authorize',creator))
        return {'douyin_auth':'待达人确认' if self.pending else '授权生效','toufang_auth':'待达人确认' if self.pending else '授权生效'}
    def ensure_authorization(self,creator,code):
        checked=self.auth_check(creator)
        if checked.get('authorized'):return checked
        applied=self.authorize(creator,code)
        return {**applied,'authorized':applied.get('douyin_auth')=='授权生效' and applied.get('toufang_auth')=='授权生效'}
    def bind(self,creator,route):
        self.calls.append(('bind',creator,route.plan_id))
        self.rows[route.plan_id]=[r for r in self.rows[route.plan_id] if r['creator_id']!=creator]+[binding(route,creator)]
        if self.add_then_timeout:raise TimeoutError('saved but response lost')
        return {'scan':self.scan(route),'outcome':'added','binding':binding(route,creator)}
    def ensure_materials(self,creator,route,*,before_submit=None):
        self.calls.append(('materials',creator,route.plan_id))
        if self.material_error:raise self.material_error
        key=(route.plan_id,creator)
        if not self.materials.get(key) and not self.material_wait:
            if before_submit:before_submit()
            self.calls.append(('material_submit',creator,route.plan_id))
            self.materials[key]=['video-'+route.product_id]
            if self.material_response_lost:raise TimeoutError('material saved but response lost')
        ids=self.materials.get(key,[])
        return {'account_id':'wrong' if self.material_wrong_target else route.account_id,'creator_id':creator,'product_id':route.product_id,'plan_id':route.plan_id,'verified':bool(ids),'material_ids':ids,'detail':'视频复核通过' if ids else '该商品没有可用视频'}
    def count(self,name):return sum(c[0]==name for c in self.calls)

class Base(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.old_db=store.DB_FILE;store.DB_FILE=Path(self.temp.name)/'db.sqlite'
        self.addCleanup(setattr,store,'DB_FILE',self.old_db)
        self.platform=Platform();self.workflow=flow.Workflow(self.platform)

class RoutingTests(Base):
    def test_four_product_routes(self):
        self.assertEqual(len({r.plan_id for r in MAIN_PRODUCTS}),4)
        for route in MAIN_PRODUCTS:self.assertEqual(product_route(route.product_id),route)
    def test_dirty_numeric_product_normalization(self):
        self.assertEqual(product_route('3,625226808611490412 '),A)
    def test_unknown_product_never_calls_platform(self):
        s=simple();s['商品ID']='unknown'
        self.assertEqual(self.workflow.execute(s)['progress'],'待人工处理');self.assertEqual(self.platform.calls,[])
    def test_name_alone_is_not_product_routing(self):self.assertIsNone(product_route('肥牛'))
    def test_identity_valid(self):
        check_identity(f'https://qianchuan.jinritemai.com/x?aavid={MAIN_AAVID}&adId={A.plan_id}',f'鲜会鲜 ID：{MAIN_AAVID}\n ID：{A.plan_id}',plan_id=A.plan_id)
    def test_wrong_account_is_rejected(self):
        with self.assertRaises(RuntimeError):check_identity('https://x/?aavid=bad',f'ID：{MAIN_AAVID}')
    def test_url_account_alone_is_insufficient(self):
        with self.assertRaises(RuntimeError):check_identity(f'https://x/?aavid={MAIN_AAVID}',f'ID：wrong ID：{MAIN_AAVID}')
    def test_wrong_plan_is_rejected(self):
        with self.assertRaises(RuntimeError):check_identity(f'https://x/?aavid={MAIN_AAVID}&adId={B.plan_id}',f'ID：{MAIN_AAVID} ID：{A.plan_id}',plan_id=A.plan_id)

class WorkflowTests(Base):
    def test_auto_pull_closes_without_authorization(self):
        self.platform.rows[A.plan_id]=[binding(kind='自动拉取')]
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已拉取')
        self.assertEqual(self.platform.count('authorize'),0);self.assertEqual(self.platform.count('bind'),0)
    def test_existing_manual_binding_closes_as_joined(self):
        self.platform.rows[A.plan_id]=[binding()]
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入');self.assertEqual(self.platform.count('probe'),0)
    def test_deleted_binding_is_readded(self):
        self.platform.rows[A.plan_id]=[binding(status='已删除')]
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入');self.assertEqual(self.platform.count('bind'),1)
    def test_matrix_origin_still_uses_main_flow(self):
        self.platform.authorized=False
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入');self.assertEqual(self.platform.count('authorize'),1)
    def test_same_creator_different_products_are_distinct(self):
        self.platform.rows[A.plan_id]=[binding(kind='自动拉取')]
        self.workflow.execute(simple());self.workflow.execute(simple(route=B))
        self.assertEqual(self.platform.count('bind'),1)
        self.assertIn(('bind','creator01',B.plan_id),self.platform.calls)
    def test_cross_table_same_product_once(self):
        self.workflow.execute(simple());self.workflow.execute(simple(**{'业务部':'业务2部'}))
        self.assertEqual(self.platform.count('bind'),1)
    def test_partial_authorization_waits_without_failure(self):
        self.platform.authorized=False;self.platform.pending=True
        r=self.workflow.execute(simple());self.assertEqual(r['progress'],'等待达人接受')
        self.assertEqual(self.platform.count('bind'),0)
    def test_already_effective_authorization_not_resubmitted(self):
        self.workflow.execute(simple());self.assertEqual(self.platform.count('authorize'),0)
    def test_expired_authorization_is_reapplied(self):
        self.platform.authorized=False
        self.workflow.execute(simple());self.assertEqual(self.platform.count('authorize'),1)
    def test_commission_over_limit_is_checked_after_authorization(self):
        self.platform.rate=15.0;self.platform.authorized=False
        outcome=self.workflow.execute(simple())
        self.assertEqual(outcome['progress'],'佣金过高');self.assertTrue(outcome['auth']['authorized'])
        self.assertEqual(self.platform.count('authorize'),1);self.assertEqual(self.platform.count('bind'),0)
        calls=[c[0] for c in self.platform.calls]
        self.assertLess(calls.index('authorize'),len(calls)-1)
        self.assertEqual(calls[-1],'probe')
    def test_high_display_commission_does_not_block_pending_authorization(self):
        self.platform.rate=15.0;self.platform.authorized=False;self.platform.pending=True
        outcome=self.workflow.execute(simple())
        self.assertEqual(outcome['progress'],'等待达人接受')
        self.assertEqual(self.platform.count('authorize'),1);self.assertEqual(self.platform.count('bind'),0)
        self.assertEqual(self.platform.count('probe'),1)
    def test_effective_commission_is_reread_after_authorization(self):
        self.platform.rate=15.0;self.platform.authorized=False
        original=self.platform.ensure_authorization
        def authorize(creator,code):
            auth=original(creator,code);self.platform.rate=5.0;return auth
        self.platform.ensure_authorization=authorize
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入')
        self.assertEqual(self.platform.count('authorize'),1);self.assertEqual(self.platform.count('bind'),1)
        self.assertEqual(self.platform.count('probe'),2)
    def test_exact_commission_limit_is_allowed(self):
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入')
    def test_unknown_commission_keeps_existing_allow_policy(self):
        self.platform.rate=None;self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入')
    def test_stop_remark_preserved(self):
        r=self.workflow.execute(simple(**{'备注':'停止投流'}));self.assertEqual(r['progress'],'待人工处理');self.assertEqual(self.platform.calls,[])
    def test_duplicate_stop_veto(self):
        self.workflow.denied[binding_key('creator01',A)]='人工停投'
        self.assertEqual(self.workflow.execute(simple())['progress'],'待人工处理');self.assertEqual(self.platform.calls,[])
    def test_incomplete_scan_blocks_authorization(self):
        self.platform.complete=False
        self.assertEqual(self.workflow.execute(simple())['progress'],'待主号授权');self.assertEqual(self.platform.count('probe'),0)
    def test_wrong_account_scan_blocks(self):
        self.platform.invalid_account=True
        self.workflow.execute(simple());self.assertEqual(self.platform.count('authorize'),0);self.assertEqual(self.platform.count('bind'),0)
    def test_autopull_between_probe_and_auth(self):
        def update(p,r):
            if p.count('scan')==2:p.rows[r.plan_id]=[binding(r,kind='自动拉取')]
        self.platform.on_scan=update
        self.assertEqual(self.workflow.execute(simple())['progress'],'主号已拉取');self.assertEqual(self.platform.count('check'),0)
    def test_saved_response_lost_does_not_bind_again(self):
        self.platform.add_then_timeout=True
        self.assertEqual(self.workflow.execute(simple())['progress'],'待加入主品')
        self.platform.add_then_timeout=False
        second=flow.Workflow(self.platform).execute(simple())
        self.assertEqual(second['progress'],'主号已加入');self.assertEqual(self.platform.count('bind'),1)
    def test_future_delivery_authorizes_but_does_not_add(self):
        r=self.workflow.execute(simple(),date.today()+timedelta(days=1));self.assertEqual(r['progress'],'待加入主品');self.assertEqual(self.platform.count('bind'),0)
    def test_live_eligibility_change_prevents_mutation(self):
        cb=Mock(side_effect=RuntimeError('人工停投'))
        self.workflow.execute(simple(),still_eligible=cb)
        self.assertEqual(self.platform.count('authorize'),0);self.assertEqual(self.platform.count('bind'),0)
    def test_old_department_checkpoint_never_implies_main_completion(self):
        self.workflow.execute(simple(**{'授权成功':'ok','建计划成功':'ok'}))
        self.assertEqual(self.platform.count('check'),1);self.assertEqual(self.platform.count('bind'),1)

class MaterialWorkflowTests(Base):
    def test_already_pulled_creator_still_checks_materials(self):
        self.platform.rows[A.plan_id]=[binding(kind='自动拉取')]
        r=self.workflow.execute(simple())
        self.assertEqual(r['progress'],'主号已拉取');self.assertTrue(r['materials']['verified'])
        self.assertEqual(self.platform.count('materials'),1);self.assertEqual(self.platform.count('authorize'),0)
    def test_missing_video_is_not_a_completed_binding(self):
        self.platform.rows[A.plan_id]=[binding(kind='自动拉取')];self.platform.material_wait=True
        r=self.workflow.execute(simple(progress='主号已拉取'))
        self.assertEqual(r['progress'],'待加入素材');self.assertEqual(r['outcome'],'wait')
        self.assertEqual(self.platform.count('bind'),0)
    def test_existing_material_does_not_submit_again(self):
        self.platform.rows[A.plan_id]=[binding()];self.platform.materials[(A.plan_id,'creator01')]=['existing']
        r=self.workflow.execute(simple());self.assertEqual(r['materials']['material_ids'],['existing'])
        self.assertEqual(self.platform.count('material_submit'),0)
    def test_materials_are_product_specific(self):
        self.workflow.execute(simple());self.workflow.execute(simple(route=B))
        self.assertEqual(self.platform.count('material_submit'),2)
    def test_cross_table_materials_are_only_added_once(self):
        self.workflow.execute(simple());self.workflow.execute(simple(**{'业务部':'业务2部'}))
        self.assertEqual(self.platform.count('material_submit'),1)
    def test_material_read_failure_does_not_authorize_or_mark_done(self):
        self.platform.rows[A.plan_id]=[binding()];self.platform.material_error=TimeoutError('material list loading')
        r=self.workflow.execute(simple(progress='主号已加入'))
        self.assertEqual(r['progress'],'待加入素材');self.assertEqual(self.platform.count('authorize'),0)
    def test_material_response_loss_rechecks_before_retry(self):
        self.platform.material_response_lost=True
        self.assertEqual(self.workflow.execute(simple())['progress'],'待加入素材')
        self.platform.material_response_lost=False
        r=flow.Workflow(self.platform).execute(simple(progress='待加入素材'))
        self.assertEqual(r['progress'],'主号已加入');self.assertEqual(self.platform.count('material_submit'),1)
    def test_materials_wrong_target_never_close_record(self):
        self.platform.material_wrong_target=True
        self.assertEqual(self.workflow.execute(simple())['progress'],'待加入素材')
    def test_bound_future_record_does_not_add_materials(self):
        self.platform.rows[A.plan_id]=[binding()]
        r=self.workflow.execute(simple(),date.today()+timedelta(days=1))
        self.assertEqual(r['progress'],'待加入素材');self.assertEqual(self.platform.count('materials'),0)
    def test_material_evidence_is_persisted(self):
        self.workflow.execute(simple())
        self.assertTrue(store.workflow('creator01',A)['materials']['verified'])
    def test_old_sync_checkpoint_does_not_skip_materials(self):
        store.stage_record('main','r1','creator01',A,flow.bound_result(binding()))
        with closing(store.connect()) as c, c:c.execute("UPDATE main_product_record_sync SET version='main-products-v1'")
        self.assertEqual(store.pending_sync('main'),[])

class StoreTests(Base):
    def test_product_specific_snapshot_keys(self):
        store.save_scan(A,[binding()],complete=True);store.save_scan(B,[binding(B)],complete=True)
        self.assertEqual(len(store.load_binding_map()),2)
    def test_empty_complete_scan_only_replaces_its_product(self):
        store.save_scan(A,[binding()],complete=True);store.save_scan(B,[binding(B)],complete=True)
        store.save_scan(A,[],complete=True);self.assertEqual(set(store.load_binding_map()),{binding_key('creator01',B)})
    def test_incomplete_snapshot_is_not_written(self):
        with self.assertRaises(ValueError):store.save_scan(A,[],complete=False)
        self.assertFalse(store.DB_FILE.exists())
    def test_invalid_target_snapshot_is_not_written(self):
        with self.assertRaises(ValueError):store.save_scan(A,[binding(B)],complete=True)
    def test_read_only_missing_database_does_not_create_file(self):
        self.assertEqual(store.load_binding_map(),{});self.assertFalse(store.DB_FILE.exists())
    def test_workflow_version_and_target_are_required(self):
        store.save_result('creator01',A,flow.result('主号已加入','ok','复核'))
        self.assertIsNone(store.workflow('creator01',B));self.assertEqual(store.workflow('creator01',A)['progress'],'主号已加入')

class Client:
    def __init__(self,sheets):
        self.cfg={'sheet_id':'main','old_material_sheet_id':'old'};self.rows={s['sheet_id']:copy.deepcopy(s['simple']) for s in sheets};self.fail_write=False;self.writes=[]
    def get_records(self,record_ids):
        return [{'record_id':rid,'values':self.rows[self.cfg['sheet_id']][rid]} for rid in record_ids if rid in self.rows[self.cfg['sheet_id']]]
    def update_records(self,records):
        if self.fail_write:raise RuntimeError('simulated sheet outage')
        for record in records:
            rid=record['record_id'];self.writes.append((self.cfg['sheet_id'],rid))
            self.rows[self.cfg['sheet_id']][rid].update(simplify_record(record))

def sheet(sid,rid='r1',record=None):
    return {'sheet_id':sid,'name':'抖音任务' if sid=='main' else '旧素材抖音任务','records':[], 'simple':{rid:record or simple()}}

class BatchTests(Base):
    def setup_batch(self,sheets,phase='active'):
        self.client=Client(sheets)
        with patch.object(flow,'load_sheets',return_value=sheets):self.batch=flow.Batch(self.client.cfg,self.workflow)
        self.batch.rollout={'phase':phase,'canary_record_ids':['r1']}
        self.state={};self.args=SimpleNamespace(retry=None,force=False,dry_run=False,main_limit=0)
        self.poll=SimpleNamespace(wait_report_idle=Mock(),plan_power_busy=lambda:False,report_busy=lambda:False,
                                 write_status=Mock(),_scheduled_delivery_date=lambda s:None,write_plan_registry=Mock(),save_state=Mock(),retry_interval_seconds=lambda:1800)
    def run_sheet(self,item):return self.batch.process_sheet(self.client,item,self.state,self.args,self.poll)
    def test_cross_table_dedup_and_correct_sheet_writeback(self):
        main,old=sheet('main'),sheet('old','r2');self.setup_batch([main,old])
        self.run_sheet(main);self.run_sheet(old)
        self.assertEqual(self.platform.count('bind'),1);self.assertEqual(self.client.writes,[('main','r1'),('old','r2')])
    def test_failed_writeback_only_retries_sync(self):
        main=sheet('main');self.setup_batch([main]);self.client.fail_write=True
        self.run_sheet(main);self.assertEqual(len(store.pending_sync('main')),1)
        self.client.fail_write=False
        second=flow.Workflow(Platform());self.batch.workflow=second
        self.run_sheet(main)
        self.assertEqual(second.gateway.calls,[]);self.assertEqual(store.pending_sync('main'),[])
    def test_preview_phase_never_touches_platform_or_sheet(self):
        main=sheet('main');self.setup_batch([main],'preview');self.run_sheet(main)
        self.assertEqual(self.platform.calls,[]);self.assertEqual(self.client.writes,[]);self.assertFalse(store.DB_FILE.exists())
    def test_canary_restricts_records(self):
        main=sheet('main');main['simple']['r2']=simple('creator02');self.setup_batch([main],'canary');self.run_sheet(main)
        self.assertEqual(self.client.writes,[('main','r1')])
    def test_historical_department_done_never_reopened(self):
        main=sheet('main',record=simple(progress='已投放'));self.setup_batch([main]);self.args.force=True;self.run_sheet(main)
        self.assertEqual(self.platform.calls,[])
    def test_one_record_exception_does_not_block_next(self):
        main=sheet('main');main['simple']['r2']=simple('creator02');self.setup_batch([main])
        original=store.stage_record
        def stage(sid,rid,*rest):
            if rid=='r1':raise RuntimeError('record-specific persistence failure')
            return original(sid,rid,*rest)
        with patch.object(store,'stage_record',side_effect=stage):self.run_sheet(main)
        self.assertIn(('main','r2'),self.client.writes)
    def test_stale_table_record_changed_to_done_is_skipped(self):
        main=sheet('main');self.setup_batch([main]);self.client.rows['main']['r1']['进度']='已投放';self.run_sheet(main)
        self.assertEqual(self.platform.calls,[])
    def test_waiting_writeback_failure_is_sync_only(self):
        main=sheet('main');self.setup_batch([main]);self.client.fail_write=True
        self.platform.authorized=False;self.platform.pending=True
        self.run_sheet(main)
        self.client.fail_write=False
        second=flow.Workflow(Platform());self.batch.workflow=second
        self.run_sheet(main)
        self.assertEqual(second.gateway.calls,[])
        self.assertEqual(self.client.rows['main']['r1']['进度'],'等待达人接受')
        self.assertEqual(store.pending_sync('main'),[])
    def test_report_busy_prevents_operations(self):
        main=sheet('main');self.setup_batch([main]);self.poll.report_busy=lambda:True;self.run_sheet(main)
        self.assertEqual(self.platform.calls,[])
        self.poll.wait_report_idle.assert_not_called()

class PendingPermissionTests(Base):
    def test_platform_requires_douyin_confirmation_is_waiting(self):
        self.platform.ensure_authorization=Mock(side_effect=RuntimeError('投放授权提交被平台置灰：抖音号授权仍「待达人确认」，待达人确认后自动重试'))
        outcome=self.workflow.execute(simple())
        self.assertEqual(outcome['progress'],'等待达人接受')
        self.assertEqual(outcome['outcome'],'wait')
        self.assertEqual(self.platform.count('bind'),0)

class MainTemporaryPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_existing_eight_tabs_and_allows_only_one_owned_page(self):
        from 巨量千川 import session_prepare as session
        class Page:
            def __init__(self):self.closed=False;self.listeners={}
            def is_closed(self):return self.closed
            def on(self,event,callback):self.listeners[event]=callback
            async def close(self):
                self.closed=True
                if 'close' in self.listeners:self.listeners['close']()
        class Context:
            def __init__(self):self.pages=[Page() for _ in range(8)]
            async def new_page(self):
                page=Page();self.pages.append(page);return page
        context=Context();original=list(context.pages)
        with patch.object(session,'main_browser_lock_owned',return_value=True),patch.object(session,'_main_owned_page',None):
            owned=await session.open_main_account_page(context)
            self.assertTrue(all(not p.is_closed() for p in original))
            with self.assertRaises(session.SessionPrepareError):await session.open_main_account_page(context)
            await session.close_main_account_page()
            self.assertTrue(owned.is_closed())
            again=await session.open_main_account_page(context)
            await again.close()
        self.assertTrue(all(not p.is_closed() for p in original))
    async def test_no_shared_lock_never_opens_page(self):
        from unittest.mock import AsyncMock
        from 巨量千川 import session_prepare as session
        context=SimpleNamespace(new_page=AsyncMock())
        with patch.object(session,'main_browser_lock_owned',return_value=False):
            with self.assertRaises(session.SessionPrepareError):await session.open_main_account_page(context)
        context.new_page.assert_not_called()

class LockOwnershipTests(unittest.TestCase):
    def test_heartbeat_only_refreshes_owned_marker(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'lock'
            marker.write_text(json.dumps({'pid':123}),encoding='utf-8')
            with patch.object(flow.os,'utime') as touch:
                self.assertFalse(flow.refresh_owned_lock(marker,456))
                touch.assert_not_called()
                self.assertTrue(flow.refresh_owned_lock(marker,123))
                touch.assert_called_once_with(marker,None)
            self.assertEqual(json.loads(marker.read_text())['pid'],123)
    def test_missing_marker_is_never_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'lock'
            self.assertFalse(flow.refresh_owned_lock(marker,123))
            self.assertFalse(marker.exists())

class MainPullMonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 主号监控的旧 stdout 包装只在真实 CLI 使用，测试不替换测试运行器流。
        with patch.object(sys, 'stdout', None):
            import main_pull_daily
        cls.daily=main_pull_daily
    def test_shared_guard_covers_entire_probe_process(self):
        from contextlib import contextmanager
        held=[]
        @contextmanager
        def guard():
            held.append(True)
            try:yield
            finally:held.pop()
        def probe(*args,**kwargs):
            self.assertEqual(held,[True])
            return SimpleNamespace(returncode=0,stdout='{"rows":[],"complete":true}',stderr='')
        with patch.object(flow,'browser_guard',guard), patch.object(self.daily.subprocess,'run',side_effect=probe) as run:
            self.assertTrue(self.daily.run_scout()['complete'])
            run.assert_called_once()
        self.assertEqual(held,[])
    def test_busy_browser_never_starts_probe(self):
        with patch.object(flow,'browser_guard',side_effect=RuntimeError('browser busy')), patch.object(self.daily.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):self.daily.run_scout()
            run.assert_not_called()

class FakeBody:
    def __init__(self,page):self.page=page
    async def inner_text(self):
        p=self.page
        return f'鲜会鲜 ID：{MAIN_AAVID}\n计划 ID：{A.plan_id}\n商品 ID：{p.product_id}\n共 {p.total} 条记录'
class FakeNext:
    def __init__(self,page):self.page=page
    async def is_visible(self):return True
    async def evaluate(self,code):return self.page.index==len(self.page.pages)-1
    async def click(self,**kw):
        if not self.page.stall:self.page.index+=1
class FakeNexts:
    def __init__(self,page):self.page=page
    async def count(self):return 0 if self.page.missing_next else 1
    def nth(self,i):return FakeNext(self.page)
class FakePage:
    def __init__(self,n=1):
        self.pages=[[f'达人{i} | 自动拉取 | ID：creator{i} | 商品 | ID：{A.product_id}'] for i in range(n)]
        self.total=n;self.index=0;self.product_id=A.product_id;self.stall=False;self.missing_next=False
        self.url=f'https://qianchuan.jinritemai.com/uni-prom/setting?aavid={MAIN_AAVID}&adId={A.plan_id}'
    async def goto(self,*args,**kwargs):self.index=0
    async def wait_for_timeout(self,*args):pass
    async def wait_for_function(self,*args,**kwargs):pass
    async def evaluate(self,code,*args):
        if 'table tbody tr' in code:return self.pages[self.index]
        return True
    def locator(self,sel):return FakeBody(self) if sel=='body' else FakeNexts(self)

class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_more_than_fifteen_pages(self):
        from 巨量千川.main_pull_scout import read_target
        scan=await read_target(FakePage(19),A)
        self.assertTrue(scan['complete']);self.assertEqual(len(scan['rows']),19)
    async def test_missing_next_with_incomplete_count_blocks(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage(2);p.missing_next=True
        with self.assertRaisesRegex(RuntimeError,'分页不完整'):await read_target(p,A)
    async def test_stuck_next_page_blocks(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage(2);p.stall=True
        with self.assertRaisesRegex(RuntimeError,'未前进'):await read_target(p,A)
    async def test_wrong_product_blocks(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage();p.product_id=B.product_id
        with self.assertRaisesRegex(RuntimeError,'商品校验失败'):await read_target(p,A)
    async def test_unparseable_creator_is_not_silently_dropped(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage();p.pages=[[f'商品 ID：{A.product_id}']]
        with self.assertRaisesRegex(RuntimeError,'无法识别'):await read_target(p,A)

class DeletedRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_row_without_product_is_a_missing_relation(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage();p.pages=[['已删除 | 测试达人 | 自动拉取 | ID：creator01 | -']]
        scan=await read_target(p,A)
        self.assertEqual(scan['rows'][0]['bind_status'],'已删除')
        self.assertTrue(scan['complete'])
    async def test_active_row_without_product_still_blocks(self):
        from 巨量千川.main_pull_scout import read_target
        p=FakePage();p.pages=[['测试达人 | 自动拉取 | ID：creator01 | -']]
        with self.assertRaisesRegex(RuntimeError,'商品与目标不符'):await read_target(p,A)

class ManualRestrictionTests(Base):
    def test_active_stop_name_prevents_main_authorization(self):
        original=self.platform.probe
        def probe(cid,route):return {**original(cid,route),'creator_name':'保留停投达人'}
        self.platform.probe=probe
        with patch('巨量千川.manual_whitelist._load_entries',return_value=[{'name':'保留停投达人','reason':'人工干预，暂不投放'}]):
            outcome=self.workflow.execute(simple())
        self.assertEqual(outcome['progress'],'待人工处理')
        self.assertEqual(self.platform.count('authorize'),0);self.assertEqual(self.platform.count('bind'),0)
    def test_nonstop_whitelist_does_not_block_new_workflow(self):
        with patch('巨量千川.manual_whitelist._load_entries',return_value=[{'name':'creator01','reason':'允许新素材'}]):
            self.assertEqual(self.workflow.execute(simple())['progress'],'主号已加入')




class ManualRetryProtectionTests(Base):
    def test_stopped_sheet_name_is_protected_even_when_already_bound(self):
        self.platform.rows[A.plan_id]=[binding()]
        with patch('巨量千川.manual_whitelist._load_entries',return_value=[{'name':'明确停投达人','reason':'暂不投放'}]):
            outcome=self.workflow.execute(simple(progress='待人工处理',达人名称='明确停投达人'))
        self.assertEqual(outcome['progress'],'待人工处理')
        self.assertEqual(self.platform.count('materials'),0)
        self.assertEqual(self.platform.count('bind'),0)
    def test_failed_scan_preserves_manual_status_instead_of_guessing_auth(self):
        self.platform.complete=False
        outcome=self.workflow.execute(simple(progress='待人工处理'))
        self.assertEqual(outcome['progress'],'待人工处理')
        self.assertEqual(outcome['outcome'],'blocked')
        self.assertEqual(self.platform.count('authorize'),0)

if __name__=='__main__':unittest.main(verbosity=2)
