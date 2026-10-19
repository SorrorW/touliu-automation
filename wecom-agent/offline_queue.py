#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微不可用时的本地投流队列。

只从 local_pending_tasks.json 读取任务并执行千川前置流程，执行结果写回本地。
不调用企微 API；网络恢复后显式运行 --sync-wecom 才会回填进度。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "投流")))

import local_tools

HERE = Path(__file__).resolve().parent
QUEUE_FILE = HERE / "local_pending_tasks.json"
LOG_FILE = HERE / "logs" / f"offline_queue_{dt.date.today():%Y%m%d}.log"
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("offline_queue")


def load_queue() -> dict:
    data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    data.setdefault("tasks", [])
    return data


def save_queue(data: dict) -> None:
    data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def completed_steps(task: dict) -> list[str]:
    return [name for name, value in (task.get("steps") or {}).items() if value == "ok"]


def deferred_progress(status: str, detail: str) -> str:
    import re
    main=re.search(r"\[主号进度=([^\]]+)\]",detail or "")
    if main:
        return main.group(1)
    if status == "ok":
        return "已投放"
    if status == "fatal":
        return "失败"
    if status == "wait":
        if "待达人确认" in detail or "待确认" in detail:
            return "等待达人接受"
        return "可投放"
    return "待处理"


def run_task(task: dict) -> None:
    task["local_state"] = "running"
    task["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
    steps = task.setdefault("steps", {})
    notes = task.setdefault("step_notes", {})

    def step_cb(name: str, status: str, detail: str = "") -> None:
        if status == "成功":
            steps[name] = "ok"
        elif status == "等待":
            steps[name] = "wait"
        elif status == "失败":
            steps[name] = "fail"
        notes[name] = detail[:500]
        task["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")

    result, detail = local_tools.process_douyin_task(
        creator=task["creator"],
        code=task.get("code", ""),
        product=task.get("product_id", ""),
        seller=task.get("seller", ""),
        department=task.get("department", ""),
        remark=task.get("remark", ""),
        scheduled_start=task.get("scheduled_start", ""),
        skip_steps=completed_steps(task),
        step_cb=step_cb,
        step_notes=notes,
    )
    task["result"] = result
    task["detail"] = detail[:1000]
    task["deferred_progress"] = deferred_progress(result, detail)
    task["local_state"] = {
        "ok": "completed",
        "wait": "waiting",
        "fatal": "failed",
        "fail": "retry",
    }.get(result, "retry")
    task["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")


def sync_wecom(data: dict) -> None:
    """按抖音号、合作码、商品、部门精确匹配后回填，歧义记录绝不写。"""
    import wecom_client

    client = wecom_client.WeComClient()
    records = client.get_all_records()
    index: dict[tuple[str, str, str, str], list[str]] = {}
    for record in records:
        simple = wecom_client.simplify_record(record)
        key = (
            local_tools._clean_token(simple.get("抖音号", "")),
            local_tools._clean_token(simple.get("合作码", "")),
            local_tools._clean_token(simple.get("商品ID", "")),
            simple.get("业务部", "").strip(),
        )
        index.setdefault(key, []).append(record["record_id"])

    updates = []
    for task in data["tasks"]:
        progress = task.get("deferred_progress")
        if not progress or task.get("synced_at"):
            continue
        key = (task["creator"], task.get("code", ""), task.get("product_id", ""), task["department"])
        matches = index.get(key, [])
        if len(matches) != 1:
            task["sync_note"] = f"匹配到 {len(matches)} 条企微记录，未回填"
            continue
        updates.append({"record_id": matches[0], "values": {"进度": wecom_client.text_cell(progress)}})
        task["wecom_record_id"] = matches[0]

    if updates:
        client.update_records(updates)
        now = dt.datetime.now().isoformat(timespec="seconds")
        for task in data["tasks"]:
            if task.get("wecom_record_id"):
                task["synced_at"] = now
        log.info("已回填 %d 条企微记录", len(updates))
    else:
        log.info("没有可安全回填的记录")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1, help="本次最多执行几条，默认 1")
    parser.add_argument("--sync-wecom", action="store_true", help="企业微信恢复后才执行回填")
    args = parser.parse_args()
    data = load_queue()
    if args.sync_wecom:
        sync_wecom(data)
        save_queue(data)
        return

    if args.limit < 1:
        log.info("仅校验队列，未执行任务")
        return

    candidates = [
        task for task in data["tasks"]
        if task.get("local_state", "pending") in {"pending", "retry", "waiting"}
    ]
    candidates.sort(key=lambda task: (task.get("local_state") == "waiting", task["created_at"]))
    for task in candidates[:args.limit]:
        log.info("处理 %s / %s", task["creator"], task["product_id"])
        run_task(task)
        save_queue(data)
        log.info("结果 %s: %s", task["creator"], task["local_state"])


if __name__ == "__main__":
    main()
