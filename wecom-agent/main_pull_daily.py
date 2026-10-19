# -*- coding: utf-8 -*-
"""主号自动拉取达人日报（2026-10-13 用户定）：

每日 09:10 / 13:30 / 17:30 计划任务：抓主号四大主品计划达人绑定 → 落库（main_pull_snapshots）
→ 与企微任务表对比回填业务部/业务员 → 与上一快照 diff → 汇总群播报新增/删除
（抖音号 ID + 名称）。首日 --baseline 只播存量概览。

CLI: python main_pull_daily.py [--baseline] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import logging
import os
import subprocess
import sys
import urllib.request

if sys.stdout is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
TOULIU_DIR = os.path.normpath(os.path.join(HERE, "..", "投流"))
sys.path.insert(0, HERE)  # wecom_client 在本目录
sys.path.insert(0, TOULIU_DIR)
sys.path.insert(0, os.path.join(TOULIU_DIR, "webui"))

import db as plan_db  # noqa: E402
from 巨量千川.main_products import product_route, clean_id, MAIN_AAVID, MAIN_PRODUCTS
from 巨量千川 import main_product_store as main_store
from wecom_client import WeComClient, simplify_record  # noqa: E402

LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log = logging.getLogger("main_pull_daily")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(
    os.path.join(LOG_DIR, f"main_pull_{datetime.date.today():%Y%m%d}.log"),
    encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
if sys.stdout is not None:  # pythonw（计划任务）下 stdout/stderr 皆 None
    log.addHandler(logging.StreamHandler(sys.stdout))

_PY = os.path.join(TOULIU_DIR, "runtime", "python.exe")


def run_scout() -> dict:
    """主号监控与表格授权共用浏览器互斥，锁忙时留待后续任务。"""
    from main_account_flow import browser_guard
    with browser_guard():
        return _run_scout_unlocked()


def _run_scout_unlocked() -> dict:
    """子进程跑只读探针（隔离浏览器状态），末行 JSON：{rows, oplogs}。"""
    p = subprocess.run(
        [_PY, "-X", "utf8", "-m", "巨量千川.main_pull_scout", "--with-oplog"],
        cwd=TOULIU_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        raise RuntimeError(f"探针失败 rc={p.returncode}: {(p.stderr or '')[-200:]}")
    return json.loads(out.splitlines()[-1])


def sheet_creator_map() -> dict[str, dict]:
    """企微主表+旧素材表：抖音号 → {dept, seller}（单元格去空白/尾逗号脏数据）。"""
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    m: dict[str, dict] = {}
    for sid in (cfg["sheet_id"], cfg.get("old_material_sheet_id")):
        if not sid:
            continue
        c = WeComClient(dict(cfg))
        c.cfg["sheet_id"] = sid
        for r in c.get_all_records():
            s = simplify_record(r) or {}
            cid = str(s.get("抖音号") or "").strip().rstrip(",").strip()
            route=product_route(s.get("商品ID"))
            key=(clean_id(cid),route.plan_id) if route else None
            if key and key not in m:
                m[key] = {"dept": s.get("业务部") or "", "seller": s.get("业务部") and (s.get("业务员") or "") or ""}
    return m


_ACTIVE_PROGRESS = ("", "待处理", "待授权", "允许授权", "等待达人接受",
                    "等待21点进行授权", "可投放", "待主号授权", "待加入主品", "主号已拉取", "主号已加入")


def active_sheet_creators() -> list[dict]:
    """两表在队票（非终态进度）→ [{cid, dept, seller, progress}]。

    「填表未拉取」播报用（2026-10-14 用户：这些也要播报，推动解决）。
    """
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    out: list[dict] = []
    seen: set[str] = set()
    for sid in (cfg["sheet_id"], cfg.get("old_material_sheet_id")):
        if not sid:
            continue
        c = WeComClient(dict(cfg))
        c.cfg["sheet_id"] = sid
        for r in c.get_all_records():
            s = simplify_record(r) or {}
            prog = (s.get("进度") or "").strip()
            if prog not in _ACTIVE_PROGRESS:
                continue
            cid = str(s.get("抖音号") or "").strip().rstrip(",").strip()
            route=product_route(s.get("商品ID"))
            key=(clean_id(cid),route.plan_id) if route else None
            if not key or key in seen:
                continue
            from main_account_flow import stop_reason
            if stop_reason(s):continue
            seen.add(key)
            out.append({"cid": clean_id(cid), "product_id":route.product_id, "plan_id":route.plan_id, "product":route.name, "dept": s.get("业务部") or "未填部门",
                        "seller": s.get("业务员") or "", "progress": prog or "待处理"})
    return out


def diff_snapshots(today: list[dict], prev: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 creator_id×plan_key 对比。新增=今天有昨天无；删除=昨天在投今天无或转已删除。"""
    def key(r):
        return (r["creator_id"], r["plan_key"])
    t_map = {key(r): r for r in today}
    p_map = {key(r): r for r in prev}
    added = [r for k, r in t_map.items() if k not in p_map]
    removed = [r for k, r in p_map.items()
               if k not in t_map
               or (r.get("bind_status") == "在投"
                   and t_map[k].get("bind_status") == "已删除")]
    return added, removed


def build_report(today: list[dict], added: list[dict], removed: list[dict],
                 baseline: bool, snap_date: str,
                 oplogs: dict | None = None) -> str:
    def fmt(r):
        dept = r.get("dept") or "未填表"
        return f"{r.get('creator_name')}（{r.get('creator_id')}）·{r.get('plan_key')}·{dept}"
    lines = [f"**【主号拉取达人日报】{snap_date}**"]
    if baseline:
        auto_alive = [r for r in today
                      if r.get("bind_type") == "自动拉取" and r.get("bind_status") == "在投"]
        unfiled = [r for r in auto_alive if not r.get("dept")]
        lines.append(f"首日基线：绑定 {len(today)} 席，自动拉取在投 {len(auto_alive)} 席，"
                     f"其中未填表 {len(unfiled)} 席")
        lines.append("明日起只播新增/删除。")
        return "\n".join(lines)
    if not added and not removed:
        return ""  # 无变化不刷屏
    if added:
        lines.append(f"**新增 {len(added)}**：")
        lines += ["- " + fmt(r) for r in added]
    if removed:
        lines.append(f"**删除 {len(removed)}**：")
        lines += ["- " + fmt(r) for r in removed]
    # 操作日志佐证：有变化的计划附上近期「抖音号」操作（谁什么时候拉/删）
    if oplogs:
        changed_plans = {r.get("plan_key") for r in added + removed}
        for pk in sorted(p for p in changed_plans if p):
            entries = (oplogs.get(pk) or [])[:5]
            if entries:
                lines.append(f"**{pk} 操作日志**：")
                lines += [f"- {e[:150]}" for e in entries]
    return "\n".join(lines)


def push_summary_group(content: str) -> bool:
    """汇总群推送（复用日报 webhook 管道）。"""
    from daily_report_push import load_webhooks
    hooks = load_webhooks()
    url = hooks.get("汇总")
    if not url:
        log.error("webhooks 里没有「汇总」群")
        return False
    req = urllib.request.Request(
        url, data=json.dumps({"msgtype": "markdown",
                              "markdown": {"content": content}}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        if body.get("errcode", 0) == 0:
            log.info("达人日报已推送到「汇总」群")
            return True
        log.warning("推送失败: %s", body)
    except Exception as exc:
        log.warning("推送异常: %s", exc)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="首日基线：只播存量概览")
    ap.add_argument("--dry-run", action="store_true", help="只落库+打印，不推送")
    args = ap.parse_args()
    snap_id = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    log.info("主号达人日报启动 baseline=%s dry_run=%s", args.baseline, args.dry_run)
    result = run_scout()
    rows, oplogs = result["rows"], result.get("oplogs") or {}
    log.info("抓取 %d 条绑定；操作日志 %d 个计划", len(rows), len(oplogs))
    if not rows:
        log.error("抓取为空，本轮不落库不播报（防误报删除）")
        return 1

    if not result.get("complete") or {s.get("plan_id") for s in result.get("scans",[]) if s.get("complete")} != {r.plan_id for r in MAIN_PRODUCTS}:
        log.error("四主品扫描未完整，不落库不播报")
        return 1
    for route in MAIN_PRODUCTS:
        main_store.save_scan(route,[r for r in rows if r.get("plan_id")==route.plan_id],complete=True)

    cmap = sheet_creator_map()
    for r in rows:
        hit = cmap.get((clean_id(r.get("creator_id")),r.get("plan_id")))
        if hit:
            r["dept"], r["seller"] = hit["dept"], hit["seller"]
    n = plan_db.save_main_pull_snapshot(rows, snap_id)
    log.info("落库 %d 条（%s）", n, snap_id)

    prev_date = plan_db.latest_main_pull_date_before(snap_id)
    prev = plan_db.load_main_pull_snapshot(prev_date) if prev_date else []
    log.info("对比基线: %s（%d 条）", prev_date, len(prev))
    added, removed = ([], []) if args.baseline else diff_snapshots(rows, prev)
    log.info("diff: 新增 %d 删除 %d", len(added), len(removed))

    content = build_report(rows, added, removed, args.baseline, snap_id,
                           oplogs=oplogs)

    # 填表未拉取段（2026-10-14 用户：填了表还没被主号拉取的也要播报，推动解决）
    alive_ids = {(clean_id(r.get("creator_id")),r.get("plan_id")) for r in rows if r.get("bind_status") == "在投"}
    try:
        active = active_sheet_creators()
    except Exception as exc:
        log.warning("在队票读取失败（未拉取段跳过）: %s", str(exc)[:120])
        active = []
    unpulled = [t for t in active if (t["cid"],t["plan_id"]) not in alive_ids]
    unpulled = sorted(unpulled, key=lambda t: (t["dept"], t["cid"]))
    unpulled_fp = "|".join(t["cid"]+":"+t["plan_id"] for t in unpulled)
    fp_file = os.path.join(LOG_DIR, "main_pull_unpulled_last.json")
    last_fp = ""
    try:
        last_fp = (json.load(open(fp_file, encoding="utf-8")) or {}).get("fp", "")
    except Exception:
        pass
    unpulled_changed = unpulled_fp != last_fp

    if unpulled and (content or unpulled_changed):
        lines = [content] if content else [f"**【主号拉取达人日报】{snap_id}**"]
        lines.append(f"**填表未被拉取 {len(unpulled)}**（在队票，主号未在投）：")
        for t in unpulled[:40]:
            who = f"{t['dept']}/{t['seller']}" if t["seller"] else t["dept"]
            lines.append(f"- {t['cid']}·{t['product']}·{who}·{t['progress']}")
        if len(unpulled) > 40:
            lines.append(f"- …等共 {len(unpulled)} 个")
        content = "\n".join(lines)

    if not content:
        log.info("无变化，不播报")
        return 0
    print(content)
    if args.dry_run:
        log.info("dry-run，不推送")
        return 0
    ok = push_summary_group(content)
    if ok:
        try:
            json.dump({"fp": unpulled_fp, "ts": snap_id},
                      open(fp_file, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
