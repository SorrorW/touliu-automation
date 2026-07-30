"""快速探针：dump 千川总览页表格列布局"""
import asyncio, json, sys
from playwright.async_api import async_playwright

CDP = "http://127.0.0.1:9222"

async def probe():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP, timeout=8000)
        page = None
        for p in browser.contexts[0].pages:
            if "qianchuan" in p.url:
                page = p
                break
        if not page:
            print(json.dumps({"error": "no qianchuan page"}))
            return

        print(f"URL: {page.url}")

        # 1. 读表头
        headers = await page.evaluate("""() => {
            const ths = document.querySelectorAll('table thead th, table thead td');
            return Array.from(ths).map((th, i) => ({
                index: i,
                text: (th.innerText || '').trim().substring(0, 80)
            }));
        }""")
        print("\n=== 表头 ===")
        for h in headers:
            print(f"  [{h['index']:2d}] {h['text']}")

        # 2. 读第一行所有单元格
        cells = await page.evaluate("""() => {
            const row = document.querySelector('table tbody tr');
            if (!row) return [];
            const tds = row.querySelectorAll('td, th');
            return Array.from(tds).map((td, i) => ({
                index: i,
                text: (td.innerText || '').trim().substring(0, 200)
            }));
        }""")
        print("\n=== 第一行数据 ===")
        for c in cells:
            print(f"  [{c['index']:2d}] {c['text']}")

asyncio.run(probe())
