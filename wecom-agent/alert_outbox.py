#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警发件箱：企微推送失败不落丢，恢复后自动补发（2026-09-14，排期 W0「企微
WinError 10013 + 告警一致性」）。

背景：日报推送有「3 轮 × 10 分钟 + 跨时段 pending」重试，而监控播报/预算监控
等即时告警只推一次——WinError 10013（防火墙拦 qyapi.weixin.qq.com:443 出站，
进程级全灭）或企微 IP 白名单断线期间，这些告警只在日志里留一行就永远消失。

统一语义（所有告警路径一致）：
- 推失败 → 入箱（同内容 2h 内去重，最多 50 条，超限丢最旧并记 ERROR）；
- 任何路径推成功 → 顺手 flush() 补发积压（推成功=企微通道已恢复的证据）；
- 补发内容首行加「⏰ 补发」前缀标注原告警时间；
- 传输错误（10013/超时等）= 通道仍断，停止本轮 flush 保留下次；
- 平台拒收（errcode≠0，多为内容问题）= 单条重试 5 次后丢弃并记 ERROR。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import urllib.request
from pathlib import Path

log = logging.getLogger("alert_outbox")

_STATE_FILE = Path(__file__).resolve().parent / "logs" / "alert_outbox.json"

MAX_ENTRIES = 50
MAX_ATTEMPTS = 5          # 平台拒收（errcode≠0）的重试上限，到顶丢弃
DEDUPE_SECONDS = 7200     # 同内容 2h 内不重复入箱
RESEND_PREFIX_AFTER = 600  # 首次入箱超过 10 分钟，补发时加前缀

TRANSPORT_MARKERS = ("10013", "Permission denied", "timed out", "11001", "10060")


def _load(state_file: Path | None = None) -> dict:
    path = state_file or _STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"entries": []}


def _save(state: dict, state_file: Path | None = None) -> None:
    path = state_file or _STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _entry_id(kind: str, content: str) -> str:
    return hashlib.sha1(f"{kind}\n{content}".encode("utf-8")).hexdigest()[:12]


def _post(url: str, content: str) -> tuple[bool, str]:
    """raw webhook POST。返回 (成功与否, 错误描述)——errcode≠0 也算失败并带 errcode。"""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(
                {"msgtype": "markdown", "markdown": {"content": content}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # 传输层失败（含 WinError 10013）
        return False, str(exc)
    if body.get("errcode", 0) == 0:
        return True, ""
    return False, f"errcode={body.get('errcode')}: {body.get('errmsg')}"


def _is_transport_error(err: str) -> bool:
    return not err.startswith("errcode=") or any(m in err for m in TRANSPORT_MARKERS)


def enqueue(kind: str, content: str, webhook: str,
            now: datetime.datetime | None = None,
            state_file: Path | None = None) -> bool:
    """推送失败的告警入箱。返回 True=新入箱；False=2h 内同内容已存在（去重）。"""
    if not content or not webhook:
        return False
    now = now or datetime.datetime.now()
    state = _load(state_file)
    entries = state["entries"]
    eid = _entry_id(kind, content)
    for item in entries:
        if item.get("id") == eid:
            first = datetime.datetime.fromisoformat(item["first_ts"])
            if (now - first).total_seconds() < DEDUPE_SECONDS:
                item["last_ts"] = now.isoformat(timespec="seconds")
                _save(state, state_file)
                return False
    if len(entries) >= MAX_ENTRIES:
        dropped = entries.pop(0)
        log.error("告警发件箱超 %d 条上限，丢最旧一条: %s %s",
                  MAX_ENTRIES, dropped.get("first_ts"), dropped.get("kind"))
    entries.append({
        "id": eid,
        "kind": kind,
        "content": content,
        "webhook": webhook,
        "first_ts": now.isoformat(timespec="seconds"),
        "last_ts": now.isoformat(timespec="seconds"),
        "attempts": 0,
        "last_error": "",
    })
    _save(state, state_file)
    log.info("告警入发件箱（%s），待通道恢复补发；箱内 %d 条", kind, len(entries))
    return True


def flush(now: datetime.datetime | None = None,
          state_file: Path | None = None) -> tuple[int, int]:
    """补发箱内积压（最旧先发）。返回 (补发成功数, 箱内剩余数)。

    传输错误 → 通道仍断，立即停止本轮（保留剩余下次再试）；
    平台拒收 → 该条 attempts++，到 MAX_ATTEMPTS 丢弃（内容问题重试无用）。
    """
    now = now or datetime.datetime.now()
    state = _load(state_file)
    entries = state["entries"]
    if not entries:
        return 0, 0
    sent = 0
    remaining: list[dict] = []
    for idx, item in enumerate(entries):
        content = item["content"]
        try:
            first = datetime.datetime.fromisoformat(item["first_ts"])
            age = (now - first).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age > RESEND_PREFIX_AFTER:
            content = (f"> ⏰ 补发 {first:%m-%d %H:%M} 的{item.get('kind') or '告警'}"
                       f"（当时推送失败）\n\n" + content)
        ok, err = _post(item["webhook"], content)
        if ok:
            sent += 1
            log.info("补发成功（%s，首次入箱 %s）", item.get("kind"), item.get("first_ts"))
            continue
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_error"] = err[:200]
        if _is_transport_error(err):
            # 通道仍断：本条留下，后续条目本轮不再尝试
            remaining.append(item)
            remaining.extend(entries[idx + 1:])
            log.warning("补发仍被通道拒绝（%s），箱内 %d 条留待下轮: %s",
                        item.get("kind"), len(remaining), err[:120])
            break
        if item["attempts"] >= MAX_ATTEMPTS:
            log.error("补发连续被平台拒收 %d 次，丢弃（%s）: %s",
                      item["attempts"], item.get("kind"), err[:120])
            continue
        remaining.append(item)
    state["entries"] = remaining
    _save(state, state_file)
    return sent, len(remaining)


def pending_count(state_file: Path | None = None) -> int:
    return len(_load(state_file)["entries"])
