# -*- coding: utf-8 -*-
"""主号四大主品计划「自动拉取达人」只读抓取（2026-10-13 用户需求）。

主号（鲜会鲜 1762338766025739）四大主品计划的「抖音号」tab 逐页展开，
输出标准化行：{plan_key, plan_id, creator_name, creator_id, bind_type, bind_status}。
bind_type: 自动拉取 / 手动授权；bind_status: 在投 / 已删除（平台侧绑定被删）。

CLI: python -m 巨量千川.main_pull_scout  → 末行 JSON 数组
"""
from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import async_playwright

from .session_prepare import PRODUCT_MAP, connect_browser, open_main_account_page, main_browser_lock_owned
from .main_products import (MAIN_AAVID, MAIN_PLANS, MAIN_PRODUCTS, PRODUCT_BY_PLAN,
                            product_route, verify_account)
import re
import argparse

_PRODUCT_IDS = set(PRODUCT_MAP.values())
CREATOR_NEXT_SELECTOR = '[class*="pagination"] [class*="next"]:not([class*="ellipsis"]), li[title="下一页"]'


async def _strip_plugin_overlays(page) -> None:
    """2026-10-13 实证：uni-prom/setting 页偶发空壳插件遮罩
    （.tools-vmok-plugin-modal__wrap，无文本但拦截点击）。导航后清掉。"""
    try:
        await page.evaluate("""() => {
          for (const e of document.querySelectorAll(
            '.tools-vmok-plugin-modal__wrap, .tools-vmok-plugin-popup__mask, '
            + '[class*="tools-vmok-plugin"][class*="mask"], '
            + '[class*="tools-vmok-plugin"][class*="wrap"]')) e.remove();
        }""")
    except Exception:
        pass


async def _js_click_text(page, text: str) -> bool:
    """JS 直点指定文本元素（绕过遮罩拦截；2026-10-13 实证 vmok 插件遮罩清完会再生）。"""
    return await page.evaluate("""(text) => {
      const els = [...document.querySelectorAll('*')].filter(e =>
        e.children.length === 0 && (e.innerText || '').trim() === text && e.offsetParent !== null);
      if (!els.length) return false;
      els[0].click();
      return true;
    }""", text)


async def wait_creator_list_ready(page, *, timeout=15000):
    """等待分页总数和行/明确空态同时就绪；超时不推断未绑定。"""
    await page.wait_for_function(r"""() => {
      const visible=e=>e.offsetWidth>0&&e.offsetHeight>0;
      if([...document.querySelectorAll('[aria-busy="true"],.ovui-spin-spinning')].some(visible))return false;
      const text=document.body.innerText||'';
      const totals=[...text.matchAll(/共\s*(\d+)\s*条记录/g)];
      if(!totals.length)return false;
      const total=Number(totals[totals.length-1][1]);
      const rows=[...document.querySelectorAll('table tbody tr')].filter(visible);
      if(total===0)return /暂无数据|暂无抖音号|暂无相关|无搜索结果|未找到/.test(text);
      return rows.some(r=>/ID[:：]/.test(r.innerText||''));
    }""",timeout=timeout)


async def _read_plan_creators(page, plan_id: str) -> list[str]:
    """完整读取主品关系；总数不符、翻页卡住、账号/商品不符一律抛异常。"""
    route = PRODUCT_BY_PLAN[plan_id]
    await page.goto(
        f"https://qianchuan.jinritemai.com/uni-prom/setting?aavid={MAIN_AAVID}&adId={plan_id}",
        timeout=30000, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    await _strip_plugin_overlays(page)
    await verify_account(page, plan_id=plan_id)
    if not await _js_click_text(page, "商品"):
        raise RuntimeError("商品标签未找到，无法核实目标商品")
    await page.wait_for_function(r"""expected => {
      const ids=[...(document.body.innerText||'').matchAll(/ID[:：]\s*(\d{19})(?!\d)/g)].map(m=>m[1]);
      return ids.length && ids.every(x=>x===expected);
    }""",arg=route.product_id,timeout=15000)
    goods = await page.locator("body").inner_text()
    ids = set(re.findall(r"ID[:：]\s*(\d{19})(?!\d)", goods))
    if ids != {route.product_id}:
        raise RuntimeError(f"目标计划商品校验失败: {plan_id}")
    if not await _js_click_text(page, "抖音号"):
        raise RuntimeError("抖音号标签未找到")
    await page.wait_for_timeout(2000)
    rows, seen_pages = [], set()
    total = None
    for _ in range(500):
        await wait_creator_list_ready(page)
        await verify_account(page, plan_id=plan_id)
        body = await page.locator("body").inner_text()
        counts = re.findall(r"共\s*(\d+)\s*条记录", body)
        if not counts:
            raise RuntimeError("抖音号总条数未读取，不得推断未绑定")
        count = int(counts[-1])
        if total is not None and total != count:
            raise RuntimeError("读取期间绑定总数变化，下一轮重读")
        total = count
        batch = await page.evaluate("""() => [...document.querySelectorAll('table tbody tr')]
          .filter(e => e.offsetWidth > 0 && e.offsetHeight > 0)
          .map(e => (e.innerText||'').replace(/\\n/g, ' | ').trim())
          .filter(t => /ID[:：]/.test(t))""")
        fp = tuple((_parse_row(route.name, plan_id, t) or {}).get("creator_id") for t in batch)
        if fp in seen_pages and total:
            raise RuntimeError("主号达人分页未前进")
        seen_pages.add(fp)
        for text in batch:
            if _parse_row(route.name, plan_id, text) is None:
                raise RuntimeError("主号达人行无法识别，不得忽略")
            if text not in rows:
                rows.append(text)
        nexts = page.locator(CREATOR_NEXT_SELECTOR)
        nxt, disabled = None, True
        for i in range(await nexts.count()):
            el = nexts.nth(i)
            if await el.is_visible():
                nxt = el
                disabled = await el.evaluate("""e => !!e.closest('[disabled], [aria-disabled="true"], [class*="disabled"]')""")
                break
        if nxt is None or disabled:
            if len(rows) != total:
                raise RuntimeError(f"主号分页不完整: 已读{len(rows)}/总数{total}")
            return rows
        await nxt.click(timeout=5000)
        await page.wait_for_function(r"""previous => {
          const ids=[...document.querySelectorAll('table tbody tr')]
            .filter(e=>e.offsetWidth>0&&e.offsetHeight>0)
            .map(e=>(e.innerText||'').match(/ID[:：]\s*([A-Za-z0-9_.-]+)/)?.[1]).filter(Boolean);
          return ids.length && JSON.stringify(ids)!==JSON.stringify(previous);
        }""",arg=list(fp),timeout=15000)
        await page.wait_for_timeout(300)
    raise RuntimeError("主号分页超过安全上限，禁止作出未绑定结论")


async def read_target(page, route):
    texts = await _read_plan_creators(page, route.plan_id)
    rows = []
    for text in texts:
        row = _parse_row(route.name, route.plan_id, text)
        product_ids=set(re.findall(r"ID[:：]\s*(\d{19})(?!\d)",text))
        # 平台在已删除关系行不再展示商品。目标计划商品已在独立商品tab核实，
        # 此处仅允许明确已删除且没有商品ID的行，绝不当作有效绑定。
        deleted_without_product=(row and row["bind_status"]=="已删除" and not product_ids)
        if product_ids != {route.product_id} and not deleted_without_product:
            raise RuntimeError("达人关系行的商品与目标不符")
        row.update(account_id=route.account_id, product_id=route.product_id)
        rows.append(row)
    # Duplicate creator rows are ambiguous; never choose one silently.
    if len({r['creator_id'] for r in rows}) != len(rows):
        raise RuntimeError("同一主品出现重复达人行，需核查")
    return {"rows": rows, "complete": True, "account_id": route.account_id,
            "product_id": route.product_id, "plan_id": route.plan_id}


def _parse_row(plan_key: str, plan_id: str, text: str) -> dict | None:
    """行文本 → 标准行。行形：'| 名称 | 自动拉取 | ID：达人ID | … | 商品名 | ID：商品ID'。
    达人 ID=名称后的第一个非商品 ID；「已删除」前缀=绑定已删；自店号无自动拉取标。"""
    if "无抖音号" in text:
        return None
    parts = [p.strip() for p in text.split("|") if p.strip()]
    name, cid, auto, deleted = "", "", False, False
    for p in parts:
        if p == "已删除":
            deleted = True
            continue
        if p == "自动拉取":
            auto = True
            continue
        if re.match(r"^ID[:：]", p):
            v = re.sub(r"^ID[:：]\s*", "", p).strip()
            if v not in _PRODUCT_IDS and not cid:
                cid = v
            continue
        if not name and p not in ("编辑", "删除"):
            name = p
    if not cid or not name:
        return None
    return {"plan_key": plan_key, "plan_id": plan_id, "creator_name": name,
            "creator_id": cid,
            "bind_type": "自动拉取" if auto else "手动授权",
            "bind_status": "已删除" if deleted else "在投"}


async def _read_plan_oplog(page, plan_id: str, max_pages: int = 3) -> list[str]:
    """读计划「日志」tab 最近几页，返回含「抖音号」变更的操作行原文（只读）。

    行含：操作时间/操作类型/操作内容/操作人——拉取/删除达人即「修改 抖音号…」类条目，
    谁在什么时候拉/删一目了然（2026-10-13 用户需求）。
    """
    await page.goto(
        f"https://qianchuan.jinritemai.com/uni-prom/setting?aavid={MAIN_AAVID}&adId={plan_id}",
        timeout=30000, wait_until="domcontentloaded")
    await page.wait_for_timeout(4500)
    await _strip_plugin_overlays(page)
    if not await _js_click_text(page, "日志"):
        await page.get_by_text("日志", exact=True).first.click()
    await page.wait_for_timeout(3000)
    hits: list[str] = []
    for _ in range(max_pages):
        batch = await page.evaluate("""() => {
          const vis = e => e.offsetWidth > 0 && e.offsetHeight > 0;
          return [...document.querySelectorAll('table tbody tr')].filter(vis)
            .map(r => (r.innerText||'').replace(/\\n/g, ' | ').trim()).filter(t => t.length > 3);
        }""")
        for t in batch:
            if ("抖音号" in t or "拉取" in t) and t not in hits:
                hits.append(t)
        nxt = page.locator(
            '[class*="pagination"] [class*="next"]:not([class*="disabled"]), '
            'li[title="下一页"]:not([class*="disabled"])').first
        try:
            if await nxt.count() and await nxt.is_visible():
                cls = await nxt.get_attribute("class") or ""
                if "disabled" in cls:
                    break
                await nxt.click()
                await page.wait_for_timeout(1500)
                continue
        except Exception:
            pass
        break
    return hits


async def scrape(with_oplog: bool = False, product_ids=None) -> dict:
    targets = list(MAIN_PRODUCTS) if product_ids is None else [product_route(x) for x in product_ids]
    if any(t is None for t in targets):
        raise ValueError("未知主品商品ID")
    out, oplogs, scans = [], {}, []
    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        page = await open_main_account_page(browser.contexts[0])
        try:
            for route in targets:
                scan = await read_target(page, route)
                out.extend(scan["rows"])
                scans.append({k:v for k,v in scan.items() if k != "rows"})
                if with_oplog:
                    oplogs[route.name] = await _read_plan_oplog(page, route.plan_id)
        finally:
            await page.close()
    return {"rows": out, "oplogs": oplogs, "scans": scans, "complete": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-oplog", action="store_true")
    parser.add_argument("--product", action="append")
    args = parser.parse_args()
    if main_browser_lock_owned():
        result = asyncio.run(scrape(with_oplog=args.with_oplog, product_ids=args.product))
    else:
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'wecom-agent'))
        from main_account_flow import browser_guard
        with browser_guard():
            result = asyncio.run(scrape(with_oplog=args.with_oplog, product_ids=args.product))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
