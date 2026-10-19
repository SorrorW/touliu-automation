# -*- coding: utf-8 -*-
"""投放日报分时段推送任务。

七个播报时间点均推送今天截至扫描时点的数据：08:00、10:00、12:00、14:00、16:00、17:00 只播扫描时仍在投放且有消耗的计划；22:30 用当天全量计划数作分母，但明细只播有消耗计划（含早前投放后暂停的计划）。

每个时段一个计划任务（08:00、10:00、12:00、14:00、16:00、17:00、22:30），
触发本脚本并带 --slot；失败重试 3 轮（间隔 10 分钟），逐群逐时段记账不重复推。

状态文件 daily_report_state.json：{"date": "YYYY-MM-DD", "pushed": ["morning:业务1部", ...]}。
日报只能由本脚本的独立计划任务发送；进程锁防止同一时段被重复启动时重复推送。

手动执行：
  python daily_report_push.py --slot noon            立即扫描并真实推送午间播报
  python daily_report_push.py --slot 10am --dry-run   检查10:00在投播报，不推送
  python daily_report_push.py --slot noon --dry-run  只扫描+打印，不推送
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import msvcrt
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "daily_report_state.json"
LOCK_FILE = HERE / ".daily_report_push.lock"
CONFIG_FILE = HERE / "config.json"
BUSY_FILE = HERE / "report_busy.json"
MAX_ATTEMPTS = 3
RETRY_INTERVAL = 600  # 秒
SCHEDULE_GRACE_MINUTES = 20  # 迟到补跑只允许在原时段后 20 分钟内继续


def _busy_mark(slot: str) -> None:
    """大节点播报扫描忙标（2026-09-01 用户定：播报扫描优先，其他程序让行）。

    浏览器扫描（含 14:00/16:00 的下午跟进动作）期间写 report_busy.json，
    扫描结束即删；poll 轮首/记录间和 plan_power 动作入口都会等它清空。
    推送（纯 HTTP）不在忙标内。崩溃残留由消费方 15 分钟时效兜底。
    """
    try:
        BUSY_FILE.write_text(json.dumps(
            {"slot": slot, "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _busy_clear() -> None:
    try:
        BUSY_FILE.unlink()
    except OSError:
        pass

# 时段 → (扫描时间范围, 标题后缀)
SLOTS = {
    # 08:00—16:00 为阶段性播报，只展示扫描时仍在投放的计划。
    "morning": ("今天", "（截至 08:00）"),
    "10am": ("今天", "（截至 10:00）"),
    "noon": ("今天", "（截至 12:00）"),
    "14pm": ("今天", "（截至 14:00）"),
    "16pm": ("今天", "（截至 16:00）"),
    # 17:00 只播当前在投且有消耗；22:30 使用当天全量分母，明细仍过滤无消耗。
    "evening": ("今天", "（截至 17:00）"),
    "night": ("今天", "（截至 22:30）"),
}
SLOT_SCAN_TIMES = {
    "morning": "08:00", "10am": "10:00", "noon": "12:00",
    "14pm": "14:00", "16pm": "16:00", "evening": "17:00", "night": "22:30",
}
DEFAULT_SLOT = "evening"

sys.path.insert(0, str(HERE))
import local_tools  # noqa: E402

log = logging.getLogger("daily_report_push")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
log.addHandler(_ch)
try:
    log_dir = HERE / "logs"
    log_dir.mkdir(exist_ok=True)
    _fh = logging.FileHandler(
        log_dir / f"report_{datetime.date.today():%Y%m%d}.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except OSError:
    pass


# ---------------------------------------------------------------------------
# 状态：键格式 "时段:群名"，如 "morning:业务1部"、"evening:汇总"
# ---------------------------------------------------------------------------

def acquire_report_lock():
    """获取日报进程锁；已有实例在扫描或重试时直接退出，避免重复群发。"""
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


def release_report_lock(fd) -> None:
    try:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fd.close()

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def is_pushed(state: dict, name: str, today: str, slot: str = DEFAULT_SLOT) -> bool:
    return (state.get("date") == today
            and f"{slot}:{name}" in (state.get("pushed") or []))


def mark_pushed(state: dict, names: list[str], today: str,
                slot: str = DEFAULT_SLOT) -> dict:
    if state.get("date") != today:
        state = {"date": today, "pushed": []}
    state["pushed"] = sorted(
        set(state.get("pushed") or []) | {f"{slot}:{n}" for n in names})
    return state


def all_pushed(state: dict, webhooks: dict, today: str,
               slot: str = DEFAULT_SLOT) -> bool:
    return bool(webhooks) and all(is_pushed(state, k, today, slot) for k in webhooks)


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------

def push_missing(pending: dict, md_full: str, md_by_dept: dict) -> tuple[list, list]:
    """向 pending（群名→webhook）逐群推送。返回 (成功群名, 未完成群名)。"""
    done, left = [], []
    for name, url in pending.items():
        content = md_full if name == "汇总" else (md_by_dept or {}).get(name)
        if not content:
            log.warning("日报里找不到「%s」的数据，跳过该群", name)
            left.append(name)
            continue
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(
                    {"msgtype": "markdown", "markdown": {"content": content}}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("errcode", 0) == 0:
                done.append(name)
                log.info("日报已推送到「%s」群", name)
            else:
                log.warning("「%s」群推送失败: %s", name, body)
                left.append(name)
        except Exception as exc:
            msg = str(exc)
            if "10013" in msg or "Permission denied" in msg:
                log.error("「%s」群推送被本地拦截(WinError 10013)：企微 webhook 出站 443 "
                          "被防火墙/代理拒绝。请放通 qyapi.weixin.qq.com:443 出站，"
                          "或在运行环境配置代理后重试；该群本次不计入已推送。", name)
            else:
                log.warning("「%s」群推送异常: %s", name, exc)
            left.append(name)
    return done, left


def load_webhooks() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("读取 config.json 失败: %s", exc)
        return {}
    hooks = cfg.get("report_webhooks") or {}
    if isinstance(hooks, list):  # 兼容旧的列表写法：统一推汇总
        hooks = {"汇总": u for u in hooks}
    return {k: v for k, v in hooks.items() if k and v}


def load_monitor_webhook() -> str:
    """读取独立监控播报群 Webhook，不纳入日报分群配置。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("读取 config.json 失败: %s", exc)
        return ""
    url = cfg.get("monitor_webhook")
    return url.strip() if isinstance(url, str) else ""


def run(args) -> int:
    slot = args.slot
    time_range, suffix = SLOTS[slot]
    now = datetime.datetime.now()
    if args.force:
        # --force 重跑：用真实扫描时刻打标签并归类历史，避免用旧时段标签
        # 冒充数据（如 16:39 重跑 14pm 却标「截至14:00」），且不污染原时段的
        # 历史快照槽位（底层按 scan_slot 归类 SQLite，传旧标签会覆盖写）。
        real_slot = now.strftime("%H:%M")
        suffix = f"（截至 {real_slot} 补发）"
        report_slot = real_slot
    else:
        report_slot = SLOT_SCAN_TIMES[slot]

    webhooks = load_webhooks()
    if not webhooks:
        log.error("config.json 未配置 report_webhooks，退出")
        return 1
    today = datetime.date.today().isoformat()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # 任务计划可能因关机/占用被延迟触发；白天槽位一旦跨过下一个
        # 20 分钟保护窗就停止重试，避免 10/14/16 点的失败重试占住日报锁，
        # 进而让 17:00 收口拿到“锁失败但返回成功”的假成功。
        if os.environ.get("TOULIU_SCHEDULED") == "1" and slot not in ("evening", "night"):
            scheduled = datetime.datetime.combine(
                now.date(), datetime.datetime.strptime(SLOT_SCAN_TIMES[slot], "%H:%M").time()
            )
            if datetime.datetime.now() - scheduled > datetime.timedelta(minutes=SCHEDULE_GRACE_MINUTES):
                log.warning("计划任务补跑已超过 %d 分钟保护窗，停止时段[%s]重试", SCHEDULE_GRACE_MINUTES, slot)
                return 1
        state = load_state()
        pending = (dict(webhooks) if args.force else {
            k: u for k, u in webhooks.items()
            if not is_pushed(state, k, today, slot)
        })
        if not pending:
            log.info("今天 %s 时段[%s]已全部推送过，无需再推", today, slot)
            return 0

        log.info("第 %d/%d 轮：时段[%s] 扫描三部门[%s]数据…（待推群：%s）",
                 attempt, MAX_ATTEMPTS, slot, time_range, "/".join(pending))
        # report_slot 已在循环外按是否 --force 决定（--force 用真实扫描时刻，
        # 避免旧时段标签冒充数据、且不污染原时段历史快照槽）。
        # 14:00/16:00 先执行实际升档/补预算，再生成同一时点播报，
        # 让播报反映最新 ROI/预算；不在下午执行 ROI<6 熔断。
        # 浏览器段全程挂忙标：poll/plan_power/监控播报让行，扫描结束即放。
        _busy_mark(slot)
        try:
            if report_slot in ("14:00", "16:00") and not args.skip_follow_up:
                follow_ok, follow_data = local_tools.run_afternoon_follow_up(report_slot, dry_run=args.dry_run)
                if not follow_ok:
                    log.warning("时段[%s]下午跟进失败，仍继续生成日报: %s", report_slot, str(follow_data)[:300])
                else:
                    log.info("时段[%s]下午跟进完成: %s", report_slot, json.dumps(follow_data, ensure_ascii=False)[:1000])
            ok, data = local_tools.run_daily_report(
                time_range=time_range, suffix=suffix, scan_slot=report_slot
            )
        finally:
            _busy_clear()
        if not ok:
            log.warning("日报生成失败: %s", str(data)[:200])
        else:
            md_full = data.get("markdown") or ""
            md_by_dept = data.get("markdown_by_dept") or {}
            # 白名单「全天跑」计划预算监控：复用本次日报扫描结果，零浏览器开销。
            # dry-run 时只检查不推送。
            try:
                import budget_watch
                budget_watch.check_from_report(data, push=not args.dry_run)
            except Exception:
                log.warning("预算监控检查失败", exc_info=True)
            log.info("日报内容:\n%s", md_full)
            if args.dry_run:
                log.info("dry-run：不推送。各群内容如下：")
                for name in pending:
                    log.info("——「%s」群——\n%s", name,
                             md_full if name == "汇总" else md_by_dept.get(name, "(无数据)"))
                return 0
            done, left = push_missing(pending, md_full, md_by_dept)
            state = mark_pushed(state, done, today, slot)
            save_state(state)
            if not left:
                log.info("时段[%s]日报已全部推送（%d 个群）", slot, len(webhooks))
                # 推成功=企微通道已恢复：顺手补发发件箱积压（告警一致性 2026-09-14）
                try:
                    import alert_outbox
                    alert_outbox.flush()
                except Exception:
                    log.warning("告警发件箱补发异常", exc_info=True)
                return 0
            log.warning("以下群未推成功: %s", "/".join(left))
            if attempt == MAX_ATTEMPTS:
                # 三轮都没推出去：入发件箱，通道恢复后补发（10013/断线期不丢播报）
                try:
                    import alert_outbox
                    for name in left:
                        content = md_full if name == "汇总" else (md_by_dept or {}).get(name)
                        if content and webhooks.get(name):
                            alert_outbox.enqueue(
                                f"日报[{slot}]{name}", content, webhooks[name])
                except Exception:
                    log.warning("日报入发件箱异常", exc_info=True)

        if attempt < MAX_ATTEMPTS:
            log.info("%d 分钟后重试…", RETRY_INTERVAL // 60)
            time.sleep(RETRY_INTERVAL)
    log.error("%d 轮后仍未全部推送成功，下个触发时间点再试", MAX_ATTEMPTS)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="投放日报分时段推送")
    ap.add_argument("--slot", choices=sorted(SLOTS), default=DEFAULT_SLOT,
                    help="播报时段：08:00/10am/12:00/14pm/16pm/evening 为当前在投有消耗；night 为当天全量分母")
    ap.add_argument("--dry-run", action="store_true", help="只扫描+打印，不推送")
    ap.add_argument("--force", action="store_true",
                    help="忽略当天该时段已推送标记，重新扫描并重推所有日报群")
    ap.add_argument("--skip-follow-up", action="store_true",
                    help="只重扫并播报，不重复执行14:00/16:00下午跟进操作")
    args = ap.parse_args()
    lock = acquire_report_lock()
    if lock is None:
        log.error("已有日报实例正在执行，本次退出；调用方不得继续执行后续动作")
        return 75
    try:
        return run(args)
    finally:
        release_report_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
