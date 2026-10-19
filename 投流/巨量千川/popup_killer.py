# -*- coding: utf-8 -*-
"""通用弹窗清理：所有自动化流程导航/点击前调用。

2026-10-14 用户口径：「跳出来的弹窗你不会点掉就在那边卡住？」——
弹窗处理策略从「卡住报错」改为「见就点掉」。
覆盖三类：
1. 公告类：我知道了 / 如何使用乘方 等公告弹窗 → 点掉；
2. 空壳遮罩：tools-vmok-plugin-* mask/wrap（无文本纯拦截）→ DOM 删除；
3. 挽留确认类：含「确定提交」的模态 → 点「确定」通过。
"""
from __future__ import annotations


async def dismiss_popups(page) -> dict:
    """清掉页面上已知的拦截弹窗。返回 {announced, overlays, modals} 计数。"""
    result = {"announced": 0, "overlays": 0, "modals": 0}
    try:
        # 2. 空壳遮罩：直接删
        result["overlays"] = await page.evaluate("""() => {
          let n = 0;
          for (const e of document.querySelectorAll(
            '.tools-vmok-plugin-modal__wrap, .tools-vmok-plugin-popup__mask, '
            + '[class*="tools-vmok-plugin"][class*="mask"], '
            + '[class*="tools-vmok-plugin"][class*="wrap"]')) { e.remove(); n++; }
          return n;
        }""")
        # 1. 公告类按钮
        for text in ("我知道了", "知道了"):
            try:
                btn = page.locator(f'button:has-text("{text}")').first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=2000)
                    result["announced"] += 1
                    await page.wait_for_timeout(600)
            except Exception:
                pass
        # 3. 挽留确认模态：含「确定提交」的 → 点确定
        result["modals"] = await page.evaluate("""() => {
          let n = 0;
          for (const d of document.querySelectorAll('[role="dialog"], [class*="modal"]')) {
            if (d.offsetParent === null) continue;
            if (!(d.innerText || '').includes('确定提交')) continue;
            for (const b of d.querySelectorAll('button')) {
              if (b.offsetParent !== null && (b.innerText || '').trim() === '确定') {
                b.click(); n++; break;
              }
            }
          }
          return n;
        }""")
    except Exception:
        pass
    return result
