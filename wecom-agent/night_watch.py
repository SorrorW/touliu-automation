# -*- coding: utf-8 -*-
"""夜间守望（2026-09-02 用户：「还有这么多计划，帮我盯着，别漏暂停了、误启动」）。

00:00-07:50 每轮扫描三部门：除重点计划外，凡「投放中」一律暂停并挂恢复票——
新计划/潜力 → 今天 08:00，日常 → 今天 12:00（_resume_time_for 自动分流）。
夜间合法在投 = 昨日 23:00 终分类的重点计划（收口只恢复重点）。
08:00 起新计划/潜力由 wecom_morning_resume 正常拉起，本守望退出窗口。

cron 触发：runtime python -X utf8 night_watch.py
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "投流")))

import budget_watch  # noqa: E402
from 巨量千川 import plan_power as pp  # noqa: E402
from 巨量千川.manual_whitelist import is_manual_whitelisted  # noqa: E402

DEPS = ("业务1部", "业务2部", "业务3部")
WINDOW_END = (7, 50)   # 07:50 后不再暂停（08:00 恢复任务接管）
# 恩豆妈咪：2026-09-02 用户口径「恩豆是重点」。她昨天白天一直挂着白名单标签，
# 看板没有 key_plan 记录，这里显式放行；明晚 23:00 终分类按数据自然归标签。
KEY_OVERRIDE_PIDS = {"1870108771652617"}
LOG = logging.getLogger("night_watch")
if not LOG.handlers:
    LOG.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_h)
    try:
        _fh = logging.FileHandler(
            os.path.join(HERE, "logs", f"night_watch_{datetime.date.today():%Y%m%d}.log"),
            encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        LOG.addHandler(_fh)
    except OSError:
        pass


def _in_window(now: datetime.datetime) -> bool:
    return (now.hour, now.minute) < WINDOW_END


def _key_plan_ids() -> set[str]:
    """昨日终分类的重点计划 pid = 夜间合法在投。"""
    from webui import db
    day = datetime.date.today()
    if datetime.datetime.now().hour < 8:
        day -= datetime.timedelta(days=1)
    ids: set[str] = set()
    try:
        for row in db.list_plan_daily_board(day.isoformat()):
            labels = [row.get(f"label_{s}") for s in ("08", "12", "17", "23")]
            labels = [x for x in labels if x]
            if labels and labels[-1] == "key_plan":
                pid = str(row.get("plan_id") or "").strip()
                if pid:
                    ids.add(pid)
    except Exception as exc:
        LOG.error("读昨日重点标签失败（本轮保守不暂停）: %s", str(exc)[:150])
        raise
    ids |= KEY_OVERRIDE_PIDS
    return ids


async def _run() -> dict:
    from playwright.async_api import async_playwright
    from 巨量千川.plan_metrics import list_all_plans

    key_ids = _key_plan_ids()
    result = {"key_ids": sorted(key_ids), "paused": [], "already": [],
              "failed": [], "running_ok": []}
    fd, held = pp._acquire_action_lock(max_wait_s=300)
    budget_watch._busy_mark("night-watch")
    try:
        pp._ensure_cdp()
        async with async_playwright() as pw:
            browser = await pp.connect_browser(pw, timeout=8000)
            ctx = browser.contexts[0]
            for dep in DEPS:
                plans = await list_all_plans(dep, time_range="last_7_days")
                running = [
                    {"plan_id": str(getattr(p, "plan_id", "") or ""),
                     "plan_name": getattr(p, "plan_name", "") or "",
                     "department": dep}
                    for p in plans if "投放中" in str(getattr(p, "status", "") or "")
                ]
                ok_runners = [r for r in running if r["plan_id"] in key_ids]
                # 2026-09-07：人工白名单计划（味蕾食光等）用户要求保持在投，
                # 不算异常、不补停——pause_plan 已拦白名单，这里前置排除避免整夜误报。
                anomalies = [
                    r for r in running
                    if r["plan_id"] not in key_ids
                    and not is_manual_whitelisted(
                        department=dep, plan_id=r["plan_id"], plan_name=r["plan_name"])
                ]
                result["running_ok"].extend(ok_runners)
                if not anomalies:
                    continue
                LOG.info("[%s] 夜间异常在投 %d 条: %s", dep, len(anomalies),
                         [a["plan_name"] for a in anomalies])
                page = await pp._department_page(ctx, dep)
                for t in anomalies:
                    try:
                        ok, detail = await pp.pause_plan(page, t["plan_name"], t["plan_id"], dep)
                    except Exception as exc:
                        LOG.warning("[%s] 暂停异常，刷新重试: %s (%s)", dep,
                                    t["plan_name"], str(exc)[:120])
                        try:
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(5000)
                            ok, detail = await pp.pause_plan(page, t["plan_name"], t["plan_id"], dep)
                        except Exception as exc2:
                            ok, detail = False, f"error:{str(exc2)[:150]}"
                    rec = {**t, "detail": detail}
                    if ok and detail == "ok":
                        result["paused"].append(rec)
                        LOG.info("[%s] 夜间守望补停: %s", dep, t["plan_name"])
                    elif ok:
                        result["already"].append(rec)
                    else:
                        result["failed"].append(rec)
                        LOG.error("[%s] 夜间守望暂停失败: %s (%s)", dep, t["plan_name"], detail)
    finally:
        budget_watch._busy_clear()
        pp._release_action_lock(fd, held)

    stopped = result["paused"] + result["already"]
    if stopped:
        from 巨量千川.plan_name import _DATE_PREFIX_RE
        for t in stopped:
            is_new = bool(_DATE_PREFIX_RE.match(t["plan_name"] or ""))
            t["category"] = "new_plan" if is_new else "old_pool"
        pp._queue_old_pool_resume(
            [{"plan_name": t["plan_name"], "plan_id": t["plan_id"],
              "department": t["department"], "category": t["category"],
              "status": "已暂停"} for t in stopped])
        LOG.info("已挂恢复票 %d 条（新计划 08:00 / 日常 12:00 自动分流）", len(stopped))
    return result


def maybe_run() -> str:
    """poll 轮末钩子：窗口/播报守卫都在 main 逻辑里，窗外空转零开销。"""
    now = datetime.datetime.now()
    if not _in_window(now):
        return "outside-window"
    try:
        result = asyncio.run(_run())
        acted = len(result["paused"]) + len(result["failed"])
        return f"acted:{acted}" if acted else "quiet"
    except Exception:
        LOG.exception("夜间守望本轮失败")
        return "error"


def main() -> int:
    now = datetime.datetime.now()
    if not _in_window(now):
        print(f"{now:%H:%M} 不在守望窗口（00:00-07:50），退出")
        return 0
    if budget_watch.in_report_window(now) or budget_watch.report_busy():
        print("播报窗口/播报扫描中，本轮退出")
        return 0
    result = asyncio.run(_run())
    print(json.dumps({k: v for k, v in result.items() if k != "running_ok"},
                     ensure_ascii=False))
    print("合法在投:", [r["plan_name"] for r in result["running_ok"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
