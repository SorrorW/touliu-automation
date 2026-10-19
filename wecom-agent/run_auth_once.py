# -*- coding: utf-8 -*-
"""手动跑一条授权申请（持锁+忙标，与 poll/plan_power 互斥）。用法:
python run_auth_once.py <creator> <department> <code>
"""
import sys, os, subprocess
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "投流")))

from 巨量千川.plan_power import _acquire_action_lock, _release_action_lock, _busy_mark, _busy_clear

creator, department, code = sys.argv[1], sys.argv[2], sys.argv[3]
PY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "投流", "runtime", "python.exe"))
TOULIU = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "投流"))

fd, held = _acquire_action_lock(max_wait_s=900)
_busy_mark("manual-auth-once")
try:
    cmd = [PY, "-X", "utf8", "-m", "巨量千川.auth", "authorize",
           "--creator", creator, "--department", department,
           "--auth-mode", "matrix" if department == "业务1部-1" else "business",
           "--code", code]
    print("RUN:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=TOULIU, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    print("rc=", r.returncode)
    print("--- stdout tail ---")
    print((r.stdout or "")[-1500:])
    print("--- stderr tail ---")
    print((r.stderr or "")[-800:])
finally:
    _busy_clear()
    _release_action_lock(fd, held)
