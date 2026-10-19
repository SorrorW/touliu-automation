# -*- coding: utf-8 -*-
"""计划监控：白名单「全天跑」单点 + 潜力/重点计划分类预算与 ROI 监控。

2026-09-01 两轮需求：① 恩豆妈咪白名单全天跑 + 预算监控；② 潜力计划和重点
计划单独加预算+ROI 监控。全部只预警、不做动作（预算 policy=manual_only；
ROI 策略闸②③未上线，这里是监控提醒层）。

两条链路共用同一套判定：
- daily_report_push 各播报档生成日报后调用 check_from_report —— 复用刚扫的
  日报数据，零浏览器开销（分类监控只走这条链路，7 档覆盖 08:00-22:30）；
- poll_and_run 每轮结束时调用 check_live —— 逐条搜索白名单 budget_watch 计划
  （单条约 30s），播报档 ±12 分钟内自动跳过以免与日报扫描抢浏览器。

预警规则（每个级别每天每计划只推一次，状态文件 budget_watch_state.json）：
- 预算：消耗 ≥ 预算 80% → 「预算预警」；≥100% 或状态含「预算不足/耗尽」→「预算断供」
  （预算读数 None/0/99999=不限 不监控）；
- ROI（潜力/重点，仅投放中计划）：低于该分类该时段策略线 →「ROI预警」；
  消耗 ≥ 2×起报门槛仍 0 单 →「有消耗未出单」。起报门槛防零耗噪声。
独立监控告警推送到 `config.json` 的 `monitor_webhook` 群。

2026-09-03 用户改：**监控只监控**——预算类告警（warn/exhausted）只推送提醒，
不再自动入加预算队列。加预算只发生在「晋升潜力计划」时：升档/终分类把剩余
预算补足到 300+（plan_power._promote_new_plan_noon / _top_up_budget_to_remaining）。
原自动加预算队列机制（budget_topup_queue.json + process_topup_queue）保留为
手动备用：直接 `python budget_watch.py` 仍会执行存量队列；自动链路已全部摘除。
"""
from __future__ import annotations

import datetime
import math
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "投流")))

from 巨量千川.manual_whitelist import WHITELIST_FILE  # noqa: E402

LOG_DIR = os.path.join(HERE, "logs")
STATE_FILE = os.path.join(LOG_DIR, "budget_watch_state.json")
TOPUP_QUEUE_FILE = os.path.join(LOG_DIR, "budget_topup_queue.json")
ACTION_LOCK_FILE = os.path.join(HERE, "plan_power_action.lock")
BUSY_FILE = os.path.join(HERE, "plan_power_busy.json")
UNLIMITED = 99999.0  # 千川「不限预算」读数
WARN_RATIO = 0.8
REPORT_SLOTS = ("08:00", "10:00", "12:00", "14:00", "16:00", "17:00", "22:30")
# 2026-09-01 用户定：预算监控固定每小时 :15/:45。整点播报档扫描 ~13 分钟内
# 结束，守卫窗收窄到 ±12 分钟让 :15 监控穿过；扫描超时由 report_busy 忙标兜底。
SLOT_MARGIN_MIN = 12
TOPUP_TARGET_REMAINING = 300.0  # 自动加预算后至少保留的日预算额度
TOPUP_ROUNDING = 100.0           # 增加金额按 100 元向上取整
TOPUP_MAX_ATTEMPTS = 3    # 单条加预算失败重试上限
CONFIG_FILE = os.path.join(HERE, "config.json")


def load_suppressed_alert_plan_ids() -> set[str]:
    """读取不发送预算告警的计划 ID。

    这是告警层抑制，不隐藏常态监控行，不修改千川计划本身，也不影响日报或其他计划。
    """
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    raw = cfg.get("monitor_suppressed_alert_plan_ids") or []
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(pid).strip() for pid in raw if str(pid).strip()}


def is_alert_suppressed(plan_id: str) -> bool:
    return str(plan_id or "").strip() in load_suppressed_alert_plan_ids()

# 潜力/重点分类监控规则（阈值与用户已定策略线一致：潜力 12:00<6/17:00<7.3 降级闸；
# 重点 四道闸 0-8>6 / 0-12>7.3 / 0-17>8.5 / 0-23>10）。
# roi_floor: (起始分钟数, 该时段起的 ROI 预警线)，按起始分钟倒序匹配。
CATEGORY_RULES = {
    "potential_plan": {
        "label": "潜力计划", "min_spend": 30.0,
        "roi_floor": [(17 * 60, 7.3), (0, 6.0)],
    },
    "key_plan": {
        "label": "重点计划", "min_spend": 50.0,
        "roi_floor": [(23 * 60, 10.0), (17 * 60, 8.5), (12 * 60, 7.3), (0, 6.0)],
    },
}

log = logging.getLogger("budget_watch")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_h)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _fh = logging.FileHandler(
            os.path.join(LOG_DIR, f"budget_watch_{datetime.date.today():%Y%m%d}.log"),
            encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(_fh)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 监控名单与状态
# ---------------------------------------------------------------------------

def load_watch_entries() -> list[dict]:
    """白名单中带 budget_watch=true 且有 plan_id 的条目。"""
    try:
        raw = json.loads(open(WHITELIST_FILE, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for entry in raw.get("creators", []):
        if not isinstance(entry, dict) or not entry.get("budget_watch"):
            continue
        for pid in entry.get("plan_ids") or []:
            out.append({
                "name": str(entry.get("name") or ""),
                "department": str(entry.get("department") or ""),
                "plan_id": str(pid),
            })
    return out


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


def _already(st: dict, pid: str, level: str, today: str) -> bool:
    return st.get("date") == today and level in (st.get("alerts", {}).get(pid) or [])


def _mark(st: dict, pid: str, level: str, today: str) -> dict:
    if st.get("date") != today:
        st = {"date": today, "alerts": {}}
    st.setdefault("alerts", {}).setdefault(pid, []).append(level)
    return st


# ---------------------------------------------------------------------------
# 判定（纯函数）
# ---------------------------------------------------------------------------

def evaluate(spent, budget, status) -> str | None:
    """预算水位判定：'exhausted'（断供）/ 'warn'（≥80%）/ None（不监控或正常）。"""
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        return None
    if budget <= 0 or budget >= UNLIMITED:
        return None
    spent = float(spent or 0)
    status_text = str(status or "")
    if spent >= budget or "预算不足" in status_text or "预算耗尽" in status_text:
        return "exhausted"
    if spent >= budget * WARN_RATIO:
        return "warn"
    return None


def roi_floor_for(category: str, minutes: int) -> float | None:
    """该分类在一天中某时刻（分钟数）的 ROI 预警线；未配置分类返回 None。"""
    rule = CATEGORY_RULES.get(category)
    if not rule:
        return None
    for start, floor in rule["roi_floor"]:
        if minutes >= start:
            return floor
    return None


def evaluate_roi(category: str, roi, orders, spent, minutes: int) -> str | None:
    """ROI 判定（仅潜力/重点）：'roi_low'（低于时段线）/ 'no_orders'（高耗零单）/ None。

    起报门槛：消耗 < min_spend 不评（零耗噪声）；消耗 ≥ 2×min_spend 且 0 单
    → 'no_orders'（此时 roi 多半为 None，不能当正常放过）。
    """
    rule = CATEGORY_RULES.get(category)
    if not rule:
        return None
    spent = float(spent or 0)
    min_spend = rule["min_spend"]
    if spent < min_spend:
        return None
    orders = int(orders or 0)
    if orders == 0:
        return "no_orders" if spent >= min_spend * 2 else None
    floor = roi_floor_for(category, minutes)
    if floor is None or roi is None:
        return None
    return "roi_low" if float(roi) < floor else None


def in_report_window(now: datetime.datetime | None = None,
                     margin: int = SLOT_MARGIN_MIN) -> bool:
    """播报档 ±margin 分钟内为 True（此时段不做 live 扫描，避免抢浏览器）。"""
    now = now or datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    for slot in REPORT_SLOTS:
        hh, mm = int(slot[:2]), int(slot[3:])
        if abs(cur - (hh * 60 + mm)) <= margin:
            return True
    return False


def report_busy() -> bool:
    """大节点播报扫描正在写浏览器（report_busy.json，15 分钟时效兜底）。

    2026-09-01 用户定：预算监控固定在每小时 :15/:45——整点档扫描通常 13 分钟内
    结束，:15 已出 ±12 分钟守卫窗；若扫描超时拖到 :15 之后，靠本忙标兜底跳过。
    """
    try:
        with open(os.path.join(HERE, "report_busy.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        return datetime.datetime.now().timestamp() - float(payload.get("ts", 0)) < 15 * 60
    except Exception:
        return False


def _alert_md(entry: dict, *, spent, budget, status, orders, roi, level: str,
              tag: str = "全天跑白名单", roi_floor: float | None = None) -> str:
    now = datetime.datetime.now()
    ratio = f"{spent / budget * 100:.0f}%" if budget else "--"
    roi_text = f"，ROI {roi:g}" if isinstance(roi, (int, float)) else ""
    plan_line = f"> {entry['department']}｜{entry['name']}（{tag}，ID {entry['plan_id']}）"
    if level in ("roi_low", "no_orders"):
        lines = [f"**【ROI预警】{now:%m月%d日 %H:%M}**", plan_line]
        if level == "no_orders":
            lines.append(f"> 今日消耗 {spent:,.2f} 元仍 **0** 单（高耗未出单）")
        else:
            lines.append(
                f"> 今日 ROI **{roi:g}** 低于{tag}当前时段线 {roi_floor:g}"
                f"，消耗 {spent:,.2f} 元，成交 {int(orders or 0)} 单")
        lines.append("> 仅监控提醒，未做动作；如需处理请登录千川调整。")
        return "\n".join(lines)
    title = "预算断供" if level == "exhausted" else "预算预警"
    lines = [
        f"**【{title}】{now:%m月%d日 %H:%M}**",
        plan_line,
        f"> 今日消耗 {spent:,.2f} / 预算 {budget:,.0f} 元（{ratio}），成交 {int(orders or 0)} 单{roi_text}",
    ]
    if level == "exhausted":
        lines.append("> 已达预算上限，平台将限投/暂停。要继续跑：请提高日预算；"
                     "若已显示暂停，加预算后需手动开启（白名单计划自动化不代开）。")
    else:
        lines.append("> 预算即将耗尽。要跑全天：请提前提高日预算。")
    return "\n".join(lines)


def _push_alert(md: str) -> bool:
    import alert_outbox
    import daily_report_push as push_mod
    url = push_mod.load_monitor_webhook()
    if not url:
        log.warning("config.json 未配置 monitor_webhook，预算监控播报无法推送")
        return False
    done, left = push_mod.push_missing({"汇总": url}, md, {})
    if left:
        # 推失败入发件箱：通道恢复后由任一成功推送顺手补发（告警一致性 2026-09-14）
        alert_outbox.enqueue("预算监控", md, url)
        return False
    alert_outbox.flush()
    return True


# ---------------------------------------------------------------------------
# 链路一：复用日报扫描结果（零浏览器开销）
# ---------------------------------------------------------------------------

def check_from_report(data: dict, *, push: bool = True) -> list[str]:
    """日报生成后调用。data = make_report 输出（departments→categories→plans）。

    白名单 budget_watch 计划：任何分类都查预算水位（恩豆妈咪全天跑）。
    潜力/重点分类：预算水位 + ROI 时段线（仅「投放中」计划，已暂停的不报，
    避免与熔断/收口动作重复提醒）。categories 只含有消耗计划，天然过滤零耗。
    """
    suppressed = load_suppressed_alert_plan_ids()
    watched = {e["plan_id"]: e for e in load_watch_entries()}
    # ROI 时段线按数据归属的扫描档位取（比按当前时间更准）
    slot_text = str((data or {}).get("scan_slot") or "")
    try:
        hh, mm = int(slot_text[:2]), int(slot_text[3:5])
        minutes = hh * 60 + mm
    except (ValueError, IndexError):
        now = datetime.datetime.now()
        minutes = now.hour * 60 + now.minute

    today = datetime.date.today().isoformat()
    st = _load_state()
    alerts: list[str] = []
    covered = 0
    fired: set[tuple[str, str]] = set()  # 本轮内去重（白名单+分类两条路径不重复报）

    def _fire(pid: str, level: str, md: str, desc: str, entry: dict) -> None:
        nonlocal st
        if (pid, level) in fired or _already(st, pid, level, today):
            return
        fired.add((pid, level))
        log.info("命中 %s：%s（%s）", level, desc, pid)
        if not push:
            # dry-run：只报告不推送，不写去重状态也不入加预算队列
            alerts.append(f"{level}:{pid}")
            return
        if _push_alert(md):
            st = _mark(st, pid, level, today)
            alerts.append(f"{level}:{pid}")
            # 2026-09-03 用户定：监控只监控，预算类告警不再自动入加预算队列

    for dep, d in ((data or {}).get("departments") or {}).items():
        for cat_key, plans in ((d or {}).get("categories") or {}).items():
            rule = CATEGORY_RULES.get(cat_key)
            for p in plans or []:
                pid = str(p.get("plan_id") or "")
                if not pid:
                    continue
                spent = float(p.get("spent") or 0)
                status = str(p.get("status") or "")
                # 1) 白名单预算监控（不限分类；恩豆妈咪即便被分类变化也不丢监控）
                entry = watched.get(pid)
                if entry is not None:
                    level = evaluate(spent, p.get("budget"), status)
                    if level and pid not in suppressed:
                        _fire(pid, level, _alert_md(
                            entry, spent=spent, budget=float(p.get("budget") or 0),
                            status=status, orders=p.get("orders"),
                            roi=p.get("roi"), level=level), entry["name"], entry)
                # 2) 潜力/重点分类监控：预算 + ROI（仅投放中）
                if rule is None or "投放中" not in status:
                    continue
                covered += 1
                cat_entry = {"name": p.get("plan_name") or pid,
                             "department": dep, "plan_id": pid}
                tag = rule["label"]
                level = evaluate(spent, p.get("budget"), status)
                if level and pid not in suppressed:
                    _fire(pid, level, _alert_md(
                        cat_entry, spent=spent, budget=float(p.get("budget") or 0),
                        status=status, orders=p.get("orders"),
                        roi=p.get("roi"), level=level, tag=tag), f"{tag}预算",
                        cat_entry)
                roi_level = evaluate_roi(cat_key, p.get("roi"), p.get("orders"),
                                         spent, minutes)
                if roi_level:
                    _fire(pid, roi_level, _alert_md(
                        cat_entry, spent=spent, budget=float(p.get("budget") or 0),
                        status=status, orders=p.get("orders"), roi=p.get("roi"),
                        level=roi_level, tag=tag,
                        roi_floor=roi_floor_for(cat_key, minutes)), f"{tag}ROI",
                        cat_entry)
    log.info("预算监控检查：覆盖 %d 条在投潜力/重点，告警 %d 条（档位 %s）",
             covered, len(alerts), slot_text or "无")
    _save_state(st)
    return alerts


# ---------------------------------------------------------------------------
# 链路二：poll 每轮结束后的 live 扫描（约 30s/条）
# ---------------------------------------------------------------------------

def check_live(*, push: bool = True) -> list[str]:
    entries = load_watch_entries()
    if not entries:
        return []
    if in_report_window() or report_busy():
        log.info("播报档窗口/播报扫描进行中，本轮预算监控跳过")
        return []
    import asyncio
    from 巨量千川.plan_metrics import search_plan_metrics

    today = datetime.date.today().isoformat()
    suppressed = load_suppressed_alert_plan_ids()
    st = _load_state()
    alerts: list[str] = []
    for entry in entries:
        try:
            rows = asyncio.run(search_plan_metrics(
                plan_name=entry["plan_id"],  # 按 ID 搜索（名称含全角逗号会搜不到）
                department=entry["department"] or "业务2部",
            ))
        except Exception as exc:
            log.warning("预算监控搜索失败 %s（%s）: %s", entry["name"], entry["plan_id"], exc)
            continue
        m = next((r for r in rows if str(r.plan_id or "") == entry["plan_id"]),
                 rows[0] if rows else None)
        if m is None:
            log.warning("预算监控未搜到计划 %s（%s）", entry["name"], entry["plan_id"])
            continue
        level = evaluate(m.spent, m.budget, m.status)
        log.info("预算监控 %s：消耗 %.2f / 预算 %s，状态 %s → %s",
                 entry["name"], m.spent or 0, m.budget, m.status, level or "正常")
        if not level or entry["plan_id"] in suppressed or _already(st, entry["plan_id"], level, today):
            continue
        md = _alert_md(entry, spent=float(m.spent or 0),
                       budget=float(m.budget or 0), status=m.status,
                       orders=m.orders, roi=m.roi, level=level)
        if not push:
            alerts.append(f"{level}:{entry['plan_id']}")
            continue
        if _push_alert(md):
            st = _mark(st, entry["plan_id"], level, today)
            alerts.append(f"{level}:{entry['plan_id']}")
            # 2026-09-03 用户定：监控只监控，预算类告警不再自动入加预算队列
    _save_state(st)
    return alerts


# ---------------------------------------------------------------------------
# 加预算队列（2026-09-03 起仅手动备用：自动入队/自动执行的链路已摘除，
# 直接 python budget_watch.py 才会执行存量队列；金额在执行时按实时数据动态计算）
# ---------------------------------------------------------------------------

def _load_topup_queue() -> list[dict]:
    try:
        q = json.loads(open(TOPUP_QUEUE_FILE, encoding="utf-8").read())
        if isinstance(q, list):
            return q
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_topup_queue(q: list[dict]) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    # 只留最近 50 条，pending 优先
    q = [i for i in q if i.get("status") == "pending"] + \
        [i for i in q if i.get("status") != "pending"][-50:]
    with open(TOPUP_QUEUE_FILE, "w", encoding="utf-8") as fh:
        json.dump(q, fh, ensure_ascii=False, indent=1)


def calculate_topup_amount(budget, spent, *, target_remaining: float = TOPUP_TARGET_REMAINING,
                           rounding: float = TOPUP_ROUNDING) -> float:
    """根据执行时的日预算和今日消耗计算需要增加的金额。

    剩余额度定义为 ``budget - spent``。已达到目标额度时返回 0；否则将缺口
    按 ``rounding`` 向上取整。该函数不处理不限预算，调用方应先跳过不限预算。
    """
    try:
        budget = float(budget)
        spent = float(spent or 0)
        target_remaining = float(target_remaining)
        rounding = float(rounding)
    except (TypeError, ValueError) as exc:
        raise ValueError("预算、消耗、目标额度和取整单位必须是数字") from exc
    if (not all(math.isfinite(value) for value in
                (budget, spent, target_remaining, rounding)) or
            budget < 0 or spent < 0 or target_remaining < 0 or rounding <= 0):
        raise ValueError("预算、消耗、目标额度必须为有限非负数，取整单位必须为正数")
    remaining = budget - spent
    deficit = target_remaining - remaining
    if deficit <= 0:
        return 0.0
    # 仅在比整倍数多出浮点误差时回落到整倍数；真实的微小缺口仍向上取整。
    ratio = deficit / rounding
    nearest = round(ratio)
    units = nearest if abs(ratio - nearest) < 1e-9 else math.ceil(ratio)
    return float(units * rounding)


def _enqueue_topup(entry: dict, level: str) -> bool:
    """预算类告警推送成功后入队。同 pid 已有 pending 不重复入队。
    队列只保存告警来源；具体增加金额必须在执行时按页面实时数据计算。"""
    q = _load_topup_queue()
    pid = entry["plan_id"]
    if any(i.get("plan_id") == pid and i.get("status") == "pending" for i in q):
        log.info("加预算队列已有 %s 待执行，跳过重复入队", pid)
        return False
    q.append({
        "plan_id": pid, "plan_name": entry.get("name") or pid,
        "department": entry.get("department") or "",
        "level": level,
        "enqueued_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "pending", "attempts": 0,
    })
    _save_topup_queue(q)
    log.info("加预算入队：%s（%s，%s 触发；金额待执行时按实时额度计算）",
             entry.get("name"), pid, level)
    return True


async def _topup_one(page, item: dict) -> dict:
    """按执行时的日预算和今日消耗动态加预算；失败抛异常。

    页面上的今日消耗来自「今天」范围的计划行，队列中的历史 amount 字段
    （若有）不会参与计算。返回值包含实际增加金额和加预算后的剩余额度。"""
    from 巨量千川.plan_metrics import _parse_plan_row, _search_plans
    from 巨量千川.old_creator_retarget import _open_edit_page, _wait_stable
    from 巨量千川.plan_sweep import _save_edit_page
    from 巨量千川.budget_raise import _read_budget, _set_budget

    pid = item["plan_id"]
    dep = item["department"] or "业务2部"
    rows = await _search_plans(page, pid, dep, by_creator=False,
                               time_range="today")
    target = None
    target_metrics = None
    for tr in rows:
        m = await _parse_plan_row(tr)
        if m is None:  # 畸形行已留 WARNING，跳过
            continue
        if str(m.plan_id or "") == pid:
            target = tr
            target_metrics = m
            break
    if target is None or target_metrics is None:
        raise RuntimeError("计划行未找到（today 按 ID 搜索）")
    await _open_edit_page(page, target)
    await _wait_stable(page, 1.5)
    before = await _read_budget(page)
    if before >= UNLIMITED:
        return {"before": before, "after": before, "spent": target_metrics.spent,
                "remaining_before": None, "remaining_after": None, "skipped": "已不限预算"}
    spent = float(target_metrics.spent or 0)
    remaining_before = before - spent
    amount = calculate_topup_amount(before, spent)
    if amount <= 0:
        return {"before": before, "after": before, "spent": spent,
                "remaining_before": remaining_before, "remaining_after": remaining_before,
                "amount": 0.0, "skipped": "剩余额度已达到 300 元"}
    after = int(min(before + amount, UNLIMITED))
    await _set_budget(page, after)
    if not await _save_edit_page(page):
        raise RuntimeError("编辑页未找到保存按钮")
    return {"before": before, "after": after, "spent": spent,
            "remaining_before": remaining_before, "remaining_after": after - spent,
            "amount": after - before}


def _try_action_lock():
    """单次非阻塞拿启停动作互斥锁（与 plan_power 同一把）。拿不到返回 None。
    绝不等待——本轮跳过，下轮再试，避免与 plan_power 的等锁顺序成环。"""
    import msvcrt
    fd = open(ACTION_LOCK_FILE, "a+b")
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


def _release_action_lock(fd) -> None:
    import msvcrt
    try:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fd.close()


def _busy_mark(action: str) -> None:
    """写忙标让 poll 整轮跳过（与 plan_power 同一个 plan_power_busy.json）。

    必须带 pid：poll 用 OpenProcess 探活，缺 pid 的忙标在进程死后要等满
    45 分钟时效才放行（2026-10-09 plan-focus-report 残留卡了补投一轮）。
    """
    try:
        import os as _os
        with open(BUSY_FILE, "w", encoding="utf-8") as fh:
            json.dump({"action": action, "ts": __import__("time").time(),
                       "pid": _os.getpid()},
                      fh, ensure_ascii=False)
    except OSError:
        pass


def _busy_clear() -> None:
    try:
        os.remove(BUSY_FILE)
    except OSError:
        pass


def _topup_done_count(st: dict, pid: str, today: str) -> int:
    if st.get("date") != today:
        return 0
    return int((st.get("topups") or {}).get(pid) or 0)


def _mark_topup(st: dict, pid: str, today: str) -> dict:
    if st.get("date") != today:
        st = {"date": today, "alerts": {}}
    topups = st.setdefault("topups", {})
    topups[pid] = int(topups.get(pid) or 0) + 1
    return st


def _topup_md(item: dict, result: dict, count_today: int) -> str:
    now = datetime.datetime.now()
    return "\n".join([
        f"**【自动加预算】{now:%m月%d日 %H:%M}**",
        f"> {item['department']}｜{item['plan_name']}（{item.get('level')} 告警触发）",
        f"> 日预算 {result['before']:,.0f} → **{result['after']:,.0f}** 元"
        f"（+{result.get('amount', result['after'] - result['before']):,.0f}），"
        f"消耗 {result.get('spent', 0):,.2f} 元，剩余额度 **{result.get('remaining_after', result['after'] - result.get('spent', 0)):,.2f} 元**，"
        f"今日第 {count_today} 次自动加",
    ])


async def _run_topup_items(page, pending: list[dict], *, push: bool) -> list[str]:
    """逐条执行加预算（page 由会话层注入；单测传 None 并打桩 _topup_one）。"""
    results: list[str] = []
    for item in pending:
        item["attempts"] = int(item.get("attempts") or 0) + 1
        try:
            res = await _topup_one(page, item)
        except Exception as exc:
            err = str(exc)[:150]
            log.warning("自动加预算失败 %s（%s）第 %d 次: %s",
                        item["plan_name"], item["plan_id"],
                        item["attempts"], err)
            if item["attempts"] >= TOPUP_MAX_ATTEMPTS:
                item["status"] = "dead"
                item["error"] = err
                results.append(f"dead:{item['plan_id']}")
                if push:
                    _push_alert(
                        f"**【自动加预算失败】**\n> {item['department']}｜"
                        f"{item['plan_name']} 连续 {TOPUP_MAX_ATTEMPTS} 次失败"
                        f"（{err}），请人工处理")
            else:
                results.append(f"retry:{item['plan_id']}")
            continue
        item["status"] = "done"
        item["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        if res.get("skipped"):
            log.info("自动加预算跳过 %s：%s", item["plan_name"], res["skipped"])
            results.append(f"skipped:{item['plan_id']}")
            continue
        today = datetime.date.today().isoformat()
        st = _mark_topup(_load_state(), item["plan_id"], today)
        _save_state(st)
        count = _topup_done_count(st, item["plan_id"], today)
        log.info("自动加预算成功 %s：日预算 %.0f → %.0f（+%.0f，剩余额度 %.2f，今日第 %d 次）",
                 item["plan_name"], res["before"], res["after"],
                 res.get("amount", res["after"] - res["before"]),
                 res.get("remaining_after", 0), count)
        results.append(f"done:{item['plan_id']}")
        if push:
            _push_alert(_topup_md(item, res, count))
    return results


async def _topup_session(pending: list[dict], *, push: bool) -> list[str]:
    """开一个浏览器会话跑完整个队列（单测打桩此函数以免连真浏览器）。"""
    from playwright.async_api import async_playwright
    from 巨量千川.plan_metrics import _ensure_cdp
    from 巨量千川.session_prepare import connect_browser, open_fresh_page

    _ensure_cdp()
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        page = await open_fresh_page(browser.contexts[0])
        try:
            return await _run_topup_items(page, pending, push=push)
        finally:
            try:
                await page.close()
            except Exception:
                pass


def process_topup_queue(*, push: bool = True, use_lock: bool = True) -> list[str]:
    """执行加预算队列。返回结果描述列表（dry-run 只列不动）。

    use_lock=True 时自持启停动作锁（非阻塞，拿不到本轮跳过）并写忙标；
    plan_focus_report 已持锁时传 False 复用。播报档 ±12 分钟内跳过。
    """
    q = _load_topup_queue()
    suppressed = load_suppressed_alert_plan_ids()
    for item in q:
        if item.get("status") == "pending" and str(item.get("plan_id") or "") in suppressed:
            item["status"] = "cancelled"
            item["cancelled_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            item["cancel_reason"] = "monitor_excluded"
            log.info("计划 %s 已抑制告警，取消由该告警触发的待执行加预算队列", item.get("plan_id"))
    pending = [i for i in q if i.get("status") == "pending"]
    if not pending:
        _save_topup_queue(q)
        return []
    if in_report_window() or report_busy():
        log.info("播报档窗口/播报扫描进行中，加预算队列（%d 条）留待下轮",
                 len(pending))
        return []
    lock = None
    if use_lock:
        lock = _try_action_lock()
        if lock is None:
            log.info("启停动作锁被占用，加预算队列（%d 条）留待下轮", len(pending))
            return []
        _busy_mark("budget-topup")
    results: list[str] = []
    try:
        if push:
            import asyncio
            results = asyncio.run(_topup_session(pending, push=push))
        else:
            log.info("dry-run：加预算队列 %d 条仅列出不执行", len(pending))
            results.extend(f"pending:{i['plan_id']}" for i in pending)
            return results
    finally:
        _save_topup_queue(q)
        if use_lock and lock is not None:
            _busy_clear()
            _release_action_lock(lock)
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="白名单全天跑计划预算监控")
    ap.add_argument("--dry-run", action="store_true", help="只检查不推送")
    args = ap.parse_args()
    hits = check_live(push=not args.dry_run)
    print("alerts:", hits or "无")
    topups = process_topup_queue(push=not args.dry_run)
    print("topups:", topups or "无")