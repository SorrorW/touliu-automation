"""Local manual-operation whitelist for creators and their plans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


WHITELIST_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "manual_creator_whitelist.json"
# 2026-09-07：企微表格新页「人工白名单」由 poll 每轮同步到该缓存（主管自助维护）。
# 底账 WHITELIST_FILE 保留不动（webui 保存接口会整体重写 creators，policy 须保留），
# 缓存是独立运行时产物，合并时叠加删除语义（状态=停用 墓碑）。
SHEET_CACHE_FILE = Path(__file__).resolve().parents[2] / "wecom-agent" / "manual_creator_whitelist_sheet.json"


def _clean(value: object) -> str:
    return "".join(str(value or "").strip().lower().split())


def _clean_plan_name(value: object) -> str:
    """计划名比较忽略人工表常见的日期分隔符差异（0815_ / 0815-）。"""
    return _clean(value).replace("_", "").replace("-", "").replace("—", "")


def _load_json_entries() -> list[dict[str, Any]]:
    """底账 JSON 的 creators 列表（原 _load_entries 行为）。"""
    try:
        raw = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("creators", []) if isinstance(raw, dict) else []
    return [item for item in entries if isinstance(item, dict)]


def _load_sheet_cache() -> dict[str, Any]:
    """读表格同步缓存；缺文件/坏 JSON → {}（行为退化为纯底账，与今日一致）。"""
    try:
        raw = json.loads(SHEET_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_entries() -> list[dict[str, Any]]:
    """合并底账 JSON 与表格缓存，应用删除语义（状态=停用 → 墓碑）。

    - 停用行有达人名 → kill_names：达人级删除，整条 entry 移出（JSON/表格一视同仁）；
    - 停用行有计划ID → deny_plan_ids：计划级删除，从各 entry 的 plan_ids 剥离
      （绝对否决另在 match_manual_whitelist 开头前置拦截，防别名子串绕过）。
    冲突（同目标双态）由同步器按「停用优先」处理，这里只需应用墓碑。
    """
    cache = _load_sheet_cache()
    kill = {_clean(v) for v in (cache.get("kill_names") or []) if _clean(v)}
    deny = {_clean(v) for v in (cache.get("deny_plan_ids") or []) if _clean(v)}
    merged: list[dict[str, Any]] = []
    for e in _load_json_entries() + list(cache.get("enable_entries") or []):
        if not isinstance(e, dict):
            continue
        names = {_clean(e.get("name"))}
        names.update(_clean(a) for a in (e.get("aliases") or []))
        names.discard("")
        if names & kill:
            continue  # 达人级墓碑：整条移出
        ids = [p for p in (e.get("plan_ids") or []) if _clean(p) not in deny]
        if not names and not ids:
            continue  # 被剥空的纯计划行丢弃
        merged.append({**e, "plan_ids": ids})
    return merged


def match_manual_whitelist(
    *, creator: str = "", department: str = "", plan_id: str = "", plan_name: str = ""
) -> dict[str, Any] | None:
    """Return the matching whitelist entry, otherwise ``None``.

    A configured department must match when it is provided by the caller. Plan
    IDs are exact matches; aliases only match an exact creator or plan name.
    """
    creator_key = _clean(creator)
    department_key = _clean(department)
    plan_id_key = _clean(plan_id)
    plan_name_key = _clean_plan_name(plan_name)
    # 计划级删除是绝对否决：即使计划名能被某条目的别名子串命中，也先压掉。
    # （否则「主管停用了这条计划」会被同达人其它条目的名字匹配重新救回。）
    if plan_id_key:
        deny = {_clean(v) for v in (_load_sheet_cache().get("deny_plan_ids") or []) if _clean(v)}
        if plan_id_key in deny:
            return None
    for entry in _load_entries():
        configured_department = _clean(entry.get("department"))
        if configured_department and department_key and configured_department != department_key:
            continue
        if plan_id_key and plan_id_key in {_clean(v) for v in entry.get("plan_ids", [])}:
            return entry
        aliases = {_clean_plan_name(entry.get("name"))}
        aliases.update(_clean_plan_name(v) for v in entry.get("aliases", []))
        aliases.discard("")
        if creator_key and creator_key in aliases:
            return entry
        if plan_name_key and any(alias in plan_name_key for alias in aliases):
            return entry
    return None


def is_manual_whitelisted(**kwargs: str) -> bool:
    return match_manual_whitelist(**kwargs) is not None


def manual_label(**kwargs: str) -> str:
    entry = match_manual_whitelist(**kwargs)
    return str(entry.get("name") or "人工白名单") if entry else ""
