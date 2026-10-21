"""主号既有主品计划的视频补入；抖音号绑定与素材加入分别复核。"""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
from playwright.async_api import async_playwright
from .main_products import verify_account, binding_key, clean_id
from .commission_gate import parse_commission_from_card_text, is_commission_blocked, load_commission_max_pct
from .main_pull_scout import _js_click_text, _strip_plugin_overlays
from .session_prepare import connect_browser, open_main_account_page

class MaterialReadError(RuntimeError):
    pass

def timestamp(value):
    try:
        number=float(value)
        if number>10**12:number/=1000
        return number if number>0 else 0
    except (ValueError,TypeError):return 0

def checked_videos(pages, author_uid):
    """只有完整、同一作者的视频列表才允许得出没有素材的结论。"""
    if not pages or pages[-1].get('hasMore') is not False:
        raise MaterialReadError('视频分页未完成，不得判为没有素材')
    seen={};previous_cursor=None
    for i,page in enumerate(pages):
        if not isinstance(page.get('videoList'),list):raise MaterialReadError('视频列表结构未知')
        if i<len(pages)-1 and not page.get('hasMore'):raise MaterialReadError('视频分页状态不连续')
        cursor=page.get('cursor')
        if i and page.get('hasMore') and cursor==previous_cursor:raise MaterialReadError('视频分页游标未前进')
        previous_cursor=cursor
        for raw in page['videoList']:
            if str(raw.get('authorUid'))!=str(author_uid):raise MaterialReadError('视频作者与精确选中的达人不一致')
            vid=str(raw.get('videoId') or '')
            if not vid or not raw.get('itemId') or not timestamp(raw.get('createTime')):raise MaterialReadError('视频标识或发布时间缺失')
            seen[vid]=raw
    return sorted(seen.values(),key=lambda v:timestamp(v['createTime']),reverse=True)

def choose_video(videos, usage):
    """先判断已加入，再按创建时间取最新合规候选，不套用分部72小时规则。"""
    if any(v['videoId'] not in usage or type(usage[v['videoId']]) is not bool for v in videos):
        raise MaterialReadError('视频是否已在本计划中的复核结果不完整')
    existing=[v for v in videos if usage[v['videoId']] is True]
    if existing:return {'kind':'existing','videos':existing}
    candidates=[v for v in videos if not str(v.get('reasonCode') or '') and not v.get('poorQuality')]
    return {'kind':'missing' if candidates else 'unavailable','videos':candidates}

def scoped_response(request, route):
    """只接受明确携带本计划ID的在用状态响应，禁止借用其他计划的结果。"""
    values=[]
    def collect(value):
        if isinstance(value,dict):
            for item in value.values():collect(item)
        elif isinstance(value,list):
            for item in value:collect(item)
        else:
            text=str(value);values.append(text)
            if text.startswith(('[','{')):
                with suppress(ValueError,TypeError):collect(json.loads(text))
    collect(parse_qs(urlsplit(request.url).query))
    with suppress(Exception):collect(request.post_data_json)
    if route.plan_id not in values:raise MaterialReadError('素材在用状态未明确对应目标计划')


def video_request_matches(request, route, uid):
    """空列表也必须由本账号、本商品和精确作者的请求证明。"""
    query=parse_qs(urlsplit(request.url).query)
    return (query.get('aavid')==[route.account_id]
            and query.get('productId')==[route.product_id]
            and query.get('userId')==[str(uid)])


def material_result(creator,route,videos,*,kind,verified=True,detail=''):
    return {'account_id':route.account_id,'creator_id':creator,'product_id':route.product_id,'plan_id':route.plan_id,
            'verified':verified,'verified_at':time.time(),'outcome':kind,'id_type':'video_id',
            'material_ids':[v['videoId'] for v in videos],
            'item_ids':[str(v['itemId']) for v in videos],
            'detail':detail or ('对应商品已有视频素材，未重复添加' if kind=='existing' else '最新可用视频已加入并重新读取确认')}


class Journal:
    def __init__(self,page):
        self.page=page;self.rows=[];self.tasks=set()
        self.listener=self.on_response
        page.on('response',self.listener)
    def on_response(self,response):
        path=urlsplit(response.url).path
        if '/ad/api/' not in path:return
        if not any(x in path for x in ('/material/','/aweme/','auto-auth','auto-pull','pull-aweme','/author/','get-items-by-product')):return
        task=asyncio.create_task(self.capture(response));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
    async def capture(self,response):
        try:
            body=await response.json()
            self.rows.append({'path':urlsplit(response.url).path,'request':response.request,'body':body})
        except Exception:pass
    async def close(self):
        self.page.remove_listener('response',self.listener)
        if self.tasks:await asyncio.gather(*list(self.tasks),return_exceptions=True)
    async def wait(self,path,after=0,accept=None):
        deadline=time.monotonic()+22
        while time.monotonic()<deadline:
            for row in self.rows[after:]:
                if not row['path'].endswith(path):continue
                body=row['body']
                if not isinstance(body,dict) or body.get('status_code',body.get('code',0)) not in (0,'0'):
                    raise MaterialReadError('平台素材接口未成功返回')
                if accept is None or accept(row):return row
            await asyncio.sleep(.15)
        raise MaterialReadError('素材页面未完成实时核验：'+path)


def _creator_uids(rows,creator):
    found=set()
    def walk(node):
        if isinstance(node,dict):
            if creator in {str(node.get(k) or '') for k in ('uniqueId','showId','shortId')}:
                uid=node.get('userId') or node.get('id')
                if uid:found.add(str(uid))
            for v in node.values():walk(v)
        elif isinstance(node,list):
            for v in node:walk(v)
    for row in rows:
        if 'aweme' in row['path'] or 'author' in row['path']:walk(row['body'].get('data'))
    return found

async def _open_picker(page,route):
    await page.goto(f'https://qianchuan.jinritemai.com/uni-prom/setting?aavid={route.account_id}&adId={route.plan_id}',wait_until='domcontentloaded',timeout=30000)
    await page.wait_for_function(r"""expected => {
      const text=document.body.innerText||'';
      const ids=[...text.slice(0,1800).matchAll(/ID[:：]\s*([0-9A-Za-z_]+)/g)].map(m=>m[1]);
      return ids[0]===expected.account && ids.slice(1).includes(expected.plan);
    }""",arg={'account':route.account_id,'plan':route.plan_id},timeout=18000)
    await _strip_plugin_overlays(page);await verify_account(page,plan_id=route.plan_id)
    if not await _js_click_text(page,'商品'):raise MaterialReadError('无法打开目标商品页')
    await page.wait_for_function(r"""expected => {
      const ids=[...(document.body.innerText||'').matchAll(/ID[:：]\s*(\d{19})(?!\d)/g)].map(x=>x[1]);
      return ids.length && ids.every(x=>x===expected);
    }""",arg=route.product_id,timeout=15000)
    if not await _js_click_text(page,'素材'):raise MaterialReadError('目标主品没有素材入口')
    button=page.get_by_role('button',name=re.compile('添加视频'))
    await button.wait_for(state='visible',timeout=15000);await button.click(timeout=8000)
    await page.get_by_text('自动拉取的抖音号',exact=True).filter(visible=True).click(timeout=10000)
    picker=page.locator('.ovui-drawer:visible').last
    await picker.wait_for(state='visible',timeout=8000)
    return picker

async def _select_creator(page,picker,journal,creator):
    await picker.get_by_text('请先选择自动拉取的抖音号',exact=True).filter(visible=True).click(timeout=8000)
    search=page.locator('input[placeholder="请搜索"]:visible')
    if await search.count()!=1:raise MaterialReadError('抖音号下拉搜索入口不唯一，未选择其他达人')
    start=len(journal.rows)
    await search.fill(creator);await search.press('Enter')
    option=page.locator('.auto-aweme-filter-select-drop-item:visible').get_by_text(re.compile(r'^ID[:：]\s*'+re.escape(creator)+r'$')).filter(visible=True)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if await option.count()==1:
            await asyncio.sleep(.3)
            if await option.count()==1:break
        await asyncio.sleep(.15)
    else:raise MaterialReadError('精确抖音号搜索结果未稳定为唯一结果，禁止猜测')
    deadline=time.monotonic()+8;uids=set()
    while time.monotonic()<deadline:
        uids=_creator_uids(journal.rows,creator)
        if len(uids)==1:break
        await asyncio.sleep(.15)
    if len(uids)!=1:raise MaterialReadError('精确抖音号的内部作者ID尚未核实')
    uid=next(iter(uids))
    await option.click(timeout=8000)
    return uid,start

def usage_request_matches(request,route,uid):
    query=parse_qs(urlsplit(request.url).query)
    return (query.get('aavid')==[route.account_id]
            and query.get('adId')==[route.plan_id]
            and query.get('productId')==[route.product_id]
            and query.get('AwemeUID')==[str(uid)])

async def _video_page(journal,route,uid,start):
    def accept(row):
        data=row['body'].get('data') or {};videos=data.get('videoList')
        return video_request_matches(row['request'],route,uid) and isinstance(videos,list) and (not videos or all(str(v.get('authorUid'))==uid for v in videos))
    row=await journal.wait('/get-items-by-product',start,accept)
    data=row['body'].get('data') or {};videos=data.get('videoList') or [];usage={}
    if videos:
        wanted={str(v.get('videoId')) for v in videos}
        def valid_usage(item):
            return usage_request_matches(item['request'],route,uid) and wanted <= set((item['body'].get('data') or {}).get('inUseMaterialMap') or {})
        used=await journal.wait('/check-ad-material-usage',start,valid_usage)
        usage=(used['body'].get('data') or {})['inUseMaterialMap']
        if any(type(usage[v]) is not bool for v in wanted):raise MaterialReadError('素材在用状态类型未知')
    return data,usage

async def _advance_video_page(picker):
    nexts=picker.locator('[class*="pagination"] [class*="next"]:not([class*="ellipsis"]),li[title="下一页"]')
    for i in range(await nexts.count()):
        nxt=nexts.nth(i)
        if not await nxt.is_visible():continue
        if await nxt.evaluate("""e=>!!e.closest('[disabled],[aria-disabled="true"],[class*="disabled"]')"""):
            raise MaterialReadError('接口仍有下一页但分页按钮不可用')
        await nxt.click(timeout=6000)
        return
    cards=picker.locator('[data-auto-id="create-material-card"]')
    await cards.first.wait_for(state='visible',timeout=12000)
    await cards.last.scroll_into_view_if_needed(timeout=6000)
    await picker.evaluate("""root => {
      const els=[root,...root.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+40&&e.clientHeight>80&&e.offsetWidth);
      for(const el of els)el.scrollTop=el.scrollHeight;
    }""")

async def _read_videos(page,picker,journal,route,uid,start):
    pages=[];usage={};raw_order=[];cursor_seen=set()
    for _ in range(40):
        data,used=await _video_page(journal,route,uid,start)
        videos=data.get('videoList') or []
        pages.append(data);raw_order.extend(videos);usage.update(used)
        if data.get('hasMore') is False:return checked_videos(pages,uid),usage,raw_order
        cursor=data.get('cursor')
        if cursor is None or str(cursor) in cursor_seen:raise MaterialReadError('视频分页游标缺失或重复')
        cursor_seen.add(str(cursor));start=len(journal.rows)
        await _advance_video_page(picker)
    raise MaterialReadError('视频分页超出核验上限，保留待处理')

async def _selection_page(page,route,creator,journal,video):
    # 完整扫描后可能停在末页。重新从第一页找目标视频，并复查最新在用状态。
    picker=await _open_picker(page,route)
    uid,start=await _select_creator(page,picker,journal,creator)
    seen=set()
    for _ in range(40):
        data,usage=await _video_page(journal,route,uid,start)
        videos=data.get('videoList') or []
        if any(usage.get(v['videoId']) is True for v in videos) or any(v.get('videoId')==video['videoId'] for v in videos):
            return picker,videos,usage
        if data.get('hasMore') is False:raise MaterialReadError('拟加入视频已从当前可用列表消失，未选择其他视频')
        cursor=data.get('cursor')
        if cursor is None or str(cursor) in seen:raise MaterialReadError('重新定位素材时分页未前进')
        seen.add(str(cursor));start=len(journal.rows)
        await _advance_video_page(picker)
    raise MaterialReadError('重新定位素材超出分页上限')

async def _selected_count(picker):
    text=await picker.inner_text();match=re.search(r'已选择\s*(\d+)\s*/',text)
    if not match:raise MaterialReadError('无法核实素材选择数量')
    return int(match.group(1))

async def _card_for(picker,video,raw_order):
    cards=picker.locator('[data-auto-id="create-material-card"]')
    await cards.first.wait_for(state='visible',timeout=12000)
    matches=[]
    for index in range(await cards.count()):
        card=cards.nth(index)
        title=await card.get_attribute('title') or ''
        text=await card.inner_text()
        attrs=await card.evaluate("el => [...el.attributes].map(x=>x.value)")
        ids={str(video['itemId']),str(video['videoId'])}
        if ids & set(attrs) or title==video.get('title') or (video.get('title') and video['title'] in text):matches.append(card)
    if len(matches)==1:return matches[0]
    raise MaterialReadError('无法将最新视频精确对应到唯一素材卡片，未选取其他视频')

async def _on_page(page,route,creator,journal):
    picker=await _open_picker(page,route)
    uid,start=await _select_creator(page,picker,journal,creator)
    videos,usage,raw_order=await _read_videos(page,picker,journal,route,uid,start)
    return picker,videos,usage,raw_order

async def ensure_materials(creator,route,*,before_submit=None,apply=True):
    from .main_products import product_route
    if product_route(route.product_id)!=route:raise ValueError('不是四主品的正确主号路由')
    creator=clean_id(creator)
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',creator):raise ValueError('抖音号格式异常')
    async with async_playwright() as pw:
        browser=await connect_browser(pw,timeout=8000)
        page=await open_main_account_page(browser.contexts[0]);journal=Journal(page)
        try:
            picker,videos,usage,raw_order=await _on_page(page,route,creator,journal)
            decision=choose_video(videos,usage)
            if decision['kind']=='existing':return material_result(creator,route,decision['videos'],kind='existing')
            if decision['kind']=='unavailable':return material_result(creator,route,[],kind='wait',verified=False,detail='抖音号已拉取，但对应商品暂无可用视频，等待素材后重试')
            max_pct=load_commission_max_pct();chosen=None;card=None
            for video in decision['videos']:
                picker,fresh,latest_usage=await _selection_page(page,route,creator,journal,video)
                existing=[v for v in fresh if latest_usage.get(v['videoId']) is True]
                if existing:return material_result(creator,route,existing,kind='existing')
                actual=next((v for v in fresh if v.get('videoId')==video['videoId']),None)
                if not actual or latest_usage.get(video['videoId']) is not False:raise MaterialReadError('目标视频的最新资格未核实，未提交')
                if str(actual.get('reasonCode') or '') or actual.get('poorQuality'):continue
                video=actual
                candidate=await _card_for(picker,video,raw_order)
                text=await candidate.inner_text()
                classes=await candidate.get_attribute('class') or ''
                if 'disabled' in classes or 'selected-item' in classes:continue
                if is_commission_blocked(parse_commission_from_card_text(text),max_pct):continue
                chosen=video;card=candidate;break
            if chosen is None:return material_result(creator,route,[],kind='wait',verified=False,detail='现有视频均不可选或佣金超过现有上限，等待可用素材')
            if not apply:
                out=material_result(creator,route,[],kind='preview',verified=False,detail='缺少素材，拟补入最新可用视频；尚未提交')
                out['candidate']={k:chosen[k] for k in ('itemId','videoId','createTime')};return out
            if await _selected_count(picker)!=0:raise MaterialReadError('选择器已有未提交勾选，未覆盖选择')
            await card.click(timeout=8000)
            deadline=time.monotonic()+4
            while time.monotonic()<deadline and await _selected_count(picker)!=1:await asyncio.sleep(.15)
            if await _selected_count(picker)!=1:raise MaterialReadError('仅允许勾选一条精确视频，选择数量不符')
            if before_submit:before_submit()
            await verify_account(page,plan_id=route.plan_id)
            confirm=picker.get_by_role('button',name='确定',exact=True)
            if await confirm.count()!=1 or not await confirm.is_enabled():raise MaterialReadError('素材确定按钮不可用，未提交')
            await confirm.click(timeout=10000)
            await picker.wait_for(state='hidden',timeout=15000)
            # Navigation destroys local selections; a fresh in-use response must prove persistence.
            _,after,after_usage,_=await _on_page(page,route,creator,journal)
            actual=next((v for v in after if v['videoId']==chosen['videoId']),None)
            if not actual or after_usage.get(chosen['videoId']) is not True:
                raise MaterialReadError('提交后未核实该视频已在目标计划，下轮先复查，不重复提交')
            return material_result(creator,route,[actual],kind='added')
        finally:
            await journal.close();await page.close()
