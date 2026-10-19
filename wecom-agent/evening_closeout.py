# -*- coding: utf-8 -*-
"""17:00 串行收口：先完成日报，先停老池止血，再调参数。

日报四个群全部成功后，依次执行（2026-09-03 用户定「先停后调」）：
1. 老计划池批量暂停（先止血，别浪费钱）；
2. 新计划 ROI<6 日内暂停；
3. 潜力降级闸（2026-09-01 白板②+订单新规；2026-09-03 起 17:00 档）：
   订单<4 或 ROI<7.3 的在投潜力转日常（10.3/300）并立即暂停，
   挂次日 12:00 恢复；
4. 重点降级闸（白板③）：0-17 累计 ROI<8.5 的重点降潜力 7.3/300 继续投放。

该入口替代 17:00 同时触发的日报、新计划熔断和 17:10 老池暂停任务，
避免多个进程争抢同一个 Edge/数据库。日报失败时不会执行任何暂停动作。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import msvcrt
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOULIU = HERE.parent / "投流"
PYTHON = TOULIU / "runtime" / "pythonw.exe"
REPORT = HERE / "daily_report_push.py"
LOCK_FILE = HERE / ".evening_closeout.lock"
LOG_DIR = HERE / "logs"
# 2026-09-03：老池停批量失效走纯逐条时 114 条约 30 分钟，1800s 被杀在补停
# 中途（业务3部漏停）。提到 45 分钟兜底；批量路径修复后再评估回调。
MAX_STEP_SECONDS = 2700

log = logging.getLogger("evening_closeout")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(
            LOG_DIR / f"evening_closeout_{dt.date.today():%Y%m%d}.log", encoding="utf-8"
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


def run_closeout(*, dry_run: bool = False) -> int:
    """日报→老池停（先止血）→新计划闸→潜力闸→重点闸；前一步失败即停后续。"""
    report_cmd = [str(PYTHON), str(REPORT), "--slot", "evening"]
    if dry_run:
        report_cmd.append("--dry-run")
    rc, _out, _err = run_step("17:00日报（四群推送）", report_cmd)
    if rc != 0:
        log.error("17:00日报未完成，禁止启动任何暂停动作")
        return rc or 1
    if dry_run:
        log.info("dry-run：日报检查完成，不执行暂停动作")
        return 0

    steps = [
        # 2026-09-03 用户定「先停后调」：老池批量停提到最前止血，
        # 再做新计划/潜力/重点的参数调整，避免改写期间老计划继续烧钱。
        ("17:00老计划池批量暂停", [
            str(PYTHON), "-m", "巨量千川.plan_power",
            "pause-old-pool", "--force",
        ]),
        ("17:00新计划ROI<6暂停", [
            str(PYTHON), "-m", "巨量千川.plan_power",
            "pause-new-low-roi", "--threshold", "6", "--force",
        ]),
        ("17:00潜力降级闸(订单<4或ROI<7.3转日常并立即暂停)", [
            str(PYTHON), "-m", "巨量千川.plan_power",
            "pause-potential-low-roi", "--threshold", "7.3",
            "--scan-slot", "17:00", "--force",
        ]),
        # 白板③重点盘中闸（2026-09-03 用户定全建）：0-17 破 8.5 降潜力继续投放
        ("17:00重点降级闸(0-17ROI<8.5降潜力7.3/300继续投放)", [
            str(PYTHON), "-m", "巨量千川.plan_power",
            "demote-key-low-roi", "--threshold", "8.5",
            "--scan-slot", "17:00", "--force",
        ]),
    ]
    for name, command in steps:
        rc, _out, _err = run_step(name, command)
        if rc != 0:
            log.error("步骤未完成，停止后续收口动作：%s", name)
            return rc or 1
    log.info("17:00收口完成：日报→老池停→新计划闸→潜力闸→重点闸")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="17:00日报完成后串行暂停计划")
    ap.add_argument("--dry-run", action="store_true", help="只检查日报，不执行暂停")
    args = ap.parse_args()
    fd = acquire_lock()
    if fd is None:
        log.warning("已有17:00收口实例运行，本次退出")
        return 0
    try:
        return run_closeout(dry_run=args.dry_run)
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
