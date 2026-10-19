"""只对四个既有主品计划添加达人；不新建、不改投放参数或启停状态。"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit
from playwright.async_api import async_playwright
from .main_products import MAIN_AAVID, product_route, clean_id, verify_account
from .main_pull_scout import read_target, _strip_plugin_overlays
from .session_prepare import connect_browser, open_main_account_page
from .plan_create import _open_creator_drawer, _disabled_reason
from .commission_gate import (parse_commission_from_row_text, is_commission_blocked,
                              load_commission_max_pct, CommissionBlockedError)

async def assert_editor(page, route):
    body = await verify_account(page)
    query = parse_qs(urlsplit(page.url).query)
    if query.get('adId') != [route.plan_id] or query.get('type') != ['edit']:
        raise RuntimeError('不是指定现有计划的编辑页，禁止提交')
    # Read only the editor body, excluding drawer content and global menus.
    ids = set(re.findall(r'ID[:：]\s*(\d{19})(?!\d)', body))
    if ids != {route.product_id}:
        raise RuntimeError('编辑器选中商品与目标不符，禁止提交：'+','.join(sorted(ids)))

async def open_editor(page, route):
    await page.goto(f'https://qianchuan.jinritemai.com/create-ad/overall-product?aavid={MAIN_AAVID}&adId={route.plan_id}&type=edit',
                    wait_until='domcontentloaded', timeout=30000)
    await page.get_by_role('button', name='保存', exact=True).wait_for(state='visible', timeout=20000)
    await page.wait_for_timeout(2000)
    await _strip_plugin_overlays(page)
    await page.wait_for_function(r"""expected => {
      const ids=[...(document.body.innerText||'').matchAll(/ID[:：]\s*(\d{19})(?!\d)/g)].map(m=>m[1]);
      const name=document.querySelector('input[placeholder*="请输入计划名称"]');
      return ids.length && ids.every(x=>x===expected) && name && name.value.length>0;
    }""",arg=route.product_id,timeout=15000)
    await assert_editor(page, route)

async def parameter_snapshot(page):
    """记录非达人抽屉中的输入/开关，添加前后必须相同。"""
    return await page.evaluate("""() => [...document.querySelectorAll('input,textarea,select,[role="switch"]')]
      .filter(e => e.offsetWidth && !e.closest('.ovui-drawer'))
      .map(e => ({tag:e.tagName,type:e.type||'',placeholder:e.placeholder||'',
        value:e.value??'',checked:e.checked??null,aria:e.getAttribute('aria-checked')}))""")

async def creator_row(page, creator):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', creator):
        raise ValueError('抖音号格式异常，待人工处理')
    await _open_creator_drawer(page)
    drawers = page.locator('.ovui-drawer')
    drawer = None
    for i in range(await drawers.count()):
        if await drawers.nth(i).is_visible():
            drawer = drawers.nth(i)
    if drawer is None:
        raise RuntimeError('达人管理抽屉未打开')
    search = drawer.locator('input[placeholder*="请输入抖音号"]').first
    await search.wait_for(state='visible', timeout=15000)
    previous_ids=await drawer.locator('tr').evaluate_all(r'''rows => rows.filter(r=>r.offsetWidth)
      .map(r=>(r.innerText||'').match(/ID[:：]\s*([A-Za-z0-9_.-]+)/)?.[1]).filter(Boolean)''')
    await search.fill(creator)
    await search.press('Enter')
    await page.wait_for_function(r"""({creator,previous}) => {
      const ds=[...document.querySelectorAll('.ovui-drawer')].filter(e=>e.offsetWidth);
      const d=ds[ds.length-1]; if(!d)return false;
      const input=d.querySelector('input[placeholder*="请输入抖音号"]');
      if(!input || input.value!==creator)return false;
      if([...d.querySelectorAll('.ovui-spin__block,[class*="loading-mask"],[aria-busy="true"]')]
         .some(e=>e.offsetWidth&&e.offsetHeight))return false;
      const current=[...d.querySelectorAll('tr')].filter(r=>r.offsetWidth)
        .map(r=>(r.innerText||'').match(/ID[:：]\s*([A-Za-z0-9_.-]+)/)?.[1]).filter(Boolean);
      const changed=JSON.stringify(current)!==JSON.stringify(previous);
      const exact=[...d.querySelectorAll('tr')].filter(r=>r.offsetWidth).some(r=>{
        const ids=[...(r.innerText||'').matchAll(/ID[:：]\s*([A-Za-z0-9_.-]+)/g)].map(x=>x[1]);
        return ids.includes(creator);
      });
      return (exact && (changed || current.every(x=>x===creator))) || (/共\s*0\s*条记录/.test(d.innerText) && /暂无|无搜索结果|未找到/.test(d.innerText));
    }""", arg={'creator':creator,'previous':previous_ids}, timeout=20000)
    await page.wait_for_timeout(400)
    matches = []
    for i in range(await drawer.locator('tr').count()):
        row = drawer.locator('tr').nth(i)
        if not await row.is_visible():
            continue
        text = await row.inner_text()
        ids = re.findall(r'ID[:：]\s*([A-Za-z0-9_.-]+)', text)
        if creator in ids:
            exact_id=re.compile(rf'ID[:：]\s*{re.escape(creator)}(?![A-Za-z0-9_.-])')
            matches.append((drawer.locator('tr:visible').filter(has_text=exact_id), text))
    if len(matches) > 1:
        raise RuntimeError('相同抖音号存在多条授权关系，禁止猜测勾选')
    if not matches:
        # A visible search result container with explicit empty state is required.
        text = await drawer.inner_text()
        if not any(x in text for x in ('暂无数据','暂无抖音号','暂无相关','未找到','无搜索结果','暂无搜索结果')):
            raise RuntimeError('达人搜索未完成或空态未确认')
        return drawer, None, ''
    return drawer, *matches[0]

async def probe(creator, route):
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await open_main_account_page(browser.contexts[0])
        try:
            await open_editor(page, route)
            _, row, text = await creator_row(page, creator)
            name_parts=[x.strip() for x in re.split(r'ID[:：]',text,maxsplit=1)[0].splitlines() if x.strip()]
            return {'found': row is not None, 'creator_name':name_parts[-1] if name_parts else '',
                    'commission_pct': parse_commission_from_row_text(text),
                    'managed': any(x in text for x in ('投放即生效','托管授权')),
                    'status_text': text[:500] if text else '无联盟合作记录',
                    'account_id': route.account_id, 'product_id': route.product_id, 'plan_id': route.plan_id}
        finally:
            await page.close()

async def bind(creator, route):
    """重读全量→仅添加一个精确 ID→保存→重新全量核实。响应不明交给下一轮重读。"""
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await open_main_account_page(browser.contexts[0])
        try:
            before = await read_target(page, route)
            hit = next((r for r in before['rows'] if r['creator_id']==creator and r['bind_status']=='在投'), None)
            if hit:
                return {'binding':hit,'scan':before,'outcome':'already_bound'}
            await open_editor(page, route)
            params = await parameter_snapshot(page)
            drawer, row, text = await creator_row(page, creator)
            if row is None:
                raise RuntimeError('授权后仍未出现在主品的可选达人列表，等待平台同步')
            rate = parse_commission_from_row_text(text)
            if is_commission_blocked(rate, load_commission_max_pct()):
                raise CommissionBlockedError(f'佣金过高：{rate}%')
            checkbox = row.locator('input[type="checkbox"]').first
            if await checkbox.count()!=1:
                raise RuntimeError('目标行缺少唯一勾选框')
            if await checkbox.is_disabled():
                reason = await _disabled_reason(page, creator)
                raise RuntimeError('平台限制，无法添加达人：'+(reason or text[:250]))
            if not await checkbox.is_checked():
                label = row.locator('label.ovui-checkbox').first
                if await label.count():
                    await label.click(timeout=5000)
                else:
                    await checkbox.check(timeout=5000)
                if not await checkbox.is_checked():
                    raise RuntimeError('目标达人勾选未生效')
            await assert_editor(page, route)
            confirm = drawer.get_by_role('button', name='确定', exact=True)
            if await confirm.count()!=1 or not await confirm.is_enabled():
                raise RuntimeError('达人抽屉确定按钮不唯一或不可用，未保存')
            await confirm.click(timeout=5000)
            await drawer.wait_for(state='hidden', timeout=12000)
            await assert_editor(page, route)
            if await parameter_snapshot(page)!=params:
                raise RuntimeError('检测到计划参数变化，已中止保存')
            save = page.get_by_role('button', name='保存', exact=True)
            if await save.count()!=1 or not await save.is_enabled():
                raise RuntimeError('既有计划保存入口不唯一或不可用')
            # Only an existing-plan save is allowed. Never click publish/create fallbacks.
            await save.click(timeout=10000)
            await page.wait_for_timeout(3000)
            save_text=await page.locator('body').inner_text()
            save_block=''
            if any(x in save_text for x in ('设置超出可跑量上限','当前设置过高','最大设置不能超过')):
                save_block='平台限制：现有主品ROI超过平台当前可保存上限；未改ROI或预算，加入结果待核查'
            result = await read_target(page, route)
            hit = next((r for r in result['rows'] if r['creator_id']==creator and r['bind_status']=='在投'), None)
            if not hit:
                raise RuntimeError(save_block or '提交结果未核实，下轮先重读绑定列表')
            await open_editor(page, route)
            if await parameter_snapshot(page)!=params:
                raise RuntimeError('绑定已提交，但计划参数复核不一致，待人工核查')
            return {'binding':hit,'scan':result,'outcome':'added'}
        finally:
            await page.close()

async def scan(route):
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await open_main_account_page(browser.contexts[0])
        try:
            return await read_target(page, route)
        finally:
            await page.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('scan','probe','bind'))
    parser.add_argument('--product',required=True)
    parser.add_argument('--creator',default='')
    args=parser.parse_args()
    route=product_route(args.product)
    if route is None: raise ValueError('未知主品')
    def operation():
        return scan(route) if args.action=='scan' else (probe if args.action=='probe' else bind)(clean_id(args.creator),route)
    from pathlib import Path
    import sys
    from .session_prepare import main_browser_lock_owned
    if main_browser_lock_owned():
        result=asyncio.run(operation())
    else:
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'wecom-agent'))
        from main_account_flow import browser_guard
        with browser_guard():result=asyncio.run(operation())
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
