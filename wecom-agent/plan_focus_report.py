# -*- coding: utf-8 -*-
"""潜力/重点计划独立监控播报（2026-09-01 用户要求）。

每 30 分钟把潜力计划 + 重点计划（外加白名单 budget_watch 计划，如恩豆妈咪）
的消耗/单量/ROI/预算水位单独推到 `config.json` 的 `monitor_webhook` 群。
与大节点日报的关系：
- 播报档 ±12 分钟内自动跳过（budget_watch.in_report_window，2026-09-01 收窄
  放行 :15/:45 监控），物理上不撞档；扫描超时拖档由 report_busy 忙标再兜底；
- 夜间 23:00-08:00 不刷常态播报，只做白名单断供检查（budget_watch.check_live）；
- 扫描共用部门总览页、时间范围固定「今天」（_set_time_range 带读回校验）。

运行通道（状态文件共享，双通道不会重复推）：
- poll_and_run 每轮末尾调 maybe_run()（轮询活着就有）；
- 或用户自行注册 Windows 定时任务（_create_focus_report_task.ps1），
  任务每 30 分钟触发本脚本，脚本内部再做窗口/夜间/频率守卫。

破线/预算标记直接进播报行（⚠️）；断供级事件额外即时单推（复用 budget_watch
的每天每级别一次去重）。手动：python plan_focus_report.py --force --dry-run

盘中预算安全闸（2026-09-15 费用安全件）：每轮扫描后对全量计划跑
plan_power.budget_safety_clamp_targets——99999 不限额一律按档位写回、
超档位预算只下压不上抬（升档补足的新计划豁免）——检出即改并随播报通告。
夜间（23:00-08:00）不跑，由 23:00 收口归位兜底。"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import msvcrt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "投流")))

import budget_watch  # noqa: E402
from webui import db as plan_db  # noqa: E402

LOG_DIR = os.path.join(HERE, "logs")
STATE_FILE = os.path.join(LOG_DIR, "plan_focus_report_state.json")
LOCK_FILE = os.path.join(HERE, ".plan_focus_report.lock")
# 2026-10-14 用户口径「一部-1不用再扫描了」：焦点播报不再扫矩阵号。
DEPARTMENTS = ("业务1部", "业务2部", "业务3部")
# 矩阵号（业务1部-1）播报行归入业务1部展示（2026-09-03 用户定「放到业务一部里」）；
# 扫描仍按真实账号（账号映射/页面路由依赖它），只改写展示部门名。
DISPLAY_DEPARTMENT = {"业务1部-1": "业务1部"}
INTERVAL_MIN = 30          # 常态播报最小间隔
NIGHT_START, NIGHT_END = 23, 8  # 23:00-08:00 不刷常态播报
MAX_BYTES = 3000           # 企微 markdown 上限留余量

log = logging.getLogger("plan_focus_report")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_h)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _fh = logging.FileHandler(
            os.path.join(LOG_DIR, f"plan_focus_{datetime.date.today():%Y%m%d}.log"),
            encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(_fh)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 守卫：窗口 / 夜间 / 频率 / 进程锁
# ---------------------------------------------------------------------------

def in_night_window(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    return now.hour >= NIGHT_START or now.hour < NIGHT_END


def _load_state() -> dict:
    try:
        st = json.loads(open(STATE_FILE, encoding="utf-8").read())
        if isinstance(st, dict):
            return st
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(st: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)


def _due(st: dict, now: datetime.datetime | None = None,
         interval_min: int = INTERVAL_MIN) -> bool:
    last = float(st.get("last_push_ts") or 0)
    return (time.time() - last) >= interval_min * 60


def _acquire_lock():
    fd = open(LOCK_FILE, "a+b")
    try:
        fd.seek(0, 2)
        if fd.tell() == 0:
            fd.write(b"0")
            fd.flush()
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        fd.close()
        return None


def _release_lock(fd) -> None:
    try:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fd.close()


# ---------------------------------------------------------------------------
# 数据组装（纯函数，便于单测）
# ---------------------------------------------------------------------------

def latest_board_labels(label_date: str) -> dict[str, str]:
    """今日看板：pid → 最新档位标签（label_08/12/17/23 取最后一个非空）。"""
    board: dict[str, str] = {}
    for row in plan_db.list_plan_daily_board(label_date):
        pid = str(row.get("plan_id") or "").strip()
        if not pid:
            continue
        labels = [row.get(f"label_{s}") for s in ("08", "12", "17", "23")]
        labels = [x for x in labels if x]
        if labels:
            board[pid] = labels[-1]
    return board


def build_rows(plans_by_dep: dict[str, list], board: dict[str, str],
               watched: dict[str, dict], minutes: int) -> list[dict]:
    """筛选潜力/重点/白名单计划并计算标记。plans_by_dep 的值是 PlanMetrics 列表。"""
    rows: list[dict] = []
    for dep, plans in plans_by_dep.items():
        for p in plans:
            pid = str(getattr(p, "plan_id", None) or "")
            if not pid:
                continue
            cat = board.get(pid, "")
            is_watch = pid in watched
            if cat not in budget_watch.CATEGORY_RULES and not is_watch:
                continue
            status = str(getattr(p, "status", "") or "")
            spent = float(getattr(p, "spent", 0) or 0)
            # 已暂停且今日零消耗的计划不进播报（就是没在跑，无监控价值）
            if "投放中" not in status and spent <= 0 and not is_watch:
                continue
            tag = "白名单" if is_watch else budget_watch.CATEGORY_RULES[cat]["label"]
            orders = getattr(p, "orders", None)
            roi = getattr(p, "roi", None)
            budget = getattr(p, "budget", None)
            budget_level = (
                None if budget_watch.is_alert_suppressed(pid)
                else budget_watch.evaluate(spent, budget, status)
            )
            roi_level = (
                budget_watch.evaluate_roi(cat, roi, orders, spent, minutes)
                if cat in budget_watch.CATEGORY_RULES else None
            )
            rows.append({
                "department": DISPLAY_DEPARTMENT.get(dep, dep), "plan_id": pid,
                "plan_name": getattr(p, "plan_name", "") or pid,
                "category": cat, "tag": tag, "status": status,
                "spent": spent, "orders": orders, "roi": roi, "budget": budget,
                "budget_level": budget_level, "roi_level": roi_level,
            })
    order = {"白名单": 0, "重点计划": 1, "潜力计划": 2}
    rows.sort(key=lambda r: (order.get(r["tag"], 9), -r["spent"]))
    return rows


def _flag_text(row: dict, minutes: int) -> str:
    flags = []
    if row["budget_level"] == "exhausted":
        flags.append("⚠️预算断供")
    elif row["budget_level"] == "warn":
        flags.append("⚠️预算告警")
    if row["roi_level"] == "roi_low":
        floor = budget_watch.roi_floor_for(row["category"], minutes)
        flags.append(f"⚠️ROI破线({floor:g})")
    elif row["roi_level"] == "no_orders":
        flags.append("⚠️高耗零单")
    return " ".join(flags)


def _safety_notice(clamp_result: dict) -> str:
    """预算安全闸结果 → 一行播报通告（无改动且无失败=空串不进播报）。"""
    changed = clamp_result.get("changed") or []
    failed = clamp_result.get("failed") or []
    if not changed and not failed:
        return ""
    parts = []
    intercepts = [c for c in changed if c.get("reason") == "unlimited_intercept"]
    over_caps = [c for c in changed if c.get("reason") != "unlimited_intercept"]
    if intercepts:
        parts.append("99999拦截 " + "、".join(
            f"{c['plan_name']}({c['current_budget']:g}→{c.get('after', c['label_budget']):g})"
            for c in intercepts[:5]))
    if over_caps:
        parts.append("漂移下压 " + "、".join(
            f"{c['plan_name']}({c['current_budget']:g}→{c.get('after', c['label_budget']):g})"
            for c in over_caps[:5]))
    line = "> 🛡️ 预算安全闸已动作：" + "；".join(parts)
    if failed:
        line += f"；失败 {len(failed)} 条（详情见日志）"
    return line


def build_markdown(rows: list[dict], now: datetime.datetime, minutes: int,
                   notices: list[str] | None = None) -> str:
    """常态播报文本。控制 3000 字节内，超长折叠尾部。"""
    lines = [f"**【潜力·重点监控】{now:%m月%d日 %H:%M}**"]
    # 预算安全闸等动作通告紧随标题（计入字节预算，2026-09-15 费用安全件）
    for notice in notices or []:
        lines.append(notice)
    if not rows:
        lines.append("> 当前无在投或有消耗的潜力/重点计划")
        return "\n".join(lines)
    body: list[str] = []
    for row in rows:
        budget = row["budget"]
        if budget and 0 < float(budget) < budget_watch.UNLIMITED:
            budget_text = f"预算 {row['spent'] / float(budget) * 100:.0f}%"
        else:
            budget_text = "预算不限"
        roi = row["roi"]
        roi_text = f"ROI {roi:g}" if isinstance(roi, (int, float)) else "ROI --"
        paused = "（已暂停）" if "投放中" not in row["status"] else ""
        flag = _flag_text(row, minutes)
        line = (
            f"> {row['tag']}｜{row['department']} {row['plan_name']}{paused}："
            f"消耗 {row['spent']:,.2f} 元/{int(row['orders'] or 0)} 单，"
            f"{roi_text}，{budget_text}"
        )
        if flag:
            line += f"  {flag}"
        body.append(line)
    total_spent = sum(r["spent"] for r in rows)
    total_orders = sum(int(r["orders"] or 0) for r in rows)
    active = sum(1 for r in rows if "投放中" in r["status"])
    tail = (f"**合计：{active}/{len(rows)} 条在投，消耗 {total_spent:,.2f} 元，"
            f"成交 {total_orders} 单**\n⚠️=ROI破线/预算告警；详情见整点日报")
    shown = 0
    for i, line in enumerate(body):
        candidate = "\n".join(lines + body[: i + 1] + [tail])
        if len(candidate.encode("utf-8")) > MAX_BYTES:
            rest = rows[i:]
            lines.append(
                f"> …其余 {len(rest)} 条：消耗 "
                f"{sum(r['spent'] for r in rest):,.2f} 元/"
                f"{sum(int(r['orders'] or 0) for r in rest)} 单")
            break
        shown += 1
    lines.extend(body[:shown])
    lines.append(tail)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 扫描与推送
# ---------------------------------------------------------------------------

async def _scan() -> tuple[list[dict], dict, dict]:
    from 巨量千川.plan_metrics import list_all_plans

    plans_by_dep: dict[str, list] = {}
    for dep in DEPARTMENTS:
        try:
            plans_by_dep[dep] = await list_all_plans(dep, time_range="今天")
        except Exception as exc:
            log.warning("[%s] 监控扫描失败: %s", dep, str(exc)[:200])
            plans_by_dep[dep] = []
    today = datetime.date.today().isoformat()
    board = latest_board_labels(today)
    watched = {e["plan_id"]: e for e in budget_watch.load_watch_entries()}
    now = datetime.datetime.now()
    return build_rows(plans_by_dep, board, watched, now.hour * 60 + now.minute), plans_by_dep, board


def _fire_immediate_alerts(rows: list[dict], *, push: bool) -> None:
    """断供/预算告警即时单推（复用 budget_watch 去重状态；ROI 破线只进播报行）。"""
    today = datetime.date.today().isoformat()
    st = budget_watch._load_state()
    for row in rows:
        level = row["budget_level"]
        if level not in ("warn", "exhausted"):
            continue
        pid = row["plan_id"]
        if budget_watch._already(st, pid, level, today):
            continue
        entry = {"name": row["plan_name"], "department": row["department"],
                 "plan_id": pid}
        md = budget_watch._alert_md(
            entry, spent=row["spent"], budget=float(row["budget"] or 0),
            status=row["status"], orders=row["orders"], roi=row["roi"],
            level=level, tag=row["tag"])
        log.info("监控播报即时告警 %s：%s（%s）", level, row["plan_name"], pid)
        if not push:
            continue  # dry-run：不推不记状态（否则真实告警会被吞掉）
        if budget_watch._push_alert(md):
            st = budget_watch._mark(st, pid, level, today)
            # 2026-09-03 用户定：监控只监控，预算类告警不再自动入加预算队列
    budget_watch._save_state(st)


def _push(md: str) -> bool:
    import alert_outbox
    import daily_report_push as push_mod
    url = push_mod.load_monitor_webhook()
    if not url:
        log.warning("config.json 未配置 monitor_webhook，监控播报无法推送")
        return False
    done, left = push_mod.push_missing({"汇总": url}, md, {})
    if left:
        # 推失败入发件箱：通道恢复后由任一成功推送顺手补发（告警一致性 2026-09-14）
        alert_outbox.enqueue("监控播报", md, url)
        return False
    alert_outbox.flush()
    return True


def run(*, push: bool = True, force: bool = False) -> str:
    """执行一轮。返回 skipped-night / skipped-window / skipped-interval /
    skipped-lock / skipped-busy / pushed / dry-run / failed。"""
    now = datetime.datetime.now()
    if in_night_window(now) and not force:
        # 夜间不刷常态播报；白名单断供照查（单条搜索，便宜）。
        # 2026-09-03 用户定：监控只监控，不再自动执行加预算队列。
        try:
            budget_watch.check_live(push=push)
        except Exception:
            log.exception("夜间断供检查失败")
        return "skipped-night"
    if budget_watch.in_report_window(now) and not force:
        log.info("播报档 ±%d 分钟内，本轮监控播报跳过",
                 budget_watch.SLOT_MARGIN_MIN)
        return "skipped-window"
    if budget_watch.report_busy() and not force:
        log.info("大节点播报扫描进行中（report_busy），本轮监控播报让行")
        return "skipped-report-busy"
    st = _load_state()
    if not force and not _due(st, now):
        log.info("距上轮不足 %d 分钟，跳过", INTERVAL_MIN)
        return "skipped-interval"
    lock = _acquire_lock()
    if lock is None:
        log.warning("已有监控播报实例在执行，本轮退出")
        return "skipped-lock"
    try:
        # 浏览器互斥（2026-09-01 12:24 教训：无锁扫描撞上 poll 复核，
        # poll 页面 goto 被 abort）：持启停动作锁 + 写忙标，poll 整轮跳过、
        # plan_power 等锁。拿不到就本轮跳过，绝不硬抢。
        action_fd = budget_watch._try_action_lock()
        if action_fd is None:
            log.info("启停动作锁被占用（plan_power 运行中），本轮跳过")
            return "skipped-busy"
        budget_watch._busy_mark("plan-focus-report")
        try:
            rows, plans_by_dep, board = asyncio.run(_scan())
            now = datetime.datetime.now()
            minutes = now.hour * 60 + now.minute
            notices: list[str] = []
            # 盘中预算安全闸（2026-09-15 费用安全件）：99999 不限额拦截 +
            # 超档位漂移下压。检出即改（此时已持动作锁+忙标），失败不影响播报。
            try:
                from 巨量千川 import plan_power
                promoted_ids = {
                    str(x.get("plan_id") or "")
                    for x in plan_power.load_state().get("new_plan_promotions") or []
                    if x.get("plan_id")
                }
                # 跳过集 = ignored 名单 ∪ 人工白名单（2026-09-16 用户纠正：
                # 味蕾食光/恩豆/陈树莉三条主管人工管理计划被 22:47 轮误压）。
                # 看板标签会在档间翻动（22:47 时它们是潜力标签而非 manual_review），
                # 必须直接查白名单本体；白名单读取异常=本轮不压（失败方向向「不动」）。
                from 巨量千川.manual_whitelist import match_manual_whitelist
                skip_ids = set(plan_power._ignored_resume_keys())
                for dep, dep_plans in plans_by_dep.items():
                    for p in dep_plans or []:
                        pid = str(getattr(p, "plan_id", None) or "")
                        if pid and match_manual_whitelist(
                                department=dep, plan_id=pid,
                                plan_name=str(getattr(p, "plan_name", "") or "")):
                            skip_ids.add(pid)
                clamp_targets = plan_power.budget_safety_clamp_targets(
                    plans_by_dep, board, promoted_ids, skip_ids=skip_ids)
                if clamp_targets:
                    log.info("预算安全闸目标 %d 条: %s", len(clamp_targets),
                             "、".join(f"{t['plan_name']}({t['current_budget']:g}→"
                                       f"{t['label_budget']:g}/{t['reason']})"
                                       for t in clamp_targets[:10]))
                    if push:
                        clamp_result = asyncio.run(
                            plan_power.clamp_budget_safety(clamp_targets))
                        notice = _safety_notice(clamp_result)
                        if notice:
                            notices.append(notice)
            except Exception:
                log.exception("预算安全闸本轮异常（不影响监控播报）")
            md = build_markdown(rows, datetime.datetime.now(), minutes,
                                notices=notices)
            log.info("监控播报内容（%d 条）:\n%s", len(rows), md)
            _fire_immediate_alerts(rows, push=push)
            # 2026-09-03 用户定：监控只监控，不再在此执行加预算队列
        finally:
            budget_watch._busy_clear()
            budget_watch._release_action_lock(action_fd)
        if not push:
            return "dry-run"
        if _push(md):
            st["last_push_ts"] = time.time()
            st["last_push_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save_state(st)
            return "pushed"
        return "failed"
    finally:
        _release_lock(lock)


def maybe_run() -> str:
    """poll 轮末钩子：频率/窗口/夜间守卫都在 run() 里。"""
    try:
        return run(push=True)
    except Exception:
        log.exception("监控播报本轮失败（不影响轮询主流程）")
        return "error"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="潜力/重点计划独立监控播报")
    ap.add_argument("--dry-run", action="store_true", help="只扫描打印，不推送")
    ap.add_argument("--force", action="store_true",
                    help="忽略播报档窗口/夜间/频率守卫（调试用手动推）")
    args = ap.parse_args()
    result = run(push=not args.dry_run, force=args.force)
    print("result:", result)
