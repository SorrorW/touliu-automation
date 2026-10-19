# -*- coding: utf-8 -*-
"""查建计划抽屉里达人的授权状态（托管型识别，只读）。

2026-10-08 用户口径：「投放即生效 / 已开启托管授权」的达人（联盟合作托管型）
不做授权、不建计划，票挂「待主账号拉取」，由用户在主账号手动拉取投放。

用法：
  python -m 巨量千川.creator_instant_status --creator <抖音号> --product <商品ID> --department <部门>
输出（最后一行 JSON）：{"found": bool, "managed": bool, "status_text": str}
  managed=True ⇔ 行内含「投放即生效」或「托管授权」。
  查不到/异常一律 managed=False（放行回正常授权流程，不误拦）。
"""
import argparse
import asyncio
import json
import re
import sys

from . import plan_create as pc
from .session_prepare import connect_browser
from playwright.async_api import async_playwright


async def probe(creator_id: str, product_id: str, department: str) -> dict:
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            page = await pc._enter_create_page(page, department)
            await pc._select_product(page, product_id)
            await pc._open_creator_drawer(page)
            # 搜索达人（同 _select_creator_in_drawer 的搜索段）
            search = page.locator('input[placeholder*="请输入抖音号"]').first
            await search.wait_for(state="attached", timeout=15000)
            await pc._wait_stable(page, 1.0)
            try:
                await search.fill(creator_id)
                await search.press("Enter")
            except Exception:
                await search.evaluate("el => { el.focus(); el.select(); }")
                await page.keyboard.type(creator_id)
                await page.keyboard.press("Enter")
            await pc._wait_stable(page, 2.5)
            # 防误认（2026-10-08 用户令「这个也不能误」）：行内「ID: xxx」必须与
            # 目标抖音号精确全等——防 ID 前缀撞车（20352184 命中 203521840…的行）。
            # 命中数≠1（撞名/搜索未生效/多行）一律按探测失败挂起重试，不猜。
            hits = await page.evaluate(f"""() => {{
                const drawers = document.querySelectorAll('.ovui-drawer');
                if (!drawers.length) return {{err: 'no_drawer'}};
                const d = drawers[drawers.length - 1];
                const sub = [], exact = [];
                for (const row of d.querySelectorAll('tr, [class*="row"]')) {{
                    const t = row.innerText || '';
                    if (!t.includes('{creator_id}')) continue;
                    sub.push(t);
                    const m = t.match(/ID[:：]\\s*([A-Za-z0-9_]+)/);
                    if (m && m[1] === '{creator_id}') exact.push(t);
                }}
                return {{sub: sub.length, exact: exact}};
            }}""")
            if hits.get("err"):
                raise RuntimeError("抽屉未打开")
            # tr 与其内层 [class*=row] 会重复命中同一逻辑行，先按文本去重
            exact_rows = list(dict.fromkeys(hits.get("exact") or []))
            if not exact_rows:
                return {"found": False, "managed": False, "status_text": "抽屉未搜到该抖音号"}
            if len(exact_rows) > 1:
                # 同一 ID 多条不同行（多授权类型并存）消解规则：
                # ①任何一行「授权生效」=已授权过 → 非托管走正常流程；
                # ②任何一行「无权限/可申请」=明确的未授权非托管态 → 走正常授权申请
                #   （托管型行只会显示「投放即生效/托管授权」，不会出无权限；
                #     2026-10-12 发财uu 实证：联盟合作行无权限可申请+另一行并存，
                #     按冲突挂起会永远卡死——防误认防的是「投放即生效」被漏看）；
                # ③全部托管字样才认托管；④其余冲突挂起不猜。
                if any("授权生效" in t for t in exact_rows):
                    exact_rows = [t for t in exact_rows if "授权生效" in t][:1]
                elif any(("无权限" in t) or ("可申请" in t) for t in exact_rows):
                    exact_rows = [t for t in exact_rows
                                  if ("无权限" in t) or ("可申请" in t)][:1]
                elif all(("投放即生效" in t) or ("托管授权" in t) for t in exact_rows):
                    exact_rows = exact_rows[:1]
                else:
                    sample = " ‖ ".join(t.replace("\n", " ")[:90] for t in exact_rows[:3])
                    raise RuntimeError(
                        f"同ID多行状态冲突（{len(exact_rows)}行），防误认挂起: {sample}")
            row_text = exact_rows[0]
            managed = ("投放即生效" in row_text) or ("托管授权" in row_text)
            status = "投放即生效" if "投放即生效" in row_text else \
                     "授权生效" if "授权生效" in row_text else \
                     "无权限" if "无权限" in row_text else "未知"
            # 联盟合作行内佣金率（如「5%」）——授权前佣金预检用（2026-10-13）
            m_rate = re.search(r"(\d+(?:\.\d+)?)\s*%", row_text)
            commission = float(m_rate.group(1)) if m_rate else None
            return {"found": True, "managed": managed,
                    "status_text": status + ("（已开启托管授权）" if "托管授权" in row_text else ""),
                    "commission_pct": commission}
        finally:
            try:
                await page.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creator", required=True)
    ap.add_argument("--product", required=True)
    ap.add_argument("--department", required=True)
    args = ap.parse_args()
    try:
        result = asyncio.run(probe(args.creator, args.product, args.department))
        rc = 0
    except Exception as exc:  # 探测失败不拦流程：managed=False 回正常授权
        result = {"found": False, "managed": False, "status_text": f"探测异常: {str(exc)[:200]}"}
        rc = 1
    print(json.dumps(result, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
