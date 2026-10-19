"""建计划前的旧计划清理：先判定是否旧计划，再按近7天数据处理。

规则（2026-08-15 与业务确认）：
  千川总览页按抖音号搜索该达人、时间范围切到「近7天」，
  先判定计划是不是此商品的（商品核实），再判定是否旧计划：

  商品核实（三级）：
  ① 行文本/计划名含商品名或商品 ID → 匹配；
  ② 计划列表接口的 adGoodsMap（计划ID→商品ID）与记录商品 ID 比对：
     一致 → 匹配（名字不带商品的计划靠这层识别）；不一致 → 不是此商品的
     计划，不动它，随后走建新计划；
  ③ 接口未返回该计划商品信息 → 无法核实，一律不动，记 warning。

  是否旧计划（对匹配上的计划）：

  ① 计划名 MMDD 日期 = 今天（本系统自动建计划命名 MMDD_达人_姓）
     → 今日新建计划，不算旧计划：不删不加素材，直接作为本记录的目标计划
       （不再建新计划，只对它追投新素材）。覆盖「上一轮建计划成功但进程崩溃」
       和「人工今天刚建」两种撞车场景。
  ② 近7天有数据（消耗>0 或 展示>0）
     → 有数据必为旧计划（今天建的不可能有7天消耗）：进编辑页加最新视频素材，
       随后不再建新计划，只对这条旧计划追投新素材。
  ③ 日期早于今天 + 近7天无数据
     → 确认旧计划：删除，随后走正常建计划/追投。
  ④ 日期无法解析 + 无数据
     → 新旧不明，一律不动，记 warning 留人工；不阻塞建计划。

  匹配不上商品的行一律不动（宁可漏删，不可错删——删除是不可逆的真实操作）。

Usage:
    # 只查询报告，不动任何计划（默认）
    python -m 巨量千川.plan_sweep --creator 58021911736 --product 肥牛 --department 业务2部

    # 实际执行删除/加素材
    python -m 巨量千川.plan_sweep --creator 58021911736 --product 肥牛 --department 业务2部 --apply

输出 JSON：
    {success, matched, plans:[{plan_name,plan_id,status,spent,impressions,has_data,bucket}],
     deleted:[], material_added:[], warnings:[], has_active, chase_plan_id, message}
    bucket ∈ today(今日新建) / old(旧计划) / unknown(日期不明)
    chase_plan_id = 目标计划 ID（poll 侧据此跳过建计划、改为对该计划追投）
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import sys

from playwright.async_api import async_playwright

from .manual_whitelist import is_manual_whitelisted, manual_label
from .commission_gate import (
    is_commission_blocked,
    load_commission_max_pct,
    parse_commission_from_card_text,
)
from .old_creator_retarget import _delete_old_plan, _open_edit_page, _wait_stable
from .plan_create import (
    NEW_VIDEO_MAX_AGE_HOURS,
    _add_video_material,
    _click_btn,
    _fresh_video_indexes,
)
from .plan_name import update_plan_name_delivery_date
from .plan_metrics import (
    CDP_URL,
    DEFAULT_DEPARTMENT,
    _ensure_cdp,
    _parse_plan_row,
    _search_plans,
)
from .session_prepare import ACCOUNT_BY_DEPARTMENT, PRODUCT_MAP, connect_browser, open_fresh_page


class SweepError(RuntimeError):
    pass


def _product_keywords(product: str) -> list[str]:
    """商品匹配关键词：商品名与商品 ID 都算（行文本/计划名命中其一即匹配）。"""
    if not product:
        return []
    if product in PRODUCT_MAP:
        return [product, PRODUCT_MAP[product]]
    if product in PRODUCT_MAP.values():
        name = {v: k for k, v in PRODUCT_MAP.items()}[product]
        return [name, product]
    return [product]


def _target_product_id(product: str) -> str:
    """记录里的商品（名或 ID）→ 千川商品 ID；查不到返回 ''。"""
    if not product:
        return ""
    if product in PRODUCT_MAP:
        return PRODUCT_MAP[product]
    if product in PRODUCT_MAP.values():
        return product
    return ""


def _find_ad_goods_map(obj, out: dict) -> None:
    """递归找接口响应里的 adGoodsMap：{计划ID: [{id: 商品ID, ...}, ...]}。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "adGoodsMap" and isinstance(v, dict):
                for plan_id, goods in v.items():
                    if isinstance(goods, list):
                        ids = {str(g.get("id")) for g in goods
                               if isinstance(g, dict) and g.get("id")}
                        if ids:
                            out.setdefault(str(plan_id), set()).update(ids)
            else:
                _find_ad_goods_map(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _find_ad_goods_map(item, out)


def _attach_goods_sniffer(page, store: dict) -> None:
    """监听计划列表接口（list-required/list-optional），收集 计划ID→商品ID集合。

    计划列表行只显示商品数量（"1 商品"）不显示商品名，行文本关键词对
    名字不带商品的计划永远无效；商品核实必须靠接口返回的 adGoodsMap。
    """
    async def _on_response(resp):
        if "uni-promotion/ad/list-" not in resp.url:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        _find_ad_goods_map(body, store)

    page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))


def _name_date_mmdd(plan_name: str) -> str | None:
    """计划名前导日期 → MMDD。本团队命名约定 MMDD_达人_姓（如 0727_煮妇小承妈_赵）。

    只认 4 位且能构成合法月日前缀；解析不了返回 None（新旧不明，不动它）。
    """
    m = re.match(r"^(\d{2})(\d{2})\D", plan_name or "")
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{month:02d}{day:02d}"


def _classify(plan_name: str, has_data: bool, today_mmdd: str) -> str:
    """是否旧计划判定：today=今日新建 / old=旧计划 / unknown=日期不明。

    有近7天数据的必然不是今天新建的（消耗/展示积累需要时间），直接归 old。
    """
    if has_data:
        return "old"
    name_date = _name_date_mmdd(plan_name)
    if name_date is None:
        return "unknown"
    return "today" if name_date == today_mmdd else "old"


async def _row_text(tr) -> str:
    # 虚拟滚动表格的行随时被回收，inner_text 会死等；evaluate 立即取、失败降级空串
    try:
        txt = await tr.evaluate("el => el.innerText || ''")
    except Exception:
        txt = ""
    return " ".join(txt.split())


async def _save_edit_page(page) -> str:
    """编辑页保存：依次尝试常见按钮文案 + 二次确认弹窗。"""
    for label in ("保存", "提交", "完成", "发布计划"):
        btn = page.get_by_role("button", name=label, exact=True).first
        try:
            if await btn.count() and await btn.is_visible():
                await btn.click()
                await _wait_stable(page, 2.5)
                for name in ("确定", "确认保存", "确认"):
                    confirm = page.get_by_role("button", name=name, exact=True).first
                    if await confirm.count() and await confirm.is_visible():
                        await confirm.click()
                        await _wait_stable(page, 1.5)
                        break
                return label
        except Exception:
            continue
    return ""


async def _edit_page_add_video(page) -> None:
    """编辑页添加最新视频素材：管理自选素材 → 添加视频 → 选第一个 → 层层确定。

    与创建页（plan_create._add_video_material）的差异仅在入口文案：
    编辑页叫「管理自选素材」，创建页叫「添加自选素材」；后续视频抽屉交互一致。
    最新视频已在素材中时卡片点不中（已添加态），抛错由调用方归类为「无需改动」。
    """
    # 1. 打开自选素材抽屉
    opened = False
    for label in ("管理自选素材", "添加自选素材"):
        try:
            await _click_btn(page, label, timeout=6.0)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const el of items) {
                const t = (el.innerText || '').trim();
                if (el.offsetParent !== null && (t.includes('管理自选素材') || t.includes('添加自选素材'))) {
                    el.click(); return;
                }
            }
            throw new Error('未找到自选素材入口');
        }""")
    await _wait_stable(page, 2.0)

    # 2. 点击「添加视频」打开视频抽屉；接口返回中含精确 createTime，
    # 必须监听而非只靠卡片排序，才能执行 72h 硬闸。
    items_future: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _capture_items(resp) -> None:
        try:
            body = await resp.json()
        except Exception:
            return
        if not items_future.done():
            items_future.set_result(body)

    def _on_response(resp) -> None:
        if "get-items-by-product" in resp.url:
            asyncio.ensure_future(_capture_items(resp))

    page.on("response", _on_response)
    try:
        await _click_btn(page, "添加视频", timeout=8.0)
    except Exception:
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                if (b.offsetParent !== null && b.innerText.trim() === '添加视频') {
                    b.click(); return;
                }
            }
        }""")
    await _wait_stable(page, 3.0)
    try:
        payload = await asyncio.wait_for(asyncio.shield(items_future), timeout=15.0)
    except asyncio.TimeoutError as exc:
        raise SweepError("视频列表接口 15s 未返回，无法核验素材是否在72小时内") from exc
    finally:
        page.remove_listener("response", _on_response)
    videos = ((payload or {}).get("data") or {}).get("videoList") or []
    fresh_indexes = _fresh_video_indexes(videos)
    if not fresh_indexes and os.environ.get("TOULIU_ALLOW_STALE_ONCE") == "1":
        # 一次性破例（2026-10-06 清妈，用户指令「投放最新的就行」）：无 ≤72h
        # 素材时按 createTime 新→旧放行全部候选。只作用于手动补跑进程，
        # 正常轮询不带此环境变量，72h 硬闸不变。
        aged = []
        for idx, video in enumerate(videos):
            try:
                aged.append((int(video.get("createTime") or 0), idx))
            except (TypeError, ValueError):
                continue
        aged.sort(reverse=True)
        fresh_indexes = [idx for _, idx in aged]
        print(f"[stale-once] 无≤{NEW_VIDEO_MAX_AGE_HOURS}h素材，一次性放行最新素材（候选 {len(fresh_indexes)} 条）")
    if not fresh_indexes:
        ages = []
        now_ts = __import__("time").time()
        for video in videos:
            try:
                ages.append((now_ts - int(video.get("createTime") or 0)) / 3600)
            except (TypeError, ValueError):
                pass
        suffix = f"（最新素材约 {min(ages):.1f} 小时前）" if ages else ""
        raise SweepError(
            f"素材超过72小时：没有发布≤{NEW_VIDEO_MAX_AGE_HOURS}小时的该商品挂车视频{suffix}，待业务员补新素材")

    # 3. 只在合格视频卡中选一张可添加、佣金合规的卡。
    max_pct = load_commission_max_pct()
    cards = page.locator('[data-auto-id="create-material-card"]')
    card = None
    blocked_cards = 0
    for _ in range(10):
        for i in fresh_indexes:
            c = cards.nth(i)
            try:
                if not await c.is_visible():
                    continue
                cls = (await c.get_attribute("class")) or ""
                if "selected-item" in cls or "disabled" in cls:
                    continue
                rate = parse_commission_from_card_text(await c.inner_text())
                if is_commission_blocked(rate, max_pct):
                    blocked_cards += 1
                    continue
                card = c
                break
            except Exception:
                continue
        if card is not None:
            break
        await _wait_stable(page, 0.8)
    if card is None:
        if blocked_cards:
            raise SweepError(
                f"佣金过高：可添加的 {blocked_cards} 张视频卡佣金率均＞上限{max_pct}%，按规则不加素材")
        raise SweepError("没有可添加的新视频（新视频或已在素材中）")
    try:
        await card.scroll_into_view_if_needed(timeout=5000)
        await card.click(timeout=8000)
    except Exception:
        await card.click(force=True)
    await _wait_stable(page, 0.5)

    # 4. 验证选中
    selected = await page.evaluate("""() => {
        const drawers = document.querySelectorAll('.ovui-drawer');
        for (let i = drawers.length - 1; i >= 0; i--) {
            const text = drawers[i].innerText;
            if (text.includes('已选择')) {
                for (const line of text.split('\\n')) {
                    if (line.includes('已选择')) return line.trim();
                }
            }
        }
        return '';
    }""")
    if "1/500" not in selected and "选择 1" not in selected:
        raise SweepError(f"视频卡片点击后未选中: {selected}")

    # 5. 视频抽屉「确定」→ 素材面板「确定/完成」
    video_confirm = page.locator(".ovui-drawer").filter(has_text="已选择").get_by_text("确定", exact=True).first
    if await video_confirm.count() == 0:
        video_confirm = page.locator(".ovui-drawer").last.get_by_text("确定", exact=True).first
    if await video_confirm.count() and await video_confirm.is_enabled():
        await video_confirm.click(force=True)
        for _ in range(15):
            await _wait_stable(page, 0.2)
            closed = True
            for i in range(await page.locator(".ovui-drawer").count()):
                d = page.locator(".ovui-drawer").nth(i)
                if await d.is_visible() and "已选择" in await d.inner_text():
                    closed = False
                    break
            if closed:
                break
        await _wait_stable(page, 1.0)

    confirmed = False
    for text in ("确定", "完成"):
        btn = page.locator(".ovui-drawer:visible").last.get_by_text(text, exact=True).first
        try:
            if await btn.count() and await btn.is_visible() and await btn.is_enabled():
                await btn.click(force=True)
                confirmed = True
                break
        except Exception:
            continue
    if not confirmed:
        # 兜底：最后一个可见 drawer 里的任何确定按钮
        btn = page.locator(".ovui-drawer").last.get_by_text("确定", exact=True).first
        if await btn.count():
            await btn.click(force=True)
    await _wait_stable(page, 1.5)


async def _add_material_to_plan(page, plan_row, plan_name: str,
                                *, update_roi: bool = True) -> tuple[str, str | None]:
    """Add the newest video and save it with any required ROI/name rewrite.

    For a non-whitelisted old plan, resetting ROI to 4.5 also stamps its name
    with today's delivery date.  Both edits and the new material are saved by
    one final click, preventing a partial ROI/name update when no material is
    actually available.
    """
    await _open_edit_page(page, plan_row)
    await _wait_stable(page, 2.0)
    # old_plan_pool imports budget_raise, which imports this module. Load the
    # edit helpers only after module initialization to avoid that cycle.
    from .old_plan_pool import _set_roi, _wait_edit_inputs

    await _wait_edit_inputs(page)
    renamed_to: str | None = None
    if update_roi:
        await _set_roi(page, 4.5)
        name_change = await update_plan_name_delivery_date(page)
        if name_change["changed"]:
            renamed_to = str(name_change["after"])
    try:
        await _edit_page_add_video(page)
    except Exception as exc:
        msg = str(exc)
        if "未选中" in msg or "未找到可选的视频卡片" in msg or "没有可添加的新视频" in msg:
            # Do not save ROI/name changes unless there is a new material.
            return "", None
        raise SweepError(f"加素材失败:{msg[:150]}") from exc
    saved = await _save_edit_page(page)
    if not saved:
        raise SweepError("已选中素材但未找到保存按钮，需人工确认编辑页")
    return saved, renamed_to


async def sweep_plans(
    creator: str,
    product: str,
    department: str = DEFAULT_DEPARTMENT,
    apply: bool = False,
    materials_only: bool = False,
    days: int = 7,
) -> dict:
    """查询并（可选）处理旧计划。materials_only 时仅加新素材，不删除计划。

    days：列表时间窗。默认 7 天（人工清理口径）；轮询管线传 30——
    2026-10-09 盲区实证：09-25 建的老计划停了 14 天无数据，7 天窗扫不到，
    预检误判「没投过」去新建撞勾选禁用（rrOW4S 70221212234 财神殿千金）。
    """
    _ensure_cdp()
    time_range = "last_7_days" if days == 7 else f"近{days}天"
    keywords = _product_keywords(product)
    target_id = _target_product_id(product)

    plans: list[dict] = []
    deleted: list[str] = []
    material_added: list[str] = []
    renamed: list[dict[str, str]] = []
    warnings: list[str] = []
    fresh_material_blocked: list[str] = []
    today_mmdd = datetime.datetime.now().strftime("%m%d")
    goods_map: dict[str, set] = {}  # 计划ID → 商品ID集合（接口嗅探）

    async with async_playwright() as pw:
        browser = await connect_browser(pw)
        # 每次开新标签页（旧标签可能被浏览器卡死导航），用毕关闭；
        # 开新页前会把积压超上限的旧千川标签从最旧的开始清掉
        page = await open_fresh_page(browser.contexts[0])
        _attach_goods_sniffer(page, goods_map)
        try:
            # 第一遍：商品匹配 + 是否旧计划判定（只读）。删除/加素材会导航/刷新表格
            # 使行句柄失效，所以改动一律在后续循环里「重新搜索 → 按 plan_id 找行 → 操作」。
            rows = await _search_plans(
                page, creator, department, by_creator=True, time_range=time_range
            )
            await page.wait_for_timeout(1000)  # 等列表接口响应全部落进 goods_map
            for tr in rows:
                m = await _parse_plan_row(tr)
                if m is None:  # 畸形行已留 WARNING，跳过
                    continue
                text = await _row_text(tr) + " " + (m.plan_name or "")
                has_data = (m.spent or 0) > 0 or (m.impression_count or 0) > 0
                # 商品核实三级判定：
                #  1) 行文本/计划名命中商品名或商品ID → 匹配（零成本）
                #  2) 接口 adGoodsMap 里该计划挂的商品ID与记录商品ID一致 → 匹配
                #     不一致 → 不是此商品的计划，不动（随后走建新计划）
                #  3) 接口没返回该计划的商品信息 → 新旧不明，一律不动
                if keywords and any(k in text for k in keywords):
                    match = True
                elif target_id and m.plan_id in goods_map:
                    match = target_id in goods_map[m.plan_id]
                else:
                    warnings.append(
                        f"{m.plan_name}: 接口未返回商品信息，无法核实商品，未处理")
                    continue
                if not match:
                    continue
                plans.append({
                    "plan_name": m.plan_name,
                    "plan_id": m.plan_id,
                    "status": m.status,
                    "spent": m.spent,
                    "impressions": m.impression_count,
                    "has_data": has_data,
                    "bucket": _classify(m.plan_name or "", has_data, today_mmdd),
                })

            # 需要动作的行：旧计划（含已暂停零消耗）→加素材+改名收养；
            # 无数据在非 materials_only 下才删除（人工清理模式）。
            # 2026-09-06 用户重申规则：查到老计划不管是否暂停（0-8 点老计划
            # 本就是暂停态），一律加新素材+改名投放日期+列新计划等 8 点开启，
            # 不得因「已暂停零消耗」跳过收养直接走建计划（会撞商品级权限）。
            actionable = [
                p for p in plans
                if p["bucket"] == "old"
            ]
            for p in plans:
                if p["bucket"] == "unknown":
                    warnings.append(
                        f"{p['plan_name']}: 计划名无日期前缀，新旧无法判定，未处理")

            if apply and actionable:
                acted: set[str] = set()
                while True:
                    target = None
                    for p in actionable:
                        key = p["plan_id"] or p["plan_name"]
                        if key and key not in acted:
                            target = p
                            break
                    if target is None:
                        break
                    key = target["plan_id"] or target["plan_name"]
                    acted.add(key)

                    # 重新搜索拿新鲜行句柄，并按 plan_id/名称精确定位
                    rows = await _search_plans(
                        page, creator, department, by_creator=True,
                        time_range=time_range,
                    )
                    tr = None
                    for cand in rows:
                        m = await _parse_plan_row(cand)
                        if m is None:  # 畸形行已留 WARNING，跳过
                            continue
                        if (target["plan_id"] and m.plan_id == target["plan_id"]) or \
                                (not target["plan_id"] and m.plan_name == target["plan_name"]):
                            tr = cand
                            break
                    if tr is None:
                        # 行已不在（上一轮刚删掉/平台延迟），视为已处理
                        continue
                    try:
                        whitelisted = is_manual_whitelisted(
                            creator=creator,
                            department=department,
                            plan_id=target["plan_id"] or "",
                            plan_name=target["plan_name"] or "",
                        )
                        # 已暂停零消耗老计划也要收养加素材（2026-09-06 用户规则：
                        # 0-8 点老计划本就暂停，查到就加素材+改名+列新计划等 8 点），
                        # 与有数据老计划同走 _add_material_to_plan（改名/ROI 已在函数内）。
                        if target["has_data"] or materials_only:
                            saved, renamed_to = await _add_material_to_plan(
                                page, tr, target["plan_name"] or "",
                                update_roi=not whitelisted)
                            # 收养的无数据老计划改名后按新计划处理：标记 plan_id 供
                            # chase 兜底（poll 据此跳过建计划、挂 08:00 新计划票）。
                            if saved and not target["has_data"]:
                                target["adopted_new"] = True
                            if saved:
                                suffix = "；保留原ROI/预算" if whitelisted else ""
                                if not target["has_data"]:
                                    suffix += "；收养老计划列新计划"
                                shown_name = target["plan_name"]
                                if renamed_to:
                                    renamed.append({
                                        "plan_id": str(target["plan_id"] or ""),
                                        "before": shown_name,
                                        "after": renamed_to,
                                    })
                                    shown_name = f"{shown_name}→{renamed_to}"
                                material_added.append(
                                    f"{shown_name}（保存:{saved}{suffix}）")
                            else:
                                material_added.append(
                                    f"{target['plan_name']}（新视频或已在素材中）")
                                # 无新视频可加也算收养目标（计划已存在，无需新建）
                                if not target["has_data"]:
                                    target["adopted_new"] = True
                        elif not materials_only:
                            if whitelisted:
                                warnings.append(
                                    f"{target['plan_name']}: {manual_label(department=department, plan_id=target['plan_id'] or '', plan_name=target['plan_name'] or '')}人工白名单，未删除旧计划")
                            else:
                                await _delete_old_plan(
                                    page, tr, target["plan_id"] or target["plan_name"])
                                deleted.append(target["plan_name"])
                    except Exception as exc:
                        detail = str(exc)[:150]
                        warnings.append(f"{target['plan_name']}: {detail}")
                        if "素材超过72小时" in detail:
                            fresh_material_blocked.append(target["plan_name"])
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # 追投/免建计划目标：今日新建的计划、有数据的旧计划（已加新素材），
    # 或本次收养的无数据老计划（adopted_new，已加素材+改名，列新计划等 8 点）。
    active = [p for p in plans if p["bucket"] == "today" or p["has_data"]]
    has_active = bool(active)
    chase_plan_id = (active[0]["plan_id"] or "") if has_active else ""
    # 无在投目标但有收养的无数据老计划：也算 chase 目标（免建新计划），
    # poll 侧据此跳过建计划、改为挂 08:00 新计划恢复票。
    if not chase_plan_id:
        adopted = [p for p in plans if p.get("adopted_new") and p["plan_id"]]
        if adopted:
            chase_plan_id = adopted[0]["plan_id"] or ""
            has_active = True

    if not plans:
        message = "未找到该达人此商品的旧计划，走正常建计划"
    else:
        parts = [f"匹配{len(plans)}条"]
        if active:
            parts.append(f"目标计划{len(active)}条（免建新计划，追投即可）")
        old_empty = [p for p in plans if p["bucket"] == "old" and not p["has_data"]]
        if old_empty:
            parts.append(f"旧计划无数据{len(old_empty)}条")
        if apply:
            if deleted:
                parts.append(f"已删{len(deleted)}条")
            if material_added:
                parts.append(f"已加素材{len(material_added)}条")
            if warnings:
                parts.append(f"警告{len(warnings)}条")
        else:
            parts.append("（未加 --apply，仅查询）")
        message = "；".join(parts)

    # 收养的无数据老计划（已暂停，已加素材+改名，列新计划等 8 点）：
    # 单独暴露其 plan_id / 改名后名称，poll 侧据此挂 08:00 新计划恢复票，
    # 而不是按在投老计划「原窗口运行」跳过暂停待发。
    adopted_new_plan_id = ""
    adopted_new_plan_name = ""
    for p in plans:
        if p.get("adopted_new") and p["plan_id"]:
            adopted_new_plan_id = p["plan_id"] or ""
            # 改名后的名称优先（renamed 记录 before→after）
            adopted_new_plan_name = p["plan_name"] or ""
            for rn in renamed:
                if str(rn.get("plan_id") or "") == str(p["plan_id"] or ""):
                    adopted_new_plan_name = rn.get("after") or adopted_new_plan_name
            break

    return {
        "success": True,
        "creator": creator,
        "product": product,
        "department": department,
        "matched": len(plans),
        "plans": plans,
        "deleted": deleted,
        "material_added": material_added,
        "renamed": renamed,
        "warnings": warnings,
        "fresh_material_blocked": fresh_material_blocked,
        "has_active": has_active,
        "chase_plan_id": chase_plan_id,
        "adopted_new_plan_id": adopted_new_plan_id,
        "adopted_new_plan_name": adopted_new_plan_name,
        "message": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="旧计划清理：近7天无数据删除，有数据加新素材")
    parser.add_argument("--creator", required=True, help="抖音号 ID/昵称")
    parser.add_argument("--product", required=True, help="商品名或商品 ID")
    parser.add_argument("--department", default=DEFAULT_DEPARTMENT,
                        choices=tuple(ACCOUNT_BY_DEPARTMENT))
    parser.add_argument("--apply", action="store_true",
                        help="实际执行删除/加素材；默认只查询报告")
    parser.add_argument("--materials-only", action="store_true",
                        help="实际执行时仅给有数据的旧计划加素材，不删除无数据计划")
    parser.add_argument("--days", type=int, default=7,
                        help="列表时间窗天数（默认 7=人工清理口径；轮询管线传 30，"
                             "防止停了 7 天以上的老计划扫不到而误判「没投过」）")
    args = parser.parse_args()

    try:
        result = asyncio.run(sweep_plans(
            args.creator, args.product, args.department, apply=args.apply,
            materials_only=args.materials_only, days=args.days))
        ok = True
    except Exception as exc:
        result = {"success": False, "message": str(exc)[:300]}
        ok = False

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
