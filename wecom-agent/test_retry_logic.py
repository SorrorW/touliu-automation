#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：验证模块化断点重试语义（不真实执行浏览器/不写表格）。

覆盖：
  A. 选择逻辑 should_process
     1. 已投放 + 在 processed_ids + --force → 永不重跑（防全盘重试重复扣费）
     2. 待处理 + 在 processed_ids（人工改回）→ 复活：清计数、留断点
     3. 失败 + 无 --force → 不选；失败 + --force → 复活续跑
     4. 待处理 + 授权等待未到点 → 跳过；到点 → 选
  B. 抖音模块化流程（mock 子进程）
     5. 授权申请 ok + check 待达人确认 → wait，授权申请断点=ok
     6. 断点续跑：skip 授权申请，check 已生效 → 直接建计划+追投
     7. check 显示失效 → 自动重提授权 → 生效 → 继续投放
"""
import io
import json
import logging
import os
import sys
import time
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# 便携 runtime 的 python312._pth 不含脚本目录，测试需显式加入本目录。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import poll_and_run as P
import local_tools as L

# 该测试会直接调用轮询模块；移除生产文件日志并禁用状态落盘，避免污染真实运行记录。
for _handler in list(logging.getLogger().handlers):
    if isinstance(_handler, logging.FileHandler):
        logging.getLogger().removeHandler(_handler)
        _handler.close()
P.write_status = lambda **_fields: None

# 测试不依赖真实时钟/全局开关：固定夜间窗口和分类完成门 + 打开追投。
# 生产环境会等待 23:00 分类收口，此处只验证后续模块化流程语义。
_real_in_plan_create_window = L.in_plan_create_window
_real_lifecycle_gate_ready = L.lifecycle_gate_ready
L.in_plan_create_window = lambda now=None: True
L.lifecycle_gate_ready = lambda now=None, gate_file=None: True
L.ENABLE_CHASE = True


def mkrec(rid, prog, **fields):
    values = {"进度": [{"type": "text", "text": prog}]} if prog else {}
    for k, v in fields.items():
        values[k] = [{"type": "text", "text": v}]
    return {"record_id": rid, "values": values}


class StubClient:
    def __init__(self, recs):
        self.cfg = {"sheet_id": "x"}
        self.recs = recs

    def get_all_records(self):
        return self.recs


def run_selection(recs, state, force=False):
    args = types.SimpleNamespace(force=force, dry_run=True)
    state.setdefault("processed_ids", [])
    state.setdefault("attempts", {})
    state.setdefault("steps", {})
    state.setdefault("errors", {})
    state.setdefault("waiting_auth", {})
    sheet = {"sheet_id": "x", "name": "抖音任务",
             "pending": ["", "待处理", "等待达人接受", "可投放"],
             "done": "已投放", "fail": "失败",
             "auth_wait": "等待达人接受", "auth_ok": "可投放"}
    import logging
    handler = logging.StreamHandler(io.StringIO())
    P.log.addHandler(handler)
    P.process_sheet(StubClient(recs), sheet, state, args)
    out = handler.stream.getvalue() if hasattr(handler, "stream") else ""
    P.log.removeHandler(handler)
    return state, out


fails = []
def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        fails.append(name)


# ---- A1: 已投放 + processed + force → 不选 ----
state = {"processed_ids": ["r1"], "attempts": {}, "steps": {}, "errors": {}, "waiting_auth": {}}
recs = [mkrec("r1", "已投放", 抖音号="abc")]
st, out = run_selection(recs, state, force=True)
check("A1 已投放永不重跑(--force也不)", "将处理 r1" not in out and "r1" in st["processed_ids"], out.strip().splitlines()[-1:] or "")

# ---- A2: 待处理 + processed（人工改回）→ 复活留断点 ----
state = {"processed_ids": ["r2"], "attempts": {"r2": 3},
         "steps": {"r2": {"授权申请": "ok", "授权确认": "ok", "建计划": "ok", "追投": "fail"}},
         "errors": {"r2": "x"}, "waiting_auth": {}}
recs = [mkrec("r2", "待处理", 抖音号="abc")]
st, out = run_selection(recs, state, force=False)
check("A2 人工重置→复活", "r2" not in st["processed_ids"], "")
check("A2 复活清计数", "r2" not in st["attempts"], "")
check("A2 复活留断点", st["steps"]["r2"].get("建计划") == "ok", json.dumps(st["steps"], ensure_ascii=False))
check("A2 选中续跑", "将处理 r2" in out and "授权申请/授权确认/建计划" in out, out.strip().splitlines()[-1:] or "")

# ---- A3: 失败记录 ----
state = {"processed_ids": ["r3"], "attempts": {"r3": 3},
         "steps": {"r3": {"授权申请": "ok"}}, "errors": {}, "waiting_auth": {}}
recs = [mkrec("r3", "失败", 抖音号="abc")]
st, out = run_selection(recs, state, force=False)
check("A3 失败无force不选", "将处理 r3" not in out, "")
st, out = run_selection(recs, state, force=True)
check("A3 失败+force复活留断点", "将处理 r3" in out and st["steps"]["r3"].get("授权申请") == "ok", "")

# ---- A4: 授权等待计时 ----
future = time.time() + 900
past = time.time() - 1
state = {"processed_ids": [], "attempts": {}, "errors": {},
         "steps": {"r4": {"授权申请": "ok"}, "r5": {"授权申请": "ok"}},
         "waiting_auth": {"r4": {"since": past, "next_check": future, "note": ""},
                          "r5": {"since": past, "next_check": past, "note": ""}}}
recs = [mkrec("r4", "待处理", 抖音号="a"), mkrec("r5", "待处理", 抖音号="b")]
st, out = run_selection(recs, state, force=False)
check("A4 未到点跳过", "将处理 r4" not in out and "等待授权 1 条" in out, "")
check("A4 到点选中", "将处理 r5" in out, "")

# ---- B: 抖音模块流程（mock _run）----
calls = []
def mock_run_factory(check_jsons):
    def _run(cmd, timeout=None):
        calls.append(" ".join(cmd[2:4]))
        if cmd[2:4] == ["巨量千川.auth", "authorize"]:
            return 0, json.dumps({"success": True}, ensure_ascii=False), ""
        if cmd[2:4] == ["巨量千川.auth", "check"]:
            j = check_jsons.pop(0)
            return (0 if j.get("authorized") else 1), json.dumps(j, ensure_ascii=False), ""
        return 0, "{}", ""  # plan_create / chase_create
    return _run

step_log = []
def cb(name, status, detail=""):
    step_log.append((name, status))

# B5: 待达人确认 → wait
calls.clear(); step_log.clear()
L._run = mock_run_factory([{"authorized": False, "douyin_auth": "授权生效", "toufang_auth": "待达人确认"}])
status, msg = L.process_douyin_task("dou123", code="c1", department="业务3部", step_cb=cb)
check("B5 待确认→wait", status == "wait", msg)
check("B5 授权申请断点ok", ("授权申请", "成功") in step_log and ("授权确认", "等待") in step_log, str(step_log))
check("B5 未建计划", not any("plan_create" in c for c in calls), str(calls))

# B6: 断点续跑 skip 授权申请，check 生效 → 直接投放
calls.clear(); step_log.clear()
L._run = mock_run_factory([{"authorized": True}])
status, msg = L.process_douyin_task("dou123", department="业务3部",
                                    skip_steps=["授权申请"], step_cb=cb)
check("B6 续跑→ok", status == "ok", msg)
check("B6 未重复授权申请", not any("authorize" in c for c in calls), str(calls))
check("B6 已建计划+追投", any("plan_create" in c for c in calls) and any("chase_create" in c for c in calls), str(calls))

# B7: check 失效 → 自动重提 → 生效 → 继续
calls.clear(); step_log.clear()
L._run = mock_run_factory([
    {"authorized": False, "douyin_auth": "授权生效", "toufang_auth": "授权申请失效"},
    {"authorized": True},
])
status, msg = L.process_douyin_task("dou123", code="c1", department="业务3部",
                                    skip_steps=["授权申请"], step_cb=cb)
check("B7 失效自动重提→ok", status == "ok", msg)
check("B7 重提了一次authorize", sum("authorize" in c for c in calls) == 1, str(calls))
check("B7 重提后投放", any("plan_create" in c for c in calls), str(calls))

# ---- A5: 中间状态「等待达人接受」「可投放」也在待处理集合内 ----
state = {"processed_ids": [], "attempts": {}, "errors": {}, "waiting_auth": {},
         "steps": {"r6": {"授权申请": "ok"}, "r7": {"授权申请": "ok", "授权确认": "ok"}}}
recs = [mkrec("r6", "等待达人接受", 抖音号="a"), mkrec("r7", "可投放", 抖音号="b")]
st, out = run_selection(recs, state, force=False)
check("A5 等待达人接受可进入", "将处理 r6" in out and "断点: 授权申请" in out, "")
check("A5 可投放可进入", "将处理 r7" in out and "授权确认" in out, "")

# ---- C: 中间状态回写（mock run_direct/writeback，不真实执行）----
SHEET = {"sheet_id": "x", "name": "抖音任务",
         "pending": ["", "待处理", "等待达人接受", "可投放"],
         "done": "已投放", "fail": "失败",
         "auth_wait": "等待达人接受", "auth_ok": "可投放",
         "material_wait": "待业务员补新素材"}
ARGS = types.SimpleNamespace(force=False, dry_run=False)
orig_run_direct, orig_writeback = P.run_direct, P.writeback
captured = []
P.writeback = lambda client, rid, val: captured.append((rid, val))

# C1: wait → 回写「等待达人接受」+ 等待计时
def blank_state():
    return {"processed_ids": [], "attempts": {}, "steps": {}, "errors": {},
            "waiting_auth": {}, "step_notes": {}, "labels": {}}
st1 = blank_state()
P.run_direct = lambda simple, skip, cb, notes=None: ("wait", "授权确认=等待达人确认")
r = P.process_record(None, SHEET, mkrec("rW", "待处理", 抖音号="a"), st1, ARGS)
check("C1 wait→回写等待达人接受", r == "wait" and captured == [("rW", "等待达人接受")], str(captured))
check("C1 等待计时已记录", st1["waiting_auth"].get("rW", {}).get("next_check", 0) > time.time(), "")
check("C1 不消耗重试次数", "rW" not in st1["attempts"], "")

# C2: 授权生效但建计划失败 → 回写「可投放」
captured.clear()
st2 = blank_state(); st2["steps"]["rF"] = {"授权申请": "ok", "授权确认": "ok"}
P.run_direct = lambda simple, skip, cb, notes=None: ("fail", "建计划=fail:网络抖动")
r = P.process_record(None, SHEET, mkrec("rF", "待处理", 抖音号="a"), st2, ARGS)
check("C2 授权ok+建计划fail→回写可投放", r == "fail" and captured == [("rF", "可投放")], str(captured))
check("C2 计入重试次数", st2["attempts"].get("rF") == 1, "")

# C3: 重试满 3 次 → 回写「失败」
captured.clear()
st3 = blank_state(); st3["attempts"]["rG"] = 2
P.run_direct = lambda simple, skip, cb, notes=None: ("fail", "建计划=fail:还是不行")
r = P.process_record(None, SHEET, mkrec("rG", "待处理", 抖音号="a"), st3, ARGS)
check("C3 满3次→回写失败+终结", r == "fail" and captured == [("rG", "失败")]
      and "rG" in st3["processed_ids"], str(captured))

# C4: 已是「等待达人接受」的 wait → 不重复回写
captured.clear()
st4 = blank_state()
P.run_direct = lambda simple, skip, cb, notes=None: ("wait", "授权确认=等待达人确认")
r = P.process_record(None, SHEET, mkrec("rH", "等待达人接受", 抖音号="a"), st4, ARGS)
check("C4 同状态不重复回写", r == "wait" and captured == [], str(captured))

# C5: fatal → 直接回写「失败」+ 终结（不消耗重试次数、不留等待）
captured.clear()
st5 = blank_state()
P.run_direct = lambda simple, skip, cb, notes=None: ("fatal", "授权申请=不可重试:需认证企业机构号")
r = P.process_record(None, SHEET, mkrec("rZ", "待处理", 抖音号="a"), st5, ARGS)
check("C5 fatal→回写失败+终结", r == "fail" and captured == [("rZ", "失败")]
      and "rZ" in st5["processed_ids"], str(captured))
check("C5 fatal不留断点重试", "rZ" not in st5["attempts"] and "rZ" not in st5["waiting_auth"], "")

# C6: 素材超过72小时是业务等待态：回写专属状态，停止自动重试。
captured.clear()
st6 = blank_state()
P.run_direct = lambda simple, skip, cb, notes=None: (
    "wait", "建计划=等待：待业务员补新素材：当前无发布≤72小时的该商品挂车视频")
r = P.process_record(None, SHEET, mkrec("rM", "可投放", 抖音号="a"), st6, ARGS)
check("C6 素材超72h→回写待补素材", r == "wait" and captured == [("rM", "待业务员补新素材")], str(captured))
check("C6 素材待补不留自动复查", "rM" not in st6["waiting_auth"] and "rM" not in st6["attempts"], str(st6))

P.run_direct, P.writeback = orig_run_direct, orig_writeback

# B8: 授权申请遇「投放授权下拉未同步」→ wait（不算失败、不建计划）
calls.clear(); step_log.clear()
def _run_sync_delay(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    if cmd[2:4] == ["巨量千川.auth", "authorize"]:
        j = {"success": False,
             "message": "投放授权下拉中未找到 ID:tanyuan1109（抖音号授权可能尚未生效或同步延迟）"}
        return 1, json.dumps(j, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_sync_delay
status, msg = L.process_douyin_task("tanyuan1109", code="c1", department="业务3部", step_cb=cb)
check("B8 同步延迟→wait", status == "wait", msg)
check("B8 模块记为等待可续跑", ("授权申请", "等待") in step_log, str(step_log))
check("B8 未建计划", not any("plan_create" in c for c in calls), str(calls))

# B9: 平台业务拒绝（个人号需认证企业机构号）→ fatal，不重试不建计划
calls.clear(); step_log.clear()
def _run_fatal(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    if cmd[2:4] == ["巨量千川.auth", "authorize"]:
        j = {"success": False,
             "message": "添加抖音号失败：「个人抖音号」商业合作授权要求已更新，如个人抖音号不符合要求…认证为【抖音企业机构号】"}
        return 1, json.dumps(j, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_fatal
status, msg = L.process_douyin_task("58021911736", code="c1", department="业务2部", step_cb=cb)
check("B9 业务拒绝→fatal", status == "fatal", msg)
# 2026-09-04 起授权前有旧计划预检（plan_sweep 调用），断言收窄到 authorize 本身不原地重试
check("B9 只调一次authorize(不原地重试)", calls.count("巨量千川.auth authorize") == 1, str(calls))
check("B9 未建计划", not any("plan_create" in c for c in calls), str(calls))

# B10: 授权申请首次超时、原地重试成功 → ok 继续流程（瞬态错误模块内消化）
calls.clear(); step_log.clear()
authorize_calls = {"n": 0}
def _run_flaky(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    if cmd[2:4] == ["巨量千川.auth", "authorize"]:
        authorize_calls["n"] += 1
        if authorize_calls["n"] == 1:
            return 1, "", "playwright._impl._errors.TimeoutError: Page.goto: Timeout 15000ms exceeded."
        return 0, json.dumps({"success": True}, ensure_ascii=False), ""
    if cmd[2:4] == ["巨量千川.auth", "check"]:
        return 0, json.dumps({"authorized": True}, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_flaky
orig_sleep = L.time.sleep
L.time.sleep = lambda s: None  # 测试不等 20s
status, msg = L.process_douyin_task("dou123", code="c1", department="业务3部", step_cb=cb)
L.time.sleep = orig_sleep
check("B10 超时原地重试→ok", status == "ok", msg)
check("B10 authorize调了两次", authorize_calls["n"] == 2, str(calls))
check("B10 重试后完成投放", any("plan_create" in c for c in calls) and any("chase_create" in c for c in calls), str(calls))

# B11: 建计划遇「无推广权限」（勾选禁用）→ wait 回退授权复查（授权确认断点被吊销）
calls.clear(); step_log.clear()
def _run_no_perm(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    if cmd[2:4] == ["巨量千川.plan_create", "--product"]:
        j = {"status": "failed",
             "message": "无法勾选抖音号 58021911736: err:该抖音号对此商品无推广权限（勾选框被平台禁用）"}
        return 1, json.dumps(j, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_no_perm
status, msg = L.process_douyin_task("58021911736", department="业务2部",
                                    skip_steps=["授权申请", "授权确认"], step_cb=cb)
check("B11 无推广权限→wait回退授权", status == "wait", msg)
check("B11 授权确认断点吊销", ("授权确认", "等待") in step_log, str(step_log))
check("B11 未追投", not any("chase_create" in c for c in calls), str(calls))

# B12: 旧计划有数据 → 建计划跳过，追投锁定旧计划 plan_id
calls.clear(); step_log.clear()
def _run_sweep_active(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]) + " " + " ".join(cmd[4:]))
    if cmd[2] == "巨量千川.plan_sweep":
        j = {"success": True, "matched": 1, "chase_plan_id": "999000111",
             "has_active": True, "deleted": [], "material_added": ["0727_老计划_赵"],
             "warnings": []}
        return 0, json.dumps(j, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_sweep_active
status, msg = L.process_douyin_task("dou123", product="肥牛", department="业务3部",
                                    skip_steps=["授权申请", "授权确认"], step_cb=cb)
check("B12 有数据→整体ok", status == "ok", msg)
check("B12 建计划被标记跳过", ("建计划", "成功") in step_log, str(step_log))
check("B12 未调plan_create", not any("plan_create" in c for c in calls), str(calls))
check("B12 追投锁定plan_id", any("chase_create" in c and "--plan-id 999000111" in c for c in calls), str(calls))

# B13: 无旧计划 → 正常建计划 + 按达人追投
calls.clear(); step_log.clear()
def _run_sweep_none(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]) + " " + " ".join(cmd[4:]))
    if cmd[2] == "巨量千川.plan_sweep":
        j = {"success": True, "matched": 0, "chase_plan_id": "", "has_active": False,
             "deleted": [], "material_added": [], "warnings": []}
        return 0, json.dumps(j, ensure_ascii=False), ""
    return 0, "{}", ""
L._run = _run_sweep_none
status, msg = L.process_douyin_task("dou123", product="肥牛", department="业务3部",
                                    skip_steps=["授权申请", "授权确认"], step_cb=cb)
check("B13 无旧计划→ok", status == "ok", msg)
check("B13 正常建计划", any("plan_create" in c for c in calls), str(calls))
check("B13 追投按达人", any("chase_create" in c and "--creator dou123" in c for c in calls), str(calls))

# B14: 断点续跑——旧计划处理/建计划已完成，从 step_notes 恢复追投 plan_id
calls.clear(); step_log.clear()
L._run = _run_sweep_none  # sweep 不应被调到；其余命令返回成功
status, msg = L.process_douyin_task(
    "dou123", product="肥牛", department="业务3部",
    skip_steps=["授权申请", "授权确认", "旧计划处理", "建计划"],
    step_cb=cb,
    step_notes={"旧计划处理": "耗时8s 匹配1条 加素材1条 chase_plan_id=999000111"})
check("B14 续跑→ok", status == "ok", msg)
check("B14 未重跑sweep", not any("plan_sweep" in c for c in calls), str(calls))
check("B14 追投恢复plan_id", any("chase_create" in c and "--plan-id 999000111" in c for c in calls), str(calls))

# B15: 追投遇「已有追投任务」→ 幂等达成（不算失败、不重复扣费）
calls.clear(); step_log.clear()
def _run_chase_exists(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    if cmd[2] == "巨量千川.plan_sweep":
        return 0, json.dumps({"success": True, "matched": 0, "chase_plan_id": "",
                              "deleted": [], "material_added": [], "warnings": []},
                             ensure_ascii=False), ""
    if cmd[2] == "巨量千川.chase_create":
        j = {"status": "failed",
             "message": "该计划已有追投任务，请用 chase_ops.py 管理，或确认当前计划需要新建追投"}
        return 1, json.dumps(j, ensure_ascii=False, indent=2), ""
    return 0, "{}", ""
L._run = _run_chase_exists
status, msg = L.process_douyin_task("dou123", product="肥牛", department="业务3部",
                                    skip_steps=["授权申请", "授权确认"], step_cb=cb)
check("B15 已有追投→幂等ok", status == "ok", msg)
check("B15 追投记成功", ("追投", "成功") in step_log, str(step_log))

# ---- D: 2026-08-18 夜间新规 ----

# D1: 断点重试间隔——失败过的记录间隔内跳过，超间隔才重试；--force/--retry 不受限
state = {"processed_ids": [], "attempts": {"rD1": 1}, "steps": {"rD1": {"授权申请": "ok"}},
         "errors": {}, "waiting_auth": {}, "last_attempt": {"rD1": time.time()}}
recs = [mkrec("rD1", "可投放", 抖音号="a")]
st, out = run_selection(recs, state, force=False)
check("D1 间隔内跳过", "将处理 rD1" not in out and "断点重试间隔" in out, "")
st, out = run_selection(recs, {"processed_ids": [], "attempts": {"rD1": 1},
                               "steps": {"rD1": {}}, "errors": {}, "waiting_auth": {},
                               "last_attempt": {"rD1": time.time() - 7200}}, force=False)
check("D1 超间隔重试", "将处理 rD1" in out, "")
st, out = run_selection(recs, {"processed_ids": [], "attempts": {"rD1": 1},
                               "steps": {"rD1": {}}, "errors": {}, "waiting_auth": {}},
                        force=False)
check("D1 无last_attempt旧状态→直接重试", "将处理 rD1" in out, "")
st, out = run_selection(recs, {"processed_ids": [], "attempts": {"rD1": 1},
                               "steps": {"rD1": {}}, "errors": {}, "waiting_auth": {},
                               "last_attempt": {"rD1": time.time()}}, force=True)
check("D1 --force不受间隔限制", "将处理 rD1" in out, "")

# D2: 窗口函数严格限定建计划时段；轮询/重试固定 15/30 分钟
import datetime as _dt
# 补跑标记文件存在时窗口恒真，会污染时段断言——先移开，测完恢复
_d2_marker = os.path.join(os.path.dirname(os.path.abspath(L.__file__)), "force_plan_create_window")
_d2_marker_bak = None
if os.path.exists(_d2_marker):
    _d2_marker_bak = _d2_marker + ".bak_test"
    os.replace(_d2_marker, _d2_marker_bak)
try:
    check("D2 03:00是夜间", _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 3, 0)), "")
    check("D2 08:00非夜间", not _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 8, 0)), "")
    check("D2 22:00非夜间", not _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 22, 0)), "")
    check("D2 23:00是夜间", _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 23, 0)), "")
    check("D2 23:59是夜间", _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 23, 59)), "")
    # 补跑标记：任意时刻强制窗口开启（夜间收口失败的白天补建用）
    open(_d2_marker, "w", encoding="utf-8").write("test")
    check("D2 标记文件强制窗口", _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 12, 0)), "")
    os.remove(_d2_marker)
    check("D2 标记删除后恢复时段", not _real_in_plan_create_window(_dt.datetime(2026, 8, 18, 12, 0)), "")
finally:
    if _d2_marker_bak and os.path.exists(_d2_marker_bak):
        os.replace(_d2_marker_bak, _d2_marker)
check("D2 固定轮询15分钟", P.round_cadence_seconds() == 900, "")
check("D2 固定重试30分钟", P.retry_interval_seconds() == 1800, "")

# D3: 失败时记录 last_attempt（配合 D1 的间隔门）
captured.clear()
stD3 = blank_state()
_orig_run_direct, _orig_writeback = P.run_direct, P.writeback
P.run_direct = lambda simple, skip, cb, notes=None: ("fail", "建计划=fail:抖一下")
P.writeback = lambda client, rid, val: captured.append((rid, val))
r = P.process_record(None, SHEET, mkrec("rD3", "待处理", 抖音号="a"), stD3, ARGS)
check("D3 失败写入last_attempt",
      r == "fail" and abs(stD3.get("last_attempt", {}).get("rD3", 0) - time.time()) < 5, "")
P.run_direct, P.writeback = _orig_run_direct, _orig_writeback

# D4: 白天建计划窗口拦截——process_douyin_task 直接返回 wait，不建计划
_orig_window = L.in_plan_create_window
_orig_gate = L.lifecycle_gate_ready
L.in_plan_create_window = lambda now=None: False
L.lifecycle_gate_ready = lambda now=None, gate_file=None: False
calls.clear(); step_log.clear()
def _run_noop(cmd, timeout=None):
    calls.append(" ".join(cmd[2:4]))
    return 0, "{}", ""
L._run = _run_noop
status, msg = L.process_douyin_task("dou123", product="肥牛", department="业务3部",
                                    skip_steps=["授权申请", "授权确认"], step_cb=cb)
check("D4 白天建计划→wait", status == "wait" and "等夜间窗口建计划" in msg, msg)
check("D4 白天未调plan_create", not any("plan_create" in c for c in calls), str(calls))
L.in_plan_create_window = _orig_window
L.lifecycle_gate_ready = _orig_gate

# D5: poll main() 整轮节奏门——距上轮不足间隔直接返回（不建 client）
_orig_load_state, _orig_client = P.load_state, P.wecom_client.WeComClient
_orig_argv = sys.argv
sys.argv = ["poll_and_run.py"]
P.load_state = lambda: {"last_round_ts": time.time()}
def _boom_client():
    raise RuntimeError("client created")
P.wecom_client.WeComClient = _boom_client
try:
    P.main()  # 应直接返回，不抛 _boom_client
    _gate_ok = True
except RuntimeError:
    _gate_ok = False
check("D5 间隔内整轮跳过", _gate_ok, "")
P.load_state = lambda: {"last_round_ts": time.time() - 7200}
try:
    P.main()
    _gate_ok = False  # 超间隔应继续走（会去建 client → 抛 RuntimeError 才对）
except RuntimeError:
    _gate_ok = True
except Exception:
    _gate_ok = True   # client 之后的步骤报错也算通过了节奏门
check("D5 超间隔放行", _gate_ok, "")
sys.argv = _orig_argv
P.load_state, P.wecom_client.WeComClient = _orig_load_state, _orig_client

# D6: plan_power 状态记账——record_night_created 去重合并
sys.path.insert(0, str(__import__("pathlib").Path(r"D:\计划投流\投流").resolve()))
import importlib
import tempfile
_pp = importlib.import_module("巨量千川.plan_power")
_orig_state_file = _pp.STATE_FILE
with tempfile.TemporaryDirectory() as _td:
    _pp.STATE_FILE = __import__("pathlib").Path(_td) / "state.json"
    _pp.record_night_created("业务3部", "111", "0818_达人A_黄", paused=True)
    _pp.record_night_created("业务3部", "111", "0818_达人A_黄", paused=True)  # 重复记账
    _pp.record_night_created("业务3部", "222", "0818_达人B_黄", paused=False)
    _st = _pp.load_state()
    check("D6 夜间新建记账去重", len(_st["night_created"]) == 2,
          json.dumps(_st["night_created"], ensure_ascii=False))
    check("D6 paused标志保留", _st["night_created"][0]["paused"] is True
          and _st["night_created"][1]["paused"] is False, "")
_pp.STATE_FILE = _orig_state_file

# D7: plan_create --pause-after 参数
import 巨量千川.plan_create as _pc
_argv_bak = sys.argv
sys.argv = ["plan_create", "--product", "肥牛", "--creator", "dou1",
            "--department", "业务3部", "--pause-after"]
_req = _pc.parse_args()
check("D7 --pause-after解析", _req.pause_after is True, "")
sys.argv = ["plan_create", "--product", "肥牛", "--creator", "dou1", "--department", "业务3部"]
check("D7 默认不暂停", _pc.parse_args().pause_after is False, "")
sys.argv = _argv_bak

# D8: 浏览器互斥——plan_power 持锁时轮询整轮跳过；锁文件 45 分钟超时兜底
_busy_path = os.path.join(P.HERE, "plan_power_busy.json")
try:
    os.remove(_busy_path)
except OSError:
    pass
check("D8 无锁文件→不忙", P.plan_power_busy() is False, "")
with open(_busy_path, "w", encoding="utf-8") as _f:
    json.dump({"action": "close-old-pool", "ts": time.time()}, _f)
check("D8 新锁→忙", P.plan_power_busy() is True, "")
with open(_busy_path, "w", encoding="utf-8") as _f:
    json.dump({"action": "close-old-pool", "ts": time.time() - 7200}, _f)
check("D8 锁超45分钟→失效", P.plan_power_busy() is False, "")
# main() 级：忙时直接返回，不建 client
with open(_busy_path, "w", encoding="utf-8") as _f:
    json.dump({"action": "resume-old-pool", "ts": time.time()}, _f)
_orig_load_state2, _orig_client2 = P.load_state, P.wecom_client.WeComClient
_argv_bak3 = sys.argv
sys.argv = ["poll_and_run.py"]
P.load_state = lambda: {"last_round_ts": 0}  # 绕过 15 分钟节奏门
P.wecom_client.WeComClient = _boom_client
try:
    P.main()
    _skip_ok = True
except RuntimeError:
    _skip_ok = False
check("D8 忙时整轮跳过", _skip_ok, "")
sys.argv = _argv_bak3
P.load_state, P.wecom_client.WeComClient = _orig_load_state2, _orig_client2
os.remove(_busy_path)

L.lifecycle_gate_ready = _real_lifecycle_gate_ready

print()
print("FAILED:", fails if fails else "无 —— 全部通过")
sys.exit(1 if fails else 0)
