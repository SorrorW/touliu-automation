"""表格达人→主号四主品。旧分部断点从不参与此流程。"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, date
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import threading

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent/'投流')]
from 巨量千川.main_products import (MAIN_AAVID, MAIN_ACCOUNT, MAIN_PRODUCTS,
    WORKFLOW_VERSION, binding_key, product_route, clean_id)
from 巨量千川 import main_product_store as store
from 巨量千川.commission_gate import is_commission_blocked, load_commission_max_pct

log = logging.getLogger(__name__)
ROLLOUT_FILE = HERE/'main_authorization_rollout.json'
PENDING = {'','待处理','待授权','允许授权','等待达人接受','可投放','待主号授权',
           '待加入主品','待加入素材','主号已拉取','主号已加入','等待21点进行授权','待主账号拉取',
           '等待23点建计划','等待23点创建'}
HISTORIC_DONE = {'已投放','已完成'}
BOUND = {'主号已拉取','主号已加入'}
STOP_PATTERN = re.compile(r'停止投流|停止投放|暂停投流|暂停投放|取消投放|先不投放|暂不投放|不要投放|不再投放|不予投放|禁止投放|先别开|先别投|不投流')

def stop_reason(simple):
    for field in ('备注','投放要求','状态说明'):
        text = str(simple.get(field) or '')
        if STOP_PATTERN.search(text):
            return '人工停投要求：'+text[:180]
    if str(simple.get('是否投放','')).strip() in ('否','不投放','停投'):
        return '人工设置不投放'
    if str(simple.get('进度','')).strip() in ('停止投流','停止投放','已取消','不投放','停投'):
        return '人工停投状态'
    return ''

def manual_creator_stop(creator, creator_name=""):
    """只继承明确的停投/不跟进要求，不把普通白名单的允许新建策略变成停投。"""
    from 巨量千川.manual_whitelist import _load_entries, _clean_plan_name
    targets={_clean_plan_name(creator),_clean_plan_name(creator_name)}-{''}
    for entry in _load_entries():
        reason=str(entry.get('reason') or entry.get('remark') or '')
        if not (STOP_PATTERN.search(reason) or '程序不跟进' in reason):continue
        names={_clean_plan_name(entry.get('name')),_clean_plan_name(entry.get('creator_id'))}
        names.update(_clean_plan_name(x) for x in entry.get('aliases',[]))
        if targets & (names-{''}):return '现有人工限制：'+reason[:200]
    return ''

def is_candidate(simple, *, retry=False):
    progress=str(simple.get('进度') or '').strip()
    if progress in HISTORIC_DONE:
        return False
    return progress in PENDING or retry

def result(progress, detail, step, **extra):
    return {'progress':progress,'detail':detail,'step':step, **extra}

def bound_result(binding):
    auto = binding.get('bind_type')=='自动拉取'
    return result('主号已拉取' if auto else '主号已加入',
                  '对应主品已有有效绑定（'+('自动拉取' if auto else '手动加入')+'）',
                  '主品绑定复核', binding=binding, verified_at=time.time(), outcome='bound')

class Gateway:
    """每个操作使用短生命周期连接，不等待达人点击确认。"""
    def scan(self, route):
        from 巨量千川.main_product_bind import scan
        return asyncio.run(scan(route))
    def probe(self, creator, route):
        from 巨量千川.main_product_bind import probe
        return asyncio.run(probe(creator, route))
    def auth_check(self, creator):
        from 巨量千川.auth import check_authorization
        return asyncio.run(check_authorization(creator, MAIN_ACCOUNT))
    def authorize(self, creator, code):
        from 巨量千川.auth import authorize_creator
        return asyncio.run(authorize_creator(creator, code, MAIN_ACCOUNT, 'business'))
    def ensure_authorization(self, creator, code):
        # authorize_creator 本身先逐项核验并跳过已生效/待确认的权限，不能再查一遍。
        applied=self.authorize(creator,code)
        return {'authorized':applied.get('douyin_auth')=='授权生效' and applied.get('toufang_auth')=='授权生效',
                'douyin_auth':applied.get('douyin_auth'),'toufang_auth':applied.get('toufang_auth')}
    def bind(self, creator, route):
        from 巨量千川.main_product_bind import bind
        return asyncio.run(bind(creator, route))
    def ensure_materials(self, creator, route, *, before_submit=None):
        from 巨量千川.main_product_materials import ensure_materials
        return asyncio.run(ensure_materials(creator, route, before_submit=before_submit))

class Workflow:
    def __init__(self, gateway=None, persistence=store):
        self.gateway=gateway or Gateway()
        self.store=persistence
        self.scans={}
        self.results={}
        self.denied={}
    def scan(self, route, *, refresh=False):
        if refresh or route.plan_id not in self.scans:
            scan=self.gateway.scan(route)
            if (scan.get('account_id'),scan.get('product_id'),scan.get('plan_id')) != (route.account_id,route.product_id,route.plan_id):
                raise RuntimeError('扫描目标账号/商品/计划不符')
            self.store.validate_scan(route,scan.get('rows',[]),scan.get('complete',False))
            self.scans[route.plan_id]=scan
        return self.scans[route.plan_id]
    def hit(self, creator, route, *, refresh=False):
        scan=self.scan(route,refresh=refresh)
        return next((r for r in scan['rows'] if r['creator_id']==creator and r['bind_status']=='在投'),None)
    def execute(self, simple, scheduled=None, still_eligible=None):
        creator=clean_id(simple.get('抖音号'))
        route=product_route(simple.get('商品ID'))
        if route is None:
            return result('待人工处理','非四主品商品，不进入分部授权或建计划','商品路由')
        key=binding_key(creator,route)
        creator_name=str(simple.get('达人名称') or simple.get('达人昵称') or simple.get('达人') or '')
        reason=stop_reason(simple) or self.denied.get(key,'') or manual_creator_stop(creator,creator_name)
        if reason:
            return result('待人工处理',reason,'人工停投保护')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',creator):
            return result('待人工处理','抖音号为空或格式异常','达人校验')
        if key in self.results:
            return self.results[key]
        step='主品完整扫描'
        try:
            hit=self.hit(creator,route)
            if hit:
                outcome=bound_result(hit)
            else:
                step='达人身份核验'
                probe=self.gateway.probe(creator,route)
                if (probe.get('account_id'),probe.get('product_id'),probe.get('plan_id')) != (route.account_id,route.product_id,route.plan_id):
                    raise RuntimeError('达人身份探测目标不符')
                manual=manual_creator_stop(creator,probe.get('creator_name',''))
                if manual:
                    outcome=result('待人工处理',manual,'人工停投保护')
                else:
                    # Refresh before authorization as the platform can auto-pull at any time.
                    hit=self.hit(creator,route,refresh=True)
                    if hit:
                        outcome=bound_result(hit)
                    else:
                        if still_eligible:
                            still_eligible()
                        step='主号授权'
                        self.store.event(creator,route,step,'开始','核验主号合作达人两项权限，已生效或待确认的权限跳过')
                        auth=self.gateway.ensure_authorization(creator,str(simple.get('合作码') or '').strip().strip(',，'))
                        if not auth.get('authorized'):
                            # Even partial/pending requests release the browser and resume next poll.
                            outcome=result('等待达人接受','抖音号：'+str(auth.get('douyin_auth'))+'；商品投放：'+str(auth.get('toufang_auth')),step,auth=auth)
                        elif scheduled and scheduled>date.today():
                            outcome=result('待加入主品','授权已生效，等待预约投放日期 '+scheduled.isoformat(),'预约投放',auth=auth)
                        else:
                            if still_eligible:
                                still_eligible()
                            step='佣金检查'
                            # 授权前的展示佣金不能阻断申请，也不能替代授权生效后的实时结果。
                            fresh_probe=self.gateway.probe(creator,route)
                            if (fresh_probe.get('account_id'),fresh_probe.get('product_id'),fresh_probe.get('plan_id')) != (route.account_id,route.product_id,route.plan_id):
                                raise RuntimeError('授权后佣金探测目标不符')
                            if is_commission_blocked(fresh_probe.get('commission_pct'),load_commission_max_pct()):
                                outcome=result('佣金过高',f"授权已生效；当前佣金率{fresh_probe['commission_pct']}%超过现有上限",step,auth=auth)
                            else:
                                step='加入主品'
                                self.store.event(creator,route,step,'开始','提交前再次重读对应计划，防止重复加入')
                                added=self.gateway.bind(creator,route)
                                scan=added['scan']
                                self.store.validate_scan(route,scan['rows'],scan.get('complete',False))
                                if (scan.get('account_id'),scan.get('product_id'),scan.get('plan_id')) != (route.account_id,route.product_id,route.plan_id):
                                    raise RuntimeError('绑定复核目标不符')
                                self.scans[route.plan_id]=scan
                                binding=next((r for r in scan['rows'] if r['creator_id']==creator and r['bind_status']=='在投'),None)
                                if not binding:
                                    raise RuntimeError('未核实有效绑定')
                                outcome=bound_result(binding)
                                outcome.update(outcome=added.get('outcome','added'),detail='主号已加入对应现有主品计划，完整分页复核通过')
            if outcome['progress'] in BOUND:
                step='主品素材核验'
                if scheduled and scheduled>date.today():
                    outcome=result('待加入素材','抖音号已绑定，等待预约日期 '+scheduled.isoformat()+' 后加入视频素材',step,binding=outcome.get('binding'),outcome='wait')
                else:
                    self.store.event(creator,route,step,'开始','先核验该达人在本商品、本计划的视频；缺失才添加最新可用视频')
                    materials=self.gateway.ensure_materials(creator,route,before_submit=still_eligible)
                    actual=(materials.get('account_id'),materials.get('creator_id'),materials.get('product_id'),materials.get('plan_id'))
                    if actual!=binding_key(creator,route):
                        raise RuntimeError('素材复核账户/达人/商品/计划不符')
                    if materials.get('verified') and materials.get('material_ids'):
                        outcome.update(step=step,materials=materials,verified_at=materials.get('verified_at',time.time()),detail=outcome['detail']+'；'+materials.get('detail','对应商品视频素材复核通过'))
                    else:
                        outcome=result('待加入素材',materials.get('detail','抖音号已绑定，尚未核实视频素材已加入'),step,binding=outcome.get('binding'),materials=materials,outcome='wait')
            scan=self.scans.get(route.plan_id)
            if scan:
                self.store.save_scan(route,scan['rows'],complete=True)
            self.store.save_result(creator,route,outcome)
            self.store.event(creator,route,outcome['step'],outcome['progress'],outcome['detail'])
        except Exception as exc:
            detail=f'{type(exc).__name__}: {exc}'[:1000]
            if '合作码错误' in detail:
                detail='平台拒绝：合作码错误，请业务员更新表格合作码后重试'
            # Result unknown is never a business failure or a completion checkpoint.
            progress='待加入素材' if step=='主品素材核验' else ('待加入主品' if step=='加入主品' else '待主号授权')
            if step=='主品完整扫描' and simple.get('进度') in BOUND|{'待人工处理'}:
                progress=simple['进度']  # 扫描未知不能推断缺少授权，也不覆盖原有处理状态
            if any(x in detail for x in ('平台限制','平台预检','被拒','合作码','用户不存在','权限')):
                progress='待人工处理'
            if '佣金过高' in detail:
                progress='佣金过高'
            waiting=(step=='主号授权' and any(t in detail for t in ('待达人确认后','抖音号授权仍「待达人确认','抖音号授权仍「待确认')) and '合作码错误' not in detail)
            if waiting:progress='等待达人接受'
            outcome=result(progress,detail,step,outcome='wait' if waiting else 'blocked')
            self.store.save_result(creator,route,outcome)
            self.store.event(creator,route,step,progress,detail)
        self.results[key]=outcome
        return outcome

def load_sheets(cfg):
    from wecom_client import WeComClient, simplify_record
    sheets=[]
    for key,name in (('sheet_id','抖音任务'),('old_material_sheet_id','旧素材抖音任务')):
        if not cfg.get(key):continue
        c=WeComClient(dict(cfg));c.cfg['sheet_id']=cfg[key]
        records=c.get_all_records()
        sheets.append({'sheet_id':cfg[key],'name':name,'records':records,
                       'simple':{r['record_id']:simplify_record(r) or {} for r in records}})
    return sheets

class Batch:
    def __init__(self, cfg, workflow=None):
        self.sheets=load_sheets(dict(cfg))
        self.workflow=workflow or Workflow()
        try:self.rollout=json.loads(ROLLOUT_FILE.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.rollout={'phase':'preview'}
        for sheet in self.sheets:
            for simple in sheet['simple'].values():
                route=product_route(simple.get('商品ID'))
                reason=stop_reason(simple)
                if route and reason:
                    self.workflow.denied[binding_key(simple.get('抖音号'),route)]=reason
    def preview(self, live=True):
        rows=[]
        if live:
            for route in MAIN_PRODUCTS:
                try:self.workflow.scan(route)
                except Exception as exc:log.warning("主品 %s 完整扫描阻塞: %s",route.name,str(exc)[:200])
        for sheet in self.sheets:
            for rid,simple in sheet['simple'].items():
                if not is_candidate(simple):continue
                route=product_route(simple.get('商品ID')); creator=clean_id(simple.get('抖音号'))
                reason=stop_reason(simple)
                hit=None; binding='未核验'; action='实时核验后主号授权并加入'
                if route:
                    reason=reason or self.workflow.denied.get(binding_key(creator,route),'')
                if not route:
                    reason='非四主品商品';action='待人工处理'
                elif reason:
                    action='待人工处理'
                elif live:
                    try:
                        hit=self.workflow.hit(creator,route)
                        if hit:
                            binding=hit['bind_type']+' / 在投';action='已绑定；继续核验视频素材，缺失才补入最新可用视频'
                        else:
                            scan=self.workflow.scans[route.plan_id]
                            deleted=any(r['creator_id']==creator and r['bind_status']=='已删除' for r in scan['rows'])
                            binding='已删除' if deleted else '未绑定'
                            action='主号合作达人授权，生效后加入对应计划'
                    except Exception as exc:
                        reason=str(exc)[:250];action='扫描阻塞，不执行授权'
                rows.append({'table':sheet['name'],'record_id':rid,'creator':creator,'department':simple.get('业务部',''),
                             'product':route.name if route else str(simple.get('商品ID','')),
                             'account_id':MAIN_AAVID,'product_id':route.product_id if route else '',
                             'plan_id':route.plan_id if route else '', 'progress':simple.get('进度',''),
                             'binding':binding,'action':action,'reason':reason})
        return rows
    def process_sheet(self, client, sheet, state, args, poll):
        client.cfg['sheet_id']=sheet['sheet_id']
        item=next(s for s in self.sheets if s['sheet_id']==sheet['sheet_id'])
        retry=getattr(args,'retry',None)
        candidates=[(rid,s) for rid,s in item['simple'].items() if (not retry or rid==retry) and is_candidate(s,retry=bool(retry) or bool(getattr(args,'force',False)))]
        phase=self.rollout.get('phase','preview')
        if getattr(args,'dry_run',False) or phase not in ('active','canary'):
            for row in self.preview(live=False):
                if row['table']==sheet['name'] and (not retry or row['record_id']==retry):
                    log.info('[主号预览] %s',json.dumps(row,ensure_ascii=False))
            return 0,0,len(candidates)
        if phase=='canary':
            allowed=set(self.rollout.get('canary_record_ids') or [])
            candidates=[(rid,s) for rid,s in candidates if rid in allowed]
        now=time.time()
        pending_sync={r['record_id']:r for r in store.pending_sync(sheet['sheet_id'])}
        candidates.sort(key=lambda x:state.get('last_attempt',{}).get(x[0],0))
        limit=getattr(args,'main_limit',10)
        processed=0; ok=wait=blocked=0
        for rid,simple in candidates:
            if limit and processed>=limit:break
            previous=state.get('waiting_auth',{}).get(rid,{})
            if not retry and not getattr(args,'force',False) and not getattr(args,'ignore_wait',False) and rid not in pending_sync and now<previous.get('next_check',0):continue
            # 自己持有轮询浏览器锁时不能等待日报（日报也在等此锁），应结束本轮释放。
            if poll.plan_power_busy() or poll.report_busy() or getattr(poll,'near_report_slot',lambda:False)():
                log.info('主号记录间给启停/播报让行，释放锁后下轮继续');break
            try:
                state.setdefault('last_attempt',{})[rid]=time.time()
                poll.write_status(sheet=sheet['name'],record_id=rid,step='主号四主品',note='按商品核验主号关系')
                from wecom_client import simplify_record
                current=client.get_records(record_ids=[rid])
                if len(current)!=1:
                    raise RuntimeError('当前表格记录无法唯一读取')
                simple=simplify_record(current[0]) or {}
                if not is_candidate(simple,retry=bool(retry) or bool(getattr(args,'force',False))):
                    continue
                route=product_route(simple.get('商品ID'));creator=clean_id(simple.get('抖音号'))
                state.setdefault('labels',{})[rid]=creator
                def still_eligible():
                    latest=client.get_records(record_ids=[rid])
                    if len(latest)!=1:raise RuntimeError('表格状态无法核验，停止平台操作')
                    fresh=simplify_record(latest[0]) or {}
                    if (clean_id(fresh.get('抖音号')),product_route(fresh.get('商品ID'))) != (creator,route):
                        raise RuntimeError('表格达人或商品已变化，停止平台操作')
                    reason=stop_reason(fresh)
                    if reason:raise RuntimeError(reason)
                    if poll.plan_power_busy() or poll.report_busy():
                        raise RuntimeError('启停/播报正在等待浏览器，本条留待下轮')
                    if not is_candidate(fresh,retry=bool(retry) or bool(getattr(args,'force',False))):
                        raise RuntimeError('表格状态已变化，不再满足待处理条件')

                sync=pending_sync.get(rid)
                # 平台结果已持久化而写表失败时，本次仅补同步；等待态同样不得重新申请。
                if (route and sync and
                    (sync['account_id'],sync['creator_id'],sync['product_id'],sync['plan_id'])==binding_key(creator,route) and
                    not (stop_reason(simple) or self.workflow.denied.get(binding_key(creator,route)))):
                    outcome=result(sync['progress'],sync['detail'],'补同步表格')
                else:
                    outcome=self.workflow.execute(simple,poll._scheduled_delivery_date(simple),still_eligible=still_eligible)
                progress=outcome['progress']
                if route:store.stage_record(sheet['sheet_id'],rid,creator,route,outcome)
                if progress!=str(simple.get('进度') or '') or sync:
                    try:
                        from wecom_client import text_cell, simplify_record
                        client.update_records([{'record_id':rid,'values':{'进度':text_cell(progress)}}])
                        check=client.get_records(record_ids=[rid])
                        if len(check)!=1 or (simplify_record(check[0]) or {}).get('进度')!=progress:
                            raise RuntimeError('回写响应返回后，表格读回状态不一致')
                        if route:store.mark_synced(sheet['sheet_id'],rid)
                    except Exception as exc:
                        log.error('主号回写失败 %s（仅重试同步）: %s',rid,str(exc)[:200])
                elif route:
                    store.mark_synced(sheet['sheet_id'],rid)
                detail=f"version={WORKFLOW_VERSION}; account_id={MAIN_AAVID}; product_id={route.product_id if route else ''}; plan_id={route.plan_id if route else ''}; "+outcome['detail']
                poll.write_plan_registry(rid,{k:v for k,v in simple.items() if k!='合作码'},progress,outcome['step'],detail,'ok' if progress in BOUND and outcome.get('outcome')!='blocked' else 'wait',plan_label='主号主品绑定')
                state.setdefault('steps',{}).setdefault(rid,{})['主号:'+WORKFLOW_VERSION]='ok' if progress in BOUND and outcome.get('outcome')!='blocked' else 'wait'
                # No 48-hour give-up and no old departmental retry counters in this workflow.
                state.setdefault('errors',{}).pop(rid,None)
                state.setdefault('attempts',{}).pop(rid,None)
                state.setdefault('waiting_auth',{})[rid]={'since':previous.get('since',time.time()),'next_check':time.time()+poll.retry_interval_seconds(),'note':progress}
                log.info('主号记录 %s → %s；%s',rid,progress,outcome['detail'][:250])
                if progress in BOUND and outcome.get('outcome')!='blocked':ok+=1
                elif progress in ('佣金过高','待人工处理') or outcome.get('outcome')=='blocked':blocked+=1
                else:wait+=1
                processed+=1
                poll.save_state(state)
            except Exception:
                log.exception('主号记录 %s 异常，保留断点并继续其他记录',rid)
                blocked+=1;processed+=1
        return ok,blocked,wait

def process_sheet(client, sheet, state, args):
    import poll_and_run as poll
    batch=getattr(args,'_main_account_batch',None)
    if batch is None:
        batch=Batch(dict(client.cfg));args._main_account_batch=batch
    return batch.process_sheet(client,sheet,state,args,poll)

def refresh_owned_lock(path, pid):
    """长批次只续期自己持有的浏览器标记，不触碰其他任务的锁。"""
    try:
        marker=Path(path)
        if json.loads(marker.read_text(encoding='utf-8')).get('pid')!=pid:
            return False
        os.utime(marker,None)
        return True
    except (OSError,ValueError):
        return False

@contextmanager
def browser_guard():
    import poll_and_run as poll
    lock=poll.acquire_instance_lock()
    if lock is None:raise RuntimeError('轮询单实例正在运行')
    owned=False
    stop_heartbeat=threading.Event()
    heartbeat=None
    try:
        if poll.plan_power_busy() or poll.report_busy() or poll._lock_fresh(poll.POLL_LOCK_FILE) or poll._lock_fresh(poll.WEBUI_LOCK_FILE):
            raise RuntimeError('浏览器正在被启停、播报或操作台占用')
        Path(poll.POLL_LOCK_FILE).write_text(json.dumps({'pid':os.getpid(),'since':time.time()}),encoding='utf-8');owned=True
        def renew():
            while not stop_heartbeat.wait(20):
                if not refresh_owned_lock(poll.POLL_LOCK_FILE,os.getpid()):break
        heartbeat=threading.Thread(target=renew,name='main-product-browser-lock',daemon=True)
        heartbeat.start()
        yield
    finally:
        stop_heartbeat.set()
        if heartbeat:heartbeat.join(timeout=1)
        if owned:
            try:
                data=json.loads(Path(poll.POLL_LOCK_FILE).read_text(encoding='utf-8'))
                if data.get('pid')==os.getpid():Path(poll.POLL_LOCK_FILE).unlink()
            except (OSError,ValueError):pass
        lock.close()

def main():
    parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preview',action='store_true')
    mode.add_argument('--apply',action='store_true')
    parser.add_argument('--record',help='仅处理指定记录（仍服从主号商品校验和单条上线阶段）')
    parser.add_argument('--all',action='store_true',help='本轮处理全部符合条件的候选')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    from wecom_client import WeComClient
    client=WeComClient()
    with browser_guard():
        batch=Batch(client.cfg)
        if args.preview:
            rows=batch.preview(live=True)
            payload={'version':WORKFLOW_VERSION,'generated_at':datetime.now().isoformat(timespec='seconds'),
                     'counts':dict(Counter(r['binding'] for r in rows)),'rows':rows}
        else:
            import poll_and_run as poll
            from types import SimpleNamespace
            opts=SimpleNamespace(retry=args.record,force=False,dry_run=False,main_limit=0 if args.all else 10,ignore_wait=args.all)
            state=poll.load_state()
            counts=[]
            for sheet in batch.sheets:
                counts.append({'table':sheet['name'],'results':batch.process_sheet(client,sheet,state,opts,poll)})
            poll.write_status(state='idle',sheet='',record_id='',step='',note='主号四主品本轮处理完成')
            payload={'version':WORKFLOW_VERSION,'phase':batch.rollout.get('phase'),'counts':counts,
                     'results':[{'account_id':k[0],'creator_id':k[1],'product_id':k[2],'plan_id':k[3],**v}
                                for k,v in batch.workflow.results.items()]}
    text=json.dumps(payload,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text,encoding='utf-8')
    print(text)
    return 0

if __name__=='__main__':raise SystemExit(main())
