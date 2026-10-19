#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whitelist_sync.py —— 企微表格「人工白名单」页 → 本地缓存同步器（2026-09-07）。

主管在企微智能表格新页维护白名单（计划ID+计划名，状态列 启用/停用），
poll 每轮（~15min）调 sync_whitelist_from_sheet() 把表格读下来合并写本地缓存
manual_creator_whitelist_sheet.json。manual_whitelist.py 判定核心读
[底账 JSON + 本缓存] 合并生效，全部调用点零改动。

删除语义：状态=停用 → 墓碑（有计划ID→deny_plan_ids 计划级删除；有达人名→
kill_names 达人级删除）。物理删行只对表格原生行有效（读不到即失效），删不掉
底账 JSON 条目——主管守则：要移出白名单，把状态改成停用。

容错：任何网络/API/解析异常一律沿用上次缓存，绝不清空、绝不抛给 poll 主流程；
未配置 whitelist_sheet_id 静默跳过（可先发码后建表）；读成功但 0 行是合法结果。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "投流"))

from wecom_client import WeComClient, simplify_record  # noqa: E402

log = logging.getLogger("whitelist_sync")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

CACHE_FILE = os.path.join(HERE, "manual_creator_whitelist_sheet.json")
# 停用墓碑收养文件：sync 发现「新增停用」的计划写这里，plan_power 恢复槽消费后
# 挂恢复票——白名单停用不再等「当晚23:00打标+次日12:00补票」才回工作流，
# 也兜住总览扫描看不到的超老暂停计划（2026-09-29 用户反馈：停用后没有恢复）。
ADOPTION_FILE = os.path.join(HERE, "whitelist_adoptions.json")

# 状态列里视为「停用」的关键词（含「停」即停用，兼容 停用/停/disabled/off/0）
_DISABLED_RE = re.compile(r"停|disabled|off|^0$", re.IGNORECASE)
# 计划ID 分隔符：逗号/中文逗号/顿号/空格/换行（rddyio 脏数据教训：尾随逗号、多 ID 挤一格）
_ID_SPLIT_RE = re.compile(r"[,，、\s]+")

_KNOWN_DEPARTMENTS = {"业务1部", "业务2部", "业务3部", "业务1部-1"}


def _clean(value) -> str:
    return "".join(str(value or "").strip().lower().split())


def _split_plan_ids(raw: str) -> list[str]:
    """剥离脏数据并拆分多 ID。'187xxx, ' / 'id1，id2' → ['187xxx'] / ['id1','id2']。"""
    out = []
    for tok in _ID_SPLIT_RE.split(str(raw or "")):
        tok = tok.strip()
        if tok:
            out.append(tok)
    return out


def _rows_to_cache(rows: list[dict], sheet_id: str) -> dict:
    """纯函数（可单测）：企微 records → 缓存 dict。

    rows 是 get_all_records() 的原始返回（含 values 嵌套）。逐行 simplify_record →
    剥 计划ID 脏数据 → 空行（计划ID/计划名/达人名 全空）跳过记 dropped →
    状态含「停」等 → 停用分支否则启用 → 冲突检测（同 pid/达人名双态，停用优先）。
    """
    enable_entries: list[dict] = []
    deny_plan_ids: list[str] = []
    deny_entries: list[dict] = []
    kill_names: list[str] = []
    conflicts: list[str] = []
    dropped = 0
    # 冲突检测：收集各标识出现的 状态 集合
    pid_states: dict[str, set] = {}
    name_states: dict[str, set] = {}

    for rec in rows:
        simple = simplify_record(rec)
        plan_ids = _split_plan_ids(simple.get("计划ID", ""))
        plan_name = (simple.get("计划名") or "").strip()
        creator = (simple.get("达人名") or "").strip()
        department = (simple.get("部门") or "").strip()
        reason = (simple.get("备注") or "").strip() or "表格白名单"
        status = (simple.get("状态") or "").strip()
        record_id = str(rec.get("record_id") or "")

        if not plan_ids and not plan_name and not creator:
            dropped += 1
            continue
        if department and department not in _KNOWN_DEPARTMENTS:
            log.warning("白名单行部门值未知（不丢弃，按不限部门）: %s | %s",
                        department, creator or plan_name or plan_ids)

        disabled = bool(_DISABLED_RE.search(status))
        state_key = "停用" if disabled else "启用"
        for pid in plan_ids:
            pid_states.setdefault(pid, set()).add(state_key)
        if creator:
            name_states.setdefault(creator, set()).add(state_key)

        if disabled:
            # 停用行 → 墓碑：计划ID 计划级删除；达人名 达人级删除（各自独立生效）
            for pid in plan_ids:
                if pid not in deny_plan_ids:
                    deny_plan_ids.append(pid)
                    deny_entries.append({"plan_id": pid, "plan_name": plan_name,
                                         "department": department})
            if creator and creator not in kill_names:
                kill_names.append(creator)
        else:
            # 启用行 → enable entry（与底账 JSON 同构）
            name = creator or plan_name or (plan_ids[0] if plan_ids else "")
            aliases = {a for a in (creator, plan_name) if a and a != name}
            enable_entries.append({
                "name": name,
                "aliases": sorted(aliases),
                "department": department,
                "plan_ids": plan_ids,
                "reason": reason,
                "record_id": record_id,
            })

    # 冲突：同标识同时有启用/停用 → 停用优先 + 记冲突让主管清理
    for pid, states in pid_states.items():
        if len(states) > 1:
            conflicts.append(f"计划ID {pid} 同时存在启用/停用行，按停用生效")
            if pid not in deny_plan_ids:
                deny_plan_ids.append(pid)
    for cname, states in name_states.items():
        if len(states) > 1:
            conflicts.append(f"达人 {cname} 同时存在启用/停用行，按停用生效")
            if cname not in kill_names:
                kill_names.append(cname)
    # 冲突的计划/达人：从 enable_entries 里剔除对应能力（停用优先）
    if conflicts:
        deny_set = {_clean(v) for v in deny_plan_ids}
        kill_set = {_clean(v) for v in kill_names}
        pruned = []
        for e in enable_entries:
            enames = {_clean(e.get("name"))} | {_clean(a) for a in e.get("aliases") or []}
            enames.discard("")
            if enames & kill_set:
                continue
            e["plan_ids"] = [p for p in e.get("plan_ids") or [] if _clean(p) not in deny_set]
            if not enames and not e["plan_ids"]:
                continue
            pruned.append(e)
        enable_entries = pruned

    return {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sheet_id": sheet_id,
        "row_count": len(rows),
        "dropped": dropped,
        "enable_entries": enable_entries,
        "deny_plan_ids": deny_plan_ids,
        "deny_entries": deny_entries,
        "kill_names": kill_names,
        "conflicts": conflicts,
    }


def _record_new_adoptions(cache: dict) -> None:
    """对比上次缓存，把「新增停用」的计划追加到收养文件（按 plan_id 去重）。

    plan_power 的恢复槽消费该文件挂恢复票。只认计划级墓碑（deny_entries）；
    达人级 kill_names 没有确定计划，不收养。任何异常静默——收养是增强，
    不能影响同步主流程。
    """
    try:
        prev_ids = set()
        if os.path.exists(CACHE_FILE):
            prev = json.load(open(CACHE_FILE, encoding="utf-8"))
            prev_ids = {str(p) for p in (prev.get("deny_plan_ids") or [])}
        new_entries = [e for e in (cache.get("deny_entries") or [])
                       if e.get("plan_id") and str(e["plan_id"]) not in prev_ids]
        if not new_entries:
            return
        pending = []
        if os.path.exists(ADOPTION_FILE):
            try:
                pending = json.load(open(ADOPTION_FILE, encoding="utf-8"))
                if not isinstance(pending, list):
                    pending = []
            except (OSError, json.JSONDecodeError):
                pending = []
        known = {str(e.get("plan_id")) for e in pending}
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        added = 0
        for e in new_entries:
            if str(e["plan_id"]) in known:
                continue
            pending.append({**e, "disabled_at": now})
            known.add(str(e["plan_id"]))
            added += 1
        if added:
            _atomic_write(ADOPTION_FILE, pending)
            log.info("白名单新增停用 %d 条，已写入收养文件待恢复槽挂票: %s",
                     added, [e["plan_id"] for e in new_entries][:5])
    except Exception as exc:  # noqa: BLE001
        log.warning("收养文件写入失败（不影响同步）: %s", str(exc)[:150])


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sync_whitelist_from_sheet(client: WeComClient | None = None) -> dict:
    """读表格 → 合并 → 原子写缓存。返回 {"ok": bool, ...}，绝不抛异常。

    读失败（网络/API/解析）一律沿用上次缓存，返回 ok=False+error；旧缓存原样保留。
    """
    try:
        client = client or WeComClient()
        sheet_id = (client.cfg.get("whitelist_sheet_id") or "").strip()
        if not sheet_id:
            return {"ok": False, "skipped": "未配置 whitelist_sheet_id"}
        # 切到白名单页读，用完恢复（不污染任务表调用）
        orig_sheet = client.cfg.get("sheet_id")
        try:
            client.cfg["sheet_id"] = sheet_id
            rows = client.get_all_records()
        finally:
            client.cfg["sheet_id"] = orig_sheet
        cache = _rows_to_cache(rows, sheet_id)
        _record_new_adoptions(cache)
        _atomic_write(CACHE_FILE, cache)
        return {"ok": True, "enable": len(cache["enable_entries"]),
                "deny": len(cache["deny_plan_ids"]), "kill": len(cache["kill_names"]),
                "conflicts": len(cache["conflicts"]), "rows": cache["row_count"],
                "dropped": cache["dropped"]}
    except Exception as exc:  # noqa: BLE001 —— 容错兜底，绝不清空缓存
        log.warning("白名单同步失败（沿用上次缓存）: %s", str(exc)[:150])
        return {"ok": False, "error": str(exc)[:200]}


if __name__ == "__main__":
    r = sync_whitelist_from_sheet()
    print("同步结果:", json.dumps(r, ensure_ascii=False))
    if r.get("ok"):
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))
        print(f"启用 {len(cache['enable_entries'])} 条 / 停用墓碑 计划{len(cache['deny_plan_ids'])} 达人{len(cache['kill_names'])} / 冲突 {len(cache['conflicts'])}")
        for e in cache["enable_entries"]:
            print("  [启用]", e["name"], e.get("department"), e.get("plan_ids"))
        for c in cache["conflicts"]:
            print("  [冲突]", c)
