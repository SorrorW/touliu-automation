# -*- coding: utf-8 -*-
"""commission_gate.py —— 达人带货佣金率拦截（2026-09-07）。

规则：佣金率 > 上限（默认 5%）不投放，=上限仍投。两道闸：
- 建计划闸（实时）：plan_create 选达人抽屉读佣金率，超标抛 CommissionBlockedError；
- 存量闸（每日一次）：建计划页「添加抖音号」抽屉按（达人×商品）逐达人搜索读佣金率
  → join plan_catalog 在投计划 → 超标且非白名单 → 批量暂停 + 写阻断名单
  （阻止 08:00/12:00 恢复队列再开回来）。

⚠️ 佣金率是（达人×商品）维度：2026-09-16 实证 小沈妈(989385568) 对肥牛 10%、
对另一商品 5%——存量闸必须按商品分组各扫一次，不能跨商品混用。
⚠️ 抖音号授权页/投放授权页均无佣金率列（2026-09-16 探针实证，表头仅授权状态等），
旧 scan_aweme_commissions 的授权页来源已失效，唯一可靠批量来源=建计划抽屉。

阈值唯一源：wecom-agent/config.json 的 commission_max_pct（缺省 5.0）。
判定统一 rate > max_pct（=max 放行）；读不到佣金率放行+记日志。

⚠️ 模块顶部只 import 标准库 + pathlib：plan_sweep 反向 import plan_create，
若本模块顶层依赖 plan_power 会形成循环 import。auth/plan_power/webui.db 全部
函数内 lazy import。plan_create 只从这里 import 纯函数与 CommissionBlockedError。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_COMMISSION_MAX_PCT = 5.0
CONFIG_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "config.json"
STATE_FILE = Path(__file__).resolve().parent / "commission_gate_state.json"
# 读不到佣金率时放行+记日志（用户 2026-09-07 定），收紧改 "block"。
COMMISSION_UNKNOWN_POLICY = "allow"
# 存量闸兜底商品：plan_registry 无 商品ID 时按肥牛扫（绝大部分计划都是它）。
DEFAULT_PRODUCT_ID = "3625226808611490412"

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_ID_RE = re.compile(r"ID[:：]\s*([0-9A-Za-z]+)")
# _norm 用：匹配只认字母/数字/CJK，emoji/空格/标点全部剥掉。
_NORM_CHARS_RE = re.compile(r"[0-9A-Za-z一-鿿]+")
# 素材卡：佣金率带「佣金率:」标签（用户 2026-09-08 截图实证「佣金率: 15%」）。
# 不能用首个百分比——卡片标题可能含其它 % 文案（如「5折」），必须认标签。
_CARD_PCT_RE = re.compile(r"佣金率[:：]\s*(\d+(?:\.\d+)?)\s*[%％]")


class CommissionBlockedError(RuntimeError):
    """佣金率超上限，业务终态；message 必含「佣金过高」供 poll 侧 _is_fatal 识别。"""


# ---------- 纯函数（可单测，无浏览器） ----------

def load_commission_max_pct(path: Path | None = None) -> float:
    """读 config.json 的 commission_max_pct；缺文件/缺键/坏 JSON → 5.0。"""
    p = path or CONFIG_FILE
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        return float(raw.get("commission_max_pct", DEFAULT_COMMISSION_MAX_PCT))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return DEFAULT_COMMISSION_MAX_PCT


def parse_commission_from_row_text(text: str) -> float | None:
    """从达人行文本抽佣金率：取第一个百分比（列序语义=带货佣金率列）。
    '--'/'—'/无百分比 → None。兼容全角％。"""
    m = _PCT_RE.search(text or "")
    return float(m.group(1)) if m else None


def parse_commission_from_card_text(text: str) -> float | None:
    """从素材卡文本抽佣金率：只认「佣金率: N%」标签，无标签 → None（放行）。

    素材卡结构：竖版 时长 / 首发素材角标 / 标题 / 佣金率: 15% / 点赞数。
    标题可能含其它 % 文案，故不复用首个百分比规则。
    """
    m = _CARD_PCT_RE.search(text or "")
    return float(m.group(1)) if m else None


def is_commission_blocked(rate: float | None, max_pct: float) -> bool:
    """rate None → False（未知放行）；> max + 1e-9 → True；=max 放行。"""
    if rate is None:
        return False
    return rate > max_pct + 1e-9


def creator_name_from_plan_name(plan_name: str) -> str:
    """从计划名抽达人显示名（复刻 plan_metrics._parse_plan_row 的命名正则）。"""
    for pattern in [
        r"^\d{2,4}[-_]\d{1,2}[-_]\s*(.+?)\s*[-_]",
        r"\d{2,4}[-_]\s*(.+?)\s*[-_]",
        r"^[\d.]+[-_]?\s*(.+?)\s*[-_]",
        r"[\d.]+[-_\s]*(.+?)\s*[（(]",
    ]:
        m = re.search(pattern, plan_name or "")
        if m:
            return m.group(1).strip()
    return ""


def _norm(value: str) -> str:
    """匹配用规范化：只保留字母/数字/中日韩文字并小写化。

    2026-09-16：千川抽屉显示名带 emoji（Yan🍓的美食日记），计划名/台账名不带，
    旧「去空白+小写」规范化对不上 → 存量闸 by_name 全落 unknown 桶。
    """
    return "".join(_NORM_CHARS_RE.findall(str(value or ""))).lower()


def match_plan_commission(plan: dict, by_id: dict, by_name: dict) -> float | None:
    """join：creator_name 命中 by_id（poll 来源存的是抖音号 ID）→ 计划名抽达人名
    规范化后命中 by_name → 都不中 None（记 unmatched 不拦）。"""
    cn = str(plan.get("creator_name") or "").strip()
    if cn and cn in by_id:
        return by_id[cn]
    name = creator_name_from_plan_name(str(plan.get("plan_name") or ""))
    key = _norm(name)
    if key and key in by_name:
        return by_name[key]
    # creator_name 也可能是显示名（monitor 来源），再试一次规范化
    cn_key = _norm(cn)
    if cn_key and cn_key in by_name:
        return by_name[cn_key]
    return None


def select_pause_targets(plans, by_id, by_name, max_pct, *, whitelisted):
    """返回 (targets, skipped_whitelist, skipped_unknown)。只选在投（含「投放中」
    且不含「暂停」）行；whitelisted 为注入回调（生产=is_manual_whitelisted）。"""
    targets, skipped_whitelist, skipped_unknown = [], [], []
    for p in plans:
        status = str(p.get("plan_status") or "")
        if "投放中" not in status or "暂停" in status:
            continue
        if whitelisted(department=str(p.get("department") or ""),
                       plan_id=str(p.get("plan_id") or ""),
                       plan_name=str(p.get("plan_name") or "")):
            skipped_whitelist.append(p)
            continue
        rate = match_plan_commission(p, by_id, by_name)
        if rate is None:
            skipped_unknown.append(p)
            continue
        if is_commission_blocked(rate, max_pct):
            targets.append({**p, "commission_rate": rate})
    return targets, skipped_whitelist, skipped_unknown


def load_commission_blocked_ids(state_file: Path | None = None) -> set[str]:
    """读阻断名单的 plan_id 集合；缺文件/坏 JSON → 空集。"""
    p = state_file or STATE_FILE
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        return set((raw.get("blocked") or {}).keys())
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def save_commission_blocked(entries: dict, state_file: Path | None = None) -> None:
    """合并写入阻断名单（key=plan_id）。不按日重置，终态直到人工解禁。"""
    p = Path(state_file or STATE_FILE)
    existing: dict = {}
    try:
        existing = json.loads(p.read_text(encoding="utf-8")).get("blocked") or {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing.update(entries)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"blocked": existing,
                               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ---------- 存量闸：商品分组 + 查询构造（纯函数，可单测） ----------

def group_plans_by_product(plans: list[dict], registry: dict) -> dict[str, list[dict]]:
    """按商品分组在投计划：registry={plan_id:{creator_id,product_id}}；缺省 DEFAULT_PRODUCT_ID。

    佣金率是（达人×商品）维度，不同商品必须分开开抽屉各扫一次。
    """
    groups: dict[str, list[dict]] = {}
    for p in plans:
        reg = registry.get(str(p.get("plan_id") or ""), {})
        prod = (reg.get("product_id") or "").strip() or DEFAULT_PRODUCT_ID
        groups.setdefault(prod, []).append(p)
    return groups


def build_creator_queries(plans: list[dict], registry: dict) -> list[dict]:
    """去重出抽屉搜索列表 [{key, cid, name}]：有 registry creator_id 用 ID 搜（精确，
    避免昵称撞车），否则用显示名/计划名解析名搜。key 用于结果回连。"""
    out: list[dict] = []
    seen: set[str] = set()
    for p in plans:
        reg = registry.get(str(p.get("plan_id") or ""), {})
        cid = (reg.get("creator_id") or "").strip()
        name = creator_name_from_plan_name(p.get("plan_name") or "") \
            or (p.get("creator_name") or "").strip()
        key = cid or _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "cid": cid, "name": name})
    return out


def load_registry_plan_map(conn) -> dict[str, dict]:
    """plan_registry → {plan_id: {creator_id, product_id}}（product_id 取 metadata_json 商品ID）。

    conn 是 webui.db 的 sqlite 连接（调用方持有）；表不存在/无数据 → {}。
    """
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT plan_id, creator_id, metadata_json FROM plan_registry "
            "WHERE plan_id IS NOT NULL AND plan_id != ''").fetchall()
    except Exception:  # 表不存在等，台账缺失不阻断闸门
        return out
    for r in rows:
        prod = ""
        try:
            meta = json.loads(r["metadata_json"] or "{}")
            prod = str(meta.get("商品ID") or "").strip()
        except (ValueError, TypeError, AttributeError):
            prod = ""
        out[str(r["plan_id"])] = {"creator_id": str(r["creator_id"] or "").strip(),
                                  "product_id": prod}
    return out


def pick_drawer_candidate(cands: list[dict], query: dict) -> dict | None:
    """从抽屉搜索候选行里挑出目标达人：ID 精确 > 名称规范化相等 > 唯一候选兜底。"""
    if query.get("cid"):
        exact = [c for c in cands if c.get("cid") == query["cid"]]
        if exact:
            return exact[0]
    if query.get("name"):
        exact = [c for c in cands if _norm(c.get("name") or "") == _norm(query["name"])]
        if exact:
            return exact[0]
    return cands[0] if len(cands) == 1 else None


# ---------- 浏览器部分（lazy import） ----------

async def scan_drawer_commissions(department: str, queries: list[dict], product_id: str,
                                  ctx=None, *, max_queries: int = 60) -> dict[str, dict]:
    """建计划页「添加抖音号」抽屉逐达人搜索读佣金率（2026-09-16 替代失效的授权页扫描）。

    对每个 query：有 cid 按 ID 搜、否则按显示名搜，读结果行的「带货佣金率」。
    返回 {query.key: {cid, name, rate, raw}}；未匹配的行 rate=None（按放行政策进 unknown 桶）。
    绝不勾选、绝不提交、绝不发布：收尾 Escape 关抽屉 + 关标签。
    ctx 为已有浏览器上下文（plan_power 任务复用同一连接）；None 则自连 CDP。
    """
    from playwright.async_api import async_playwright  # lazy
    from . import plan_create, session_prepare, plan_metrics  # lazy

    results: dict[str, dict] = {}
    if not queries:
        return results

    async def _run(context) -> None:
        page = await session_prepare.open_fresh_page(context)
        try:
            page = await plan_create._enter_create_page(page, department)
            await plan_create._select_product(page, product_id)
            await plan_create._open_creator_drawer(page)
            search = page.locator('input[placeholder*="请输入抖音号"]').first
            await search.wait_for(state="attached", timeout=15000)
            for q in queries[:max_queries]:
                kw = q["cid"] or q["name"]
                if not kw:
                    continue
                try:
                    await search.fill(kw)
                    await search.press("Enter")
                    await plan_create._wait_stable(page, 2.0)
                    row_texts = await page.evaluate("""() => {
                        const drawers = document.querySelectorAll('.ovui-drawer');
                        if (!drawers.length) return [];
                        const d = drawers[drawers.length - 1];
                        return Array.from(d.querySelectorAll('tr, [class*="row"]'))
                            .map(r => r.innerText || '').filter(t => t.indexOf('ID') >= 0);
                    }""")
                except Exception as exc:
                    log.warning("[%s] 抽屉搜索 %s 失败: %s", department, kw, str(exc)[:120])
                    row_texts = []
                cands = []
                for text in row_texts:
                    m = _ID_RE.search(text)
                    if not m:
                        continue
                    name = text[: m.start()].strip().split("\n")[-1] if m.start() > 0 else ""
                    cands.append({"cid": m.group(1), "name": name,
                                  "rate": parse_commission_from_row_text(text),
                                  "raw": " ".join(text.split())[:120]})
                pick = pick_drawer_candidate(cands, q)
                if pick:
                    results[q["key"]] = pick
                else:
                    results[q["key"]] = {"cid": q["cid"], "name": q["name"],
                                         "rate": None, "raw": "抽屉未匹配"}
        finally:
            try:
                await page.keyboard.press("Escape")
                await plan_create._wait_stable(page, 0.8)
            except Exception:
                pass
            try:
                await page.close()
            except Exception:
                pass

    if ctx is not None:
        await _run(ctx)
    else:
        plan_metrics._ensure_cdp()
        async with async_playwright() as pw:
            browser = await session_prepare.connect_browser(pw, timeout=8000)
            await _run(browser.contexts[0])
    truncated = len(queries) - min(len(queries), max_queries)
    log.info("[%s] 抽屉佣金扫描 商品=%s 查询%d 命中%d%s", department, product_id,
             min(len(queries), max_queries),
             sum(1 for v in results.values() if v.get("rate") is not None),
             f"（截断{truncated}）" if truncated > 0 else "")
    return results


async def scan_aweme_commissions(department: str, page=None) -> dict[str, dict]:
    """【已失效 2026-09-16】授权页不再有佣金率列，保留仅供对照，勿再调用。

    在抖音号授权页全表读 {creator_id: {name, rate, status}}。
    """
    rates: dict[str, dict] = {}
    seen_pages = 0
    while True:
        seen_pages += 1
        rows = page.locator(".bui-table-tr, tr.ovui-tr")
        n = await rows.count()
        for i in range(n):
            text = " ".join((await rows.nth(i).inner_text()).split())
            m = _ID_RE.search(text)
            if not m:
                continue
            cid = m.group(1)
            name = text[: m.start()].strip().split("\n")[-1] if m.start() > 0 else ""
            rates[cid] = {"name": name, "rate": parse_commission_from_row_text(text),
                          "raw": text[:120]}
        # 翻页：找「下一页」按钮，禁用即停（选择器需探针校准）
        next_btn = page.locator(
            'button:has-text("下一页"), .bui-pagination-next, li[title="下一页"]').first
        try:
            disabled = await next_btn.get_attribute("disabled")
            cls = (await next_btn.get_attribute("class")) or ""
            if disabled is not None or "disabled" in cls:
                break
            await next_btn.click()
            await page.wait_for_timeout(1500)
        except Exception:
            break  # 无分页/翻页失败 → 用已读到的
        if seen_pages > 50:
            break
    log.info("[%s] 授权页扫描 %d 个达人佣金率（%d 页）", department, len(rates), seen_pages)
    return rates


async def commission_gate_run(dry_run: bool = False,
                              departments: list[str] | None = None) -> dict:
    """存量闸主流程。返回 summary dict。"""
    from . import plan_power  # lazy
    from .manual_whitelist import is_manual_whitelisted  # lazy
    from webui import db as webui_db  # lazy（需 投流/ 在 sys.path）

    max_pct = load_commission_max_pct()
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    deps = departments or cfg.get("commission_gate_departments") or list(
        plan_power.ACCOUNT_BY_DEPARTMENT.keys())
    if isinstance(deps, str):
        deps = [deps]

    summary = {"max_pct": max_pct, "dry_run": dry_run, "departments": deps,
               "scanned_creators": 0, "scanned": {}, "paused": [], "already": [],
               "failed": [], "whitelist_skipped": [], "unmatched_plans": [],
               "targets": []}

    import sqlite3
    from playwright.async_api import async_playwright

    conn = sqlite3.connect(str(webui_db.DB_FILE))
    conn.row_factory = sqlite3.Row

    # 开一个浏览器，扫描与暂停复用同一连接（授权页和计划页在同一账号上下文）
    plan_power._ensure_cdp()
    async with async_playwright() as pw:
        browser = await plan_power.connect_browser(pw, timeout=8000)
        ctx = browser.contexts[0]
        for dep in deps:
            rows = conn.execute(
                "select plan_id, plan_name, creator_name, plan_status, department "
                "from plan_catalog where department=? and plan_status like '%投放中%'", (dep,)).fetchall()
            plans = [dict(r) for r in rows]
            registry = load_registry_plan_map(conn)
            dep_targets: list[dict] = []
            # 佣金率是（达人×商品）维度：按商品分组，每组开一次抽屉逐达人搜索
            for product_id, group in group_plans_by_product(plans, registry).items():
                queries = build_creator_queries(group, registry)
                rates = await scan_drawer_commissions(dep, queries, product_id, ctx=ctx)
                summary["scanned_creators"] += sum(
                    1 for v in rates.values() if v.get("rate") is not None)
                summary["scanned"][f"{dep}@{product_id}"] = {
                    k: {"cid": v.get("cid"), "name": v.get("name"),
                        "rate": v.get("rate"), "raw": v.get("raw")}
                    for k, v in rates.items()
                }
                by_id = {k: v["rate"] for k, v in rates.items()
                         if v.get("rate") is not None}
                by_name = {_norm(v["name"]): v["rate"] for k, v in rates.items()
                           if v.get("name") and v.get("rate") is not None}
                targets, wl, unknown = select_pause_targets(
                    group, by_id, by_name, max_pct,
                    whitelisted=lambda **kw: is_manual_whitelisted(**kw))
                summary["whitelist_skipped"].extend(
                    {"plan_id": p.get("plan_id"), "plan_name": p.get("plan_name")} for p in wl)
                summary["unmatched_plans"].extend(
                    {"plan_id": p.get("plan_id"), "plan_name": p.get("plan_name")} for p in unknown)
                summary["targets"].extend(
                    {"plan_id": t.get("plan_id"), "plan_name": t.get("plan_name"),
                     "rate": t.get("commission_rate"), "department": dep,
                     "product_id": product_id} for t in targets)
                dep_targets.extend(targets)
            if not dep_targets:
                continue
            log.info("[%s] 佣金闸目标 %d 条（>%.1f%%）", dep, len(dep_targets), max_pct)
            if dry_run:
                continue
            page = await plan_power._department_page(ctx, dep)
            result = await plan_power._batch_pause_targets(page, dep, dep_targets)
            paused = result.get("paused") or []
            for t in paused:
                summary["paused"].append({"plan_id": t.get("plan_id"),
                                          "plan_name": t.get("plan_name"), "department": dep})
            for t in result.get("already") or []:
                summary["already"].append({"plan_id": t.get("plan_id"),
                                           "plan_name": t.get("plan_name"), "department": dep})
            for t in result.get("failed") or []:
                summary["failed"].append({"plan_id": t.get("plan_id"),
                                          "plan_name": t.get("plan_name"), "department": dep})
            # 同步 plan_catalog + 写阻断名单（本部门）
            blocked_entries = {}
            for t in paused:
                pid = str(t.get("plan_id") or "")
                try:
                    webui_db.upsert_plan_from_poll(
                        {"plan_id": pid, "plan_name": t.get("plan_name"), "status": "已暂停"},
                        department=dep)
                except Exception as exc:
                    log.error("plan_catalog 同步失败 %s: %s", pid, str(exc)[:120])
                blocked_entries[pid] = {
                    "plan_name": t.get("plan_name"), "department": dep,
                    "rate": t.get("commission_rate"), "blocked_at": time.time()}
            if blocked_entries:
                save_commission_blocked(blocked_entries)
    conn.close()
    return summary


def main() -> int:
    import argparse
    import asyncio as _asyncio

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="达人佣金率存量闸（>上限不投）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--department", default="")
    ap.add_argument("--unblock-plan-id", default="", help="人工解禁某条计划（移出阻断名单）")
    args = ap.parse_args()

    if args.unblock_plan_id:
        p = STATE_FILE
        raw = {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        removed = (raw.get("blocked") or {}).pop(args.unblock_plan_id, None)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        print(json.dumps({"unblocked": args.unblock_plan_id, "was_blocked": bool(removed)},
                         ensure_ascii=False))
        return 0

    deps = [args.department] if args.department else None
    out = _asyncio.run(commission_gate_run(dry_run=args.dry_run, departments=deps))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
