# -*- coding: utf-8 -*-
"""12:00 串行入口：先播报，再恢复老计划池（2026-09-04 用户定顺序）。

此前 12:00 由两个独立定时任务同时触发（wecom_report_noon 播报 +
wecom_old_pool_enable 恢复老池），并发抢同一个 Edge/数据库。
现合并为单任务串行：
1. 12:00 午间播报（daily_report_push --slot noon）；
2. 老计划池恢复（plan_power resume-old-pool）；
3. 佣金闸（plan_power commission-gate，2026-09-07）：紧接恢复之后
   停掉 >commission_max_pct 的在投计划——恢复放出来的高佣金计划
   同链立刻被停回，阻断名单也保证它们明天不再进恢复队列。

播报是只读动作，恢复是「开通」动作（不是止损）：播报失败不阻断恢复，
否则老池会整天停投。任务被 StartWhenAvailable 重放到 12:20 之后时，
跳过播报（避免白天旧槽位扫描挤占后续档位），只执行恢复+佣金闸。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import msvcrt
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOULIU = HERE.parent / "投流"
PYTHON = TOULIU / "runtime" / "pythonw.exe"
REPORT = HERE / "daily_report_push.py"
LOCK_FILE = HERE / ".noon_report_resume.lock"
LOG_DIR = HERE / "logs"
# 播报通常几分钟；老池恢复遇到批量失效走逐条时可能 30 分钟级（09-03 教训）。
MAX_STEP_SECONDS = 2700
# 与 run_report.ps1 的 20 分钟 grace 对齐：晚于此时刻视为陈旧重放，跳过播报。
REPORT_STALE_AFTER = "12:20"

log = logging.getLogger("noon_report_resume")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(
            LOG_DIR / f"noon_report_resume_{dt.date.today():%Y%m%d}.log", encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(file_handler)
    except OSError:
        pass


def acquire_lock():
    fd = open(LOCK_FILE, "a+b")
    try:
        if fd.seek(0, 2) == 0:
            fd.write(b"0")
            fd.flush()
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        fd.close()
        return None


def release_lock(fd) -> None:
    try:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fd.close()


def run_step(name: str, command: list[str], *, timeout: int = MAX_STEP_SECONDS) -> tuple[int, str, str]:
    """Run one sequential step and retain stdout/stderr for diagnosis."""
    log.info("开始步骤：%s", name)
    try:
        result = subprocess.run(
            command, cwd=HERE, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        log.error("步骤超时：%s", name)
        return -9, out, err or f"{name} 超时"
    log_text = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        log.info("步骤完成：%s%s", name, f" | {log_text[-500:]}" if log_text else "")
    else:
        log.error("步骤失败：%s rc=%s%s", name, result.returncode,
                  f" | {log_text[-800:]}" if log_text else "")
    return result.returncode, result.stdout or "", result.stderr or ""


def run_noon_chain(*, dry_run: bool = False, now_hhmm: str | None = None) -> int:
    """12:00 播报 → 老计划池恢复；播报失败不阻断恢复（恢复是开通动作）。"""
    now_hhmm = now_hhmm or dt.datetime.now().strftime("%H:%M")
    if now_hhmm > REPORT_STALE_AFTER:
        log.warning("当前 %s 已晚于 %s，视为陈旧重放：跳过播报，仅恢复老计划池",
                    now_hhmm, REPORT_STALE_AFTER)
    else:
        report_cmd = [str(PYTHON), str(REPORT), "--slot", "noon"]
        if dry_run:
            report_cmd.append("--dry-run")
        rc, _out, _err = run_step("12:00午间播报", report_cmd)
        if rc != 0:
            log.warning("12:00播报未完成，但老池恢复是开通动作，继续执行")
    if dry_run:
        log.info("dry-run：不执行老池恢复")
        return 0
    rc, _out, _err = run_step("老计划池12:00恢复", [
        str(PYTHON), "-m", "巨量千川.plan_power",
        "resume-old-pool", "--force",
    ])
    if rc != 0:
        log.error("老计划池恢复失败")
        return rc or 1
    # 佣金闸挂链尾（止损动作，--force 不受总开关抑制）：刚恢复的高佣金计划
    # 同链停回，最长暴露半天；阻断名单防次日再恢复（resume 侧已过滤）。
    rc, _out, _err = run_step("佣金闸（>5%存量停投）", [
        str(PYTHON), "-m", "巨量千川.plan_power",
        "commission-gate", "--force",
    ])
    if rc != 0:
        log.error("佣金闸失败")
        return rc or 1
    log.info("12:00链完成：播报→老池恢复→佣金闸")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="12:00先播报再恢复老计划池")
    ap.add_argument("--dry-run", action="store_true", help="只检查播报，不执行恢复")
    args = ap.parse_args()
    fd = acquire_lock()
    if fd is None:
        log.warning("已有12:00链实例运行，本次退出")
        return 0
    try:
        return run_noon_chain(dry_run=args.dry_run)
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
