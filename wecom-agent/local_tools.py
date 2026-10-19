#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_tools.py —— 投流业务动作：企微表格记录 → 调用本机投流脚本。

每个函数 = 一个投流动作，返回 (ok: bool, msg: str)。参数名与表格字段标题对应。
通过 subprocess 调用 D:\\计划投流\\投流 下的投流脚本（-m 巨量千川.xxx / 视频号投流.xxx）。

前置条件：Edge（自动化专用 profile）已登录对应部门千川账号；本机出口 IP 在企微可信 IP 白名单。

步骤日志与断点续跑（poll_and_run 注入）：
  step_cb(name, status, detail)  每个模块 开始/成功/失败/等待 都会回调（落日志+状态文件）
  skip_steps                     上次已成功的模块名，本轮直接跳过（哪个模块出问题只重跑那个模块）

返回约定：所有 process_* 函数返回 (status, msg)，status ∈ "ok" / "fail" / "wait"
  wait = 授权待达人确认：poll_and_run 每 30 分钟复查一次授权状态，
         变成可投放后立即从断点接着建计划/追投；不消耗重试次数。

真实扣费开关（2026-08-13 起全自动开启）：
  ENABLE_PUBLISH = True    # 千川「发布计划」→ 建成即上线，真实消耗日预算
  ENABLE_CHASE   = False   # 2026-08-17 起追投暂停（等新追投标准），记录照常完成
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime as _dt, timedelta as _timedelta


def in_plan_create_window(now: _dt | None = None) -> bool:
    """建计划窗口：每天 23:00-次日 07:59。

    其余时间轮询只处理授权和旧计划识别，不建新计划、不加素材。
    夜间新建的计划建成后保持投放中，不执行午夜暂停或早八恢复。

    手动补跑口子：同目录存在 force_plan_create_window 标记文件时视为窗口内
    （用于夜间收口失败后的白天补建，2026-08-22 首次使用；用完删标记文件）。
    """
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "force_plan_create_window")
    if os.path.exists(marker):
        return True
    now = now or _dt.now()
    return now.hour >= 23 or now.hour < 8


def _night_start_date(now: _dt) -> str:
    day = now.date() if now.hour >= 23 else now.date() - _timedelta(days=1)
    return day.isoformat()


def lifecycle_gate_ready(now: _dt | None = None, gate_file: str | None = None) -> bool:
    """仅当本夜 23:00 分类收口完成时，允许建计划或新增素材。

    手动补跑口子：force_plan_create_window 标记文件存在时视为闸门就绪
    （2026-10-06 实证：假期停机数日后门文件日期过期，白天补建被
    night_start_date 不匹配卡死；标记文件本就是人工负责的补跑声明，
    分类门一并放行；用完删标记文件）。"""
    now = now or _dt.now()
    if not in_plan_create_window(now):
        return False
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "force_plan_create_window")
    if os.path.exists(marker):
        return True
    path = gate_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "nightly_lifecycle_gate.json")
    try:
        with open(path, encoding="utf-8") as handle:
            gate = json.load(handle)
        return bool(gate.get("classification_ready")) and gate.get("night_start_date") == _night_start_date(now)
    except (OSError, ValueError, TypeError):
        return False


def delivery_window_open(scheduled_start: str = "", now: _dt | None = None) -> bool:
    """预约日期按「前一晚 23:00 创建、预约日 08:00 起投」解释。


    例如备注 ``8/26投``：允许在 8/25 23:00 后建计划，计划名称使用 0826，
    建成后暂停并排入 8/26 08:00 恢复；8/26 白天不再新建。
    """
    now = now or _dt.now()
    # 2026-10-15 用户令「确保今天晚上一个计划都没有创建」：同目录存在
    # no_create_YYYYMMDD（当天日期）标记时禁建；夜间窗口跨天（23:00-次日08:00），
    # 凌晨 8 点前还要查昨天的标记，跨天自动失效。
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, f"no_create_{now.strftime('%Y%m%d')}")):
        return False
    if now.hour < 8:
        yesterday = (now - _timedelta(days=1)).strftime("%Y%m%d")
        if os.path.exists(os.path.join(here, f"no_create_{yesterday}")):
            return False
    if not lifecycle_gate_ready(now):
        return False
    if not scheduled_start:
        return True
    try:
        target = _dt.strptime(scheduled_start, "%Y-%m-%d").date()
    except ValueError:
        return False
    # 23:00-次日 07:59 这一窗口服务于“次日”投放日期；历史日期视为逾期补跑。
    delivery_date = now.date() + _timedelta(days=1) if now.hour >= 23 else now.date()
    return target <= delivery_date

def _resume_date_for_poll(scheduled_start: str = "", now: _dt | None = None) -> str:
    """返回本轮计划的实际起投日；该值同时作为计划名前缀和恢复队列日期。

    有预约日期时以预约日期为准；没有预约日期时，夜间创建归入次日 08:00，
    凌晨创建归入当天 08:00。不要用计划创建当天作为名称日期。
    """
    now = now or _dt.now()
    # 真实创建/起投窗口日期是名称日期的下限。备注里的预约日可能已经
    # 过期（例如“8.27投”在 8/28 才实际建成），不能再生成过期的 0827；
    # 这类计划应使用实际投放窗口日期 0828。
    actual_window_day = now.date() + _timedelta(days=1) if now.hour >= 23 else now.date()
    if scheduled_start:
        try:
            requested_day = _dt.strptime(str(scheduled_start), "%Y-%m-%d").date()
            return max(requested_day, actual_window_day).isoformat()
        except ValueError:
            pass
    return actual_window_day.isoformat()

# 投流目录与解释器自动定位：
#   便携包布局（wecom-agent 与 投流 同级，投流\runtime\python.exe 存在）→ 内置 runtime
#   开发布局（d:\计划投流）→ 投流\.venv
_HERE = os.path.dirname(os.path.abspath(__file__))
_pkg_touliu = os.path.normpath(os.path.join(_HERE, "..", "投流"))
if os.path.isfile(os.path.join(_pkg_touliu, "runtime", "python.exe")):
    TOULIU_DIR = _pkg_touliu
    PYTHON = os.path.join(TOULIU_DIR, "runtime", "python.exe")
else:
    TOULIU_DIR = r"D:\计划投流\投流"
    PYTHON = os.path.join(TOULIU_DIR, ".venv", "Scripts", "python.exe")

ENABLE_PUBLISH = True
ENABLE_EXISTING_PLAN_MATERIALS = True
# 2026-08-17 用户指示：建计划后的追投暂停（追投将出新标准），
# 置 False 后模块5 记「追投=暂停」直接完成，记录照常转已投放；新标准落地时改回 True。
ENABLE_CHASE = False
# 自动增长不创建达标追投，只保留 ROI 达标计划的预算不限调整。
ENABLE_QUALIFIED_CHASE = False

def automated_growth_enabled() -> bool:
    """自动追投和自动放开预算的独立开关，默认关闭以避免扫描产生扣费操作。"""
    flag_file = os.path.join(_HERE, "automation.json")
    try:
        with open(flag_file, encoding="utf-8") as handle:
            return bool(json.load(handle).get("growth_enabled", False))
    except Exception:
        return False


STEP_TIMEOUT = 1200  # 单步浏览器操作上限（秒），超时杀进程并记失败（下轮可重试）


def _run(cmd, timeout=STEP_TIMEOUT):
    """在投流目录用 venv python 跑子进程，绕过代理。返回 (returncode, stdout, stderr)。

    超时：subprocess.run 会先杀子进程再抛 TimeoutExpired，这里转成 rc=-9 的失败结果，
    避免浏览器侧残留半个操作无人知晓。
    """
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    try:
        # CREATE_NO_WINDOW：子进程是控制台版 python.exe，不给它分配黑窗口，
        # 否则定时任务每跑一步用户屏幕就弹一下 cmd（2026-08-17 用户反馈）
        r = subprocess.run(cmd, cwd=TOULIU_DIR, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -9, "", f"执行超时（{timeout}s），子进程已终止"


def _tail_json(stdout):
    """从子进程输出提取最后一个 JSON 对象（兼容单行紧凑与 indent 多行格式）。"""
    lines = (stdout or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except Exception:
                continue
    return None


def _tail_text(text, limit=300):
    """错误详情：首部异常行 + 末尾输出片段。

    playwright 报错的异常类型在第一行（如 TargetClosedError），调用日志在末尾，
    只取末尾会丢掉异常名导致瞬态误判，所以首尾都保留。
    输出是多行 JSON（indent=2）时优先提取 message 字段——首尾截取会把
    真正的错误信息拦腰截断（追投超时那次日志只剩 "{" 就是教训）。
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return ""
    if lines[0].startswith("{"):
        j = _tail_json(text)
        if isinstance(j, dict) and j.get("message"):
            return str(j["message"])[:limit]
    if len(lines) <= 3:
        return " | ".join(lines)[:limit]
    return (lines[0] + " | " + " | ".join(lines[-2:]))[:limit]


def _clean_token(s: str) -> str:
    """清理表格里带出的脏值：去空白、去首尾分隔符；多值时取第一个。

    表格里出现过 "67743018793, " 这种值（复制时带了逗号），原样传给授权模块
    会搜 "ID:67743018793," 永远匹配不到。
    """
    if not s:
        return ""
    value = str(s).strip().lstrip(",，;；| ")
    first = re.split(r"[,，]", value, maxsplit=1)[0]
    return first.strip().strip(",， ").strip()


# ---------------------------------------------------------------------------
# 业务员姓名解析（2026-08-18 新规）
# 业务员列/备注栏会写「张丽伟 8/18投」这类「姓名 + 日期要求」，
# 支持 空格 / ， 、 | 等分隔。建计划只取姓名（首字作计划名姓）；
# 日期指令/纯数字绝不能当姓名——2026-08-17 曾把 "8/18投" 的 "8" 当姓建计划。
# ---------------------------------------------------------------------------
_NAME_SPLIT = re.compile(r"[\s，、|,;；]+|(?<!\d)/(?!\d)")
_DATE_TOKEN = re.compile(r"^\d{1,2}\s*[./月]\s*\d{1,2}\s*日?\s*(开?投|投放)?$")
# 常见姓氏白名单：姓名首字必须命中，防把普通词语（如"重点客户"）误当姓名
_SURNAMES = set(
    "张王李赵刘陈杨黄周吴徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾田董潘袁"
    "蔡蒋余杜叶程苏魏吕丁任卢姚沈钟姜崔谭陆汪范金石廖贾夏韦付方白邹孟熊秦"
    "邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤肖"
)


def extract_person_name(text: str) -> str:
    """从「张丽伟 8/18投」这类文本提取姓名（2-4 个汉字、首字为常见姓氏）。

    日期指令（8/18投、8.13开投、8月18日投）、纯数字、非中文词一律不算姓名，
    取不到返回空串。
    """
    for tok in _NAME_SPLIT.split(str(text or "")):
        tok = tok.strip()
        if not tok or _DATE_TOKEN.match(tok):
            continue
        if re.fullmatch(r"[一-鿿]{2,4}", tok) and tok[0] in _SURNAMES:
            return tok
    return ""


def resolve_seller(seller_text: str, remark: str) -> tuple[str, str]:
    """汇总业务员列 + 备注栏，返回 (业务员姓名, 拦截原因)。拦截原因非空 = 等人工修正。

    规则（2026-08-18 用户原话）：
      备注带姓名就取姓名（优先于业务员列）；
      纯备注（取不出姓名）→ 拦截提示等人工修正；
      两栏都只写了日期/没写姓名 → 同样拦截（杜绝数字当姓名）；
      两栏都空 → 沿用旧默认「自动」。
    """
    seller_text = str(seller_text or "").strip()
    remark = str(remark or "").strip()
    name = extract_person_name(remark) or extract_person_name(seller_text)
    if name:
        return name, ""
    residual = " ".join(
        t for t in _NAME_SPLIT.split(remark)
        if t.strip() and not _DATE_TOKEN.match(t.strip()))
    if residual:
        return "", (f"备注「{remark}」里识别不出业务员姓名，请按「姓名 + 日期」格式修正"
                    f"（如：张丽伟 8/18投），修正后点「立即重试」或把进度改回「待处理」")
    if seller_text or remark:
        return "", ("业务员/备注只写了日期、没有姓名，请在备注栏补上业务员姓名"
                    "（如：张丽伟 8/18投），修正后点「立即重试」或把进度改回「待处理」")
    return "自动", ""


def _normalize_date(s: str, default_year: int = 2026):
    """松散日期（'8月12号'/'8.12'/'8/11'/'8..4'）→ YYYY-MM-DD，失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})月(\d{1,2})", s)
    if m:
        return f"{default_year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})[./](\d{1,2})", s)
    if m:
        return f"{default_year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _sync_poll_plan_catalog(
    plan: dict, *, department: str = "", creator_name: str = ""
) -> str:
    """把企微轮询产生的计划事件同步到 webui.db 的 plan_catalog。

    返回空串表示成功；返回错误摘要只作为运行日志告警，不让已在平台成功
    创建的计划被重复重试。数据库函数本身会对部分字段做增量合并。
    """
    try:
        from webui import db as _plan_db

        _plan_db.init_db()
        ok = _plan_db.upsert_plan_from_poll(
            plan,
            department=department,
            creator_name=creator_name,
            source="wecom_poll",
        )
        return "" if ok else "计划库同步失败：缺少 plan_id"
    except Exception as exc:
        return f"计划库同步失败：{str(exc)[:180]}"


def _mark_poll_plan_deleted(
    *, plan_id: str = "", plan_name: str = "", department: str = ""
) -> str:
    """同步企微轮询删除动作，保留计划历史但更新当前状态。"""
    try:
        from webui import db as _plan_db

        _plan_db.init_db()
        ok = _plan_db.mark_plan_deleted_from_poll(
            plan_id=plan_id, plan_name=plan_name, department=department
        )
        return "" if ok else "计划库未找到待标记的计划"
    except Exception as exc:
        return f"计划库删除状态同步失败：{str(exc)[:180]}"


def _exec_step(name, cmd, step_cb, retries=0, retry_delay=20):
    """执行一个步骤：回调开始/成功/失败（带耗时与输出尾部）。返回 (ok, detail)。

    retries>0 时，遇瞬态错误（超时/网络）在本模块内原地重试 —— 只用于幂等模块
    （授权申请/授权重提），建计划/追投非幂等禁止原地重试（防重复扣费）。
    """
    attempt = 0
    while True:
        if step_cb:
            step_cb(name, "开始" if attempt == 0 else f"开始(第{attempt + 1}次)")
        t0 = time.monotonic()
        rc, out, err = _run(cmd)
        elapsed = time.monotonic() - t0
        ok = rc == 0
        detail = _tail_text(out if ok else (err or out))
        if ok:
            if step_cb:
                step_cb(name, "成功", f"耗时{elapsed:.0f}s" + (f" {detail}" if detail else ""))
            return True, detail
        if attempt >= retries or not _is_transient(detail):
            if step_cb:
                step_cb(name, "失败", f"耗时{elapsed:.0f}s rc={rc}" + (f" {detail}" if detail else ""))
            return False, detail
        attempt += 1
        if step_cb:
            step_cb(name, "开始", f"瞬态错误，{retry_delay}s 后本模块原地重试: {detail[:120]}")
        time.sleep(retry_delay)


# 授权状态关键词（与 巨量千川.auth 的 _KNOWN_STATUSES / check 输出对应）
_AUTH_REAPPLY = ("未授权", "未申请", "授权申请失效", "申请失效", "授权失效", "已解除", "已过期")
# 授权申请失败的特殊情形：抖音号授权已提交、投放授权下拉尚未同步（分钟级），
# 属「授权等待」而非失败 —— 走等待复查机制，不消耗重试次数
_AUTH_SYNC_DELAY = ("下拉中未找到", "同步延迟")
# 授权重提接口可能返回 rc=1，但正文明确表示仍在等达人确认。
# 这种情况是正常等待，不应进入「失败次数」计数，更不能把表格改成失败。
_AUTH_PENDING = ("待达人确认", "待确认", "24h 内确认", "24小时内确认")
# 平台明确的业务拒绝：重试无意义，直接判「不可重试」回写失败
# （如个人号新政需认证企业机构号、合作码错误）
_AUTH_FATAL = ("企业机构号", "不符合要求", "合作码错误", "合作码不正确", "合作码不一致",
               # 个人号涨粉新规的另一措辞（09-10 实证 ruPtoX）：「涨粉数未满足要求」
               # 与「不符合要求」同属业务终态——达人须认证企业机构号，重试无意义
               "涨粉数未满足",
               # 记录创建与所选视频均超 48 小时：不符合「48 小时内新创视频」认定（2026-08-23 策略）
               "超48小时",
               # 佣金率超上限（2026-09-07 佣金闸）：建计划选达人时拦截，业务终态不重试
               "佣金过高",
               # 同上兜底：traceback 走 stderr 时中文可能被双重编码毁掉，
               # 但异常类名是 ASCII 永远幸存（2026-09-10 rawj88/rMlJdc 实测漏判空转）
               "CommissionBlockedError")
# 建计划时勾选框被禁用（无推广权限/无权限可申请）= 商品投放授权未完全生效：
# 不是终结错误 —— 回退到授权复查，等权限生效后再建
_PERM_BLOCKED = ("无推广权限", "勾选框被平台禁用")
# 瞬态错误（浏览器导航超时/CDP 断连/网络抖动）：幂等模块内可原地重试一次
# 「Call log: ... navigating to」是 playwright 导航超时的尾巴，
# _tail_text 截取后 Timeout 关键词常被截掉，用这个补识别（2026-08-17 r2pOjd 案例）
_TRANSIENT = ("Timeout", "net::ERR", "Target closed", "TargetClosed", "has been closed",
              "navigating to")


def _is_sync_delay(detail: str) -> bool:
    return any(k in (detail or "") for k in _AUTH_SYNC_DELAY)


def _is_auth_pending(detail: str) -> bool:
    """授权申请接口虽返回失败码，但明确表示等待达人确认。"""
    return any(k in (detail or "") for k in _AUTH_PENDING)


def _is_fatal(detail: str) -> bool:
    return any(k in (detail or "") for k in _AUTH_FATAL)


def _is_transient(detail: str) -> bool:
    return any(k in (detail or "") for k in _TRANSIENT)


def _auth_check(creator, department, retries=2, retry_delay=15):
    """只读查询授权是否已生效（两项授权都生效才算可投放）。

    返回 (status, detail)：status ∈ "ok" / "wait" / "reapply" / "fail"
      wait    = 已提交申请，待达人确认（或平台同步中）
      reapply = 申请失效/从未申请，需要重新提交授权申请
      fail    = 查询本身异常（浏览器/网络问题），按失败模块下轮重试
    只读操作幂等：瞬态错误原地重试两次再定性。
    """
    cmd = [PYTHON, "-m", "巨量千川.auth", "check",
           "--creator", creator, "--department", department]
    for attempt in range(1 + retries):
        rc, out, err = _run(cmd)
        j = _tail_json(out)
        if j is not None:
            break
        detail = _tail_text(err or out)
        if attempt >= retries or not _is_transient(detail):
            return "fail", f"授权状态查询异常 rc={rc} {detail}"
        time.sleep(retry_delay)
    if j.get("authorized"):
        return "ok", "两项授权均已生效"
    statuses = f"{j.get('douyin_auth', '')}/{j.get('toufang_auth', '')}"
    if any(k in statuses for k in _AUTH_REAPPLY):
        return "reapply", statuses
    return "wait", statuses or j.get("message") or "授权未完成"


def preauth_gate_check(creator: str, product: str, department: str):
    from 巨量千川.main_products import product_route, clean_id
    from main_account_flow import Workflow
    from 巨量千川.commission_gate import is_commission_blocked, load_commission_max_pct
    route=product_route(product)
    if not route:
        return "manual", "非四主品商品，待人工处理", ""
    try:
        workflow=Workflow()
        hit=workflow.hit(clean_id(creator),route,refresh=True)
        if hit:
            return "pulled", f"主号「{route.name}」对应商品已绑定", hit.get("creator_name", "")
        probe=workflow.gateway.probe(clean_id(creator),route)
        if is_commission_blocked(probe.get("commission_pct"),load_commission_max_pct()):
            return "commission", "佣金过高", ""
        return "clear", "主号合作达人授权可继续（不限时段）", ""
    except Exception as exc:
        return "retry", str(exc)[:300], ""


def _main_pull_fresh_hit(creator: str, product: str = "") -> str | None:
    from 巨量千川.main_products import product_route, clean_id
    from main_account_flow import Workflow
    route=product_route(product)
    if not route:
        raise ValueError("缺少有效主品商品ID，禁止按达人单独判定")
    workflow=Workflow()
    hit=workflow.hit(clean_id(creator),route,refresh=True)
    return (f"主号「{route.name}」已有对应商品有效绑定" if hit else None)


def process_douyin_task(creator: str, code: str = "", product: str = "",
                        seller: str = "", department: str = "", remark: str = "",
                        scheduled_start: str = "", created_at: str = "",
                        skip_steps=(), step_cb=None,
                        step_notes=None, roi: float = 0, plan_label: str = "", **_kw):
    """手动/离线入口同样只走主号四主品，旧分部断点不作成功凭据。"""
    from main_account_flow import Workflow, ROLLOUT_FILE, browser_guard, BOUND
    from datetime import date
    try:
        rollout=json.loads(ROLLOUT_FILE.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        rollout={'phase':'preview'}
    rid=str(_kw.get('source_record_id') or '')
    if rollout.get('phase')!='active' and not (rollout.get('phase')=='canary' and rid in rollout.get('canary_record_ids',[])):
        return 'wait','[主号进度=待主号授权] 当前为预览/单条验证阶段，尚未放开该记录'
    simple={'抖音号':creator,'合作码':code,'商品ID':product,'业务部':department,'业务员':seller,'备注':remark}
    scheduled=None
    if scheduled_start:
        try:scheduled=date.fromisoformat(scheduled_start[:10])
        except ValueError:return 'fatal','[主号进度=待人工处理] 预约日期无法识别'
    try:
        # Direct callers have no poll browser owner; use exactly the same guard as preview.
        with browser_guard():
            outcome=Workflow().execute(simple,scheduled)
    except Exception as exc:
        return 'wait','[主号进度=待主号授权] '+str(exc)[:500]
    progress=outcome['progress']
    status='ok' if progress in BOUND else ('fatal' if progress in ('待人工处理','佣金过高') else 'wait')
    if step_cb:step_cb(outcome['step'],'成功' if status=='ok' else '等待',outcome['detail'])
    return status,f"[主号进度={progress}] "+outcome['detail']


def _process_douyin_task_department_legacy(creator: str, code: str = "", product: str = "",
                        seller: str = "", department: str = "", remark: str = "",
                        scheduled_start: str = "", created_at: str = "",
                        skip_steps=(), step_cb=None,
                        step_notes=None, roi: float = 0, plan_label: str = "", **_kw):
    """抖音投流模块化流程：授权申请 → 授权确认 → 旧计划处理 → 建计划(发布) → 追投(100元/24h)。

    返回 (status, msg)，status ∈ "ok" / "fail" / "wait"：
      wait = 授权待达人确认，poll_and_run 每 30 分钟复查，生效后从断点继续投放。
    skip_steps 里列出的模块名视为已完成、直接跳过（断点续跑，不重复建计划/扣费）。
    step_notes: {模块名: 成功时的细节}，断点续跑时恢复跨模块信息（如追投目标 plan_id）。
    seller/remark：业务员列与备注栏原文；姓名解析见 resolve_seller（2026-08-18 新规），
      取不到姓名直接 fatal 拦截，等人工修正。
    """
    creator = _clean_token(creator)
    code = _clean_token(code)
    product = _clean_token(product)
    seller, blocked = resolve_seller(seller, remark)
    if blocked:
        return "fatal", f"[拦截] {blocked}"
    if not creator:
        return "fail", "缺少抖音号"
    if not department:
        return "fail", "缺少业务部"

    # 计划名和暂停恢复队列共用同一个“实际起投日”。
    resume_date = _resume_date_for_poll(scheduled_start)
    # 计划名和暂停恢复队列共用同一个“实际起投日”。
    delivery_open = delivery_window_open(scheduled_start)
    resume_date = _resume_date_for_poll(scheduled_start)
    if scheduled_start and not delivery_open:
        delivery_note = f"预约 {scheduled_start} 08:00 起投，等待该日期前一晚 23:00-次日07:59 创建"
    elif scheduled_start:
        delivery_note = f"预约 {scheduled_start} 08:00 起投，当前窗口允许创建"
    else:
        delivery_note = "等待 23:00 分类收口完成后，23:00-次日07:59 执行"

    done = set(skip_steps)
    if "授权" in done:  # 兼容旧版断点名（一个「授权」步骤涵盖申请+确认）
        done.update(("授权申请", "授权确认"))
    steps = []

    # 2026-10-14 mxy557856 事故防线：进任何模块前先复核主号拉取。
    # 票挂起期间主号可能已拉取（授权会顶掉主号自动拉取、主号投放失效）——
    # 授权前最后一道闸，命中即终止，绝不发授权。复核异常同样不放行。
    if "授权申请" not in done:
        try:
            _pulled = _main_pull_fresh_hit(creator, product)
        except Exception as exc:
            return "wait", f"主号拉取复核异常（{str(exc)[:80]}），为确保不误授权挂起重试"
        if _pulled:
            return "pulled", _pulled
        steps.append("主号拉取复核=未拉取")

    # 模块0 旧计划预检（2026-09-04 用户定「先查老计划」）：
    # 授权排在最前会把「已有老计划」的记录卡在授权门口——r033Yl 案例：
    # 该达人已有在投老计划，但授权被平台个人号新规拒，记录直接标死，
    # 永远走不到排第三的旧计划检查。预检为纯只读搜索（不带 --apply，
    # 白天夜间都不动素材）：
    #   有老计划 → 授权申请/确认整段绕过（老计划在投即证明授权链路通），
    #              原模块3 照常执行（夜间窗口仍会加素材/改日期前缀）；
    #   没有     → 照旧授权→建计划，行为不变（模块3 会再搜一次，可接受）；
    #   预检失败 → 不阻塞，照旧走原流程。
    if "旧计划处理" not in done and "授权申请" not in done:
        if step_cb:
            step_cb("旧计划预检", "开始")
        cmd = [PYTHON, "-m", "巨量千川.plan_sweep",
               "--creator", creator,
               "--product", product or "肥牛",
               "--department", department,
               "--days", "30"]  # 30 天窗：停了 7 天以上的老计划也要认（2026-10-09 盲区）
        rc, out, err = _run(cmd)
        j = _tail_json(out)
        if j is None:
            steps.append(f"旧计划预检=失败（{_tail_text(err or out)[:80]}），按原流程继续")
        else:
            prescan_matched = int(j.get("matched") or 0)
            prescan_chase = j.get("chase_plan_id") or None
            for scanned_plan in j.get("plans") or []:
                if isinstance(scanned_plan, dict):
                    _sync_poll_plan_catalog(
                        scanned_plan, department=department, creator_name=creator
                    )
            steps.append(f"旧计划预检=匹配{prescan_matched}条")
            if prescan_chase or prescan_matched > 0:
                done.update(("授权申请", "授权确认"))
                steps.append("授权=跳过（已有老计划，授权链路本就有效）")
        if step_cb:
            step_cb("旧计划预检", "完成", steps[-1])

    # 模块1 授权申请（已提交/已生效的平台侧自动跳过；失效的由模块2触发重提）
    if "授权申请" in done:
        steps.append("授权申请=跳过(已完成)")
    else:
        # 托管型前置（2026-10-08 用户令）：建计划抽屉里「投放即生效/已开启托管
        # 授权」的达人（联盟合作托管型）——白天不授权不建计划，票挂「等待21点进行授权」
        # 留在队列，21:00 后自动续跑授权（无需人工操作）；21 点后本探测直接跳过
        # 走正常授权。矩阵账号（自运营）与缺商品ID的票不适用；探测失败放行。
        if department != "业务1部-1" and product and _dt.now().hour < 21:
            cmd_probe = [PYTHON, "-m", "巨量千川.creator_instant_status",
                         "--creator", creator, "--product", product,
                         "--department", department]
            rc_p, out_p, _err_p = _run(cmd_probe, timeout=300)
            pj = _tail_json(out_p) if rc_p == 0 else None
            if pj is None:
                # 探测失败绝不放行授权（2026-10-08 用户令「确保不能误授权」）：
                # 宁可本轮挂起下轮重探，也不能在识别不了托管型时贸然提交授权。
                # 不会永久停摆——21:00 后探测整体跳过、授权照常进行。
                note = "托管探测失败，为确保不误授权本轮挂起，下轮重试"
                if step_cb:
                    step_cb("授权申请", "等待", note)
                steps.append(f"授权申请={note}")
                return "wait", "；".join(steps)
            if pj.get("managed"):
                note = f"托管型达人（{pj.get('status_text', '投放即生效')}），授权与建计划推迟到21点后，等待21点进行授权"
                if step_cb:
                    step_cb("授权申请", "等待", note)
                steps.append(f"授权申请={note}")
                return "wait", "；".join(steps)
        cmd = [PYTHON, "-m", "巨量千川.auth", "authorize",
               "--creator", creator, "--department", department,
               "--auth-mode", "matrix" if department == "业务1部-1" else "business"]
        if code:
            cmd += ["--code", code]
        ok1, detail = _exec_step("授权申请", cmd, step_cb, retries=1)
        if not ok1:
            if _is_sync_delay(detail):
                # 抖音号授权已提交、投放授权下拉未同步 → 等待复查，不算失败
                if step_cb:
                    step_cb("授权申请", "等待", f"平台同步延迟：{detail}")
                steps.append("授权申请=等待平台同步")
                return "wait", "；".join(steps)
            if _is_auth_pending(detail):
                # 投放授权提交被平台置灰（抖音号授权待达人确认的前置保护，
                # 2026-09-26 乘方实证）→ 等达人确认，不算失败
                if step_cb:
                    step_cb("授权申请", "等待", f"等待达人确认：{detail}")
                steps.append("授权申请=等待达人确认")
                return "wait", "；".join(steps)
            if _is_fatal(detail):
                # 平台业务拒绝（如个人号需认证企业机构号）：重试无意义，直接终结
                steps.append(f"授权申请=不可重试:{detail}")
                return "fatal", "；".join(steps)
            steps.append(f"授权申请=fail:{detail}")
            return "fail", "；".join(steps)
        steps.append("授权申请=ok")

    # 模块2 授权确认（check 只读查询；待达人确认 → wait，半小时后复查，不算失败）
    if "授权确认" in done:
        steps.append("授权确认=跳过(已完成)")
    else:
        if step_cb:
            step_cb("授权确认", "开始")
        t0 = time.monotonic()
        st, detail = _auth_check(creator, department)
        if st == "reapply":
            # 申请失效/未申请：重提一次授权申请（幂等），再查一次
            if step_cb:
                step_cb("授权确认", "开始", f"授权状态[{detail}]，重新提交授权申请")
            cmd = [PYTHON, "-m", "巨量千川.auth", "authorize",
                   "--creator", creator, "--department", department,
                   "--auth-mode", "matrix" if department == "业务1部-1" else "business"]
            if code:
                cmd += ["--code", code]
            ok_r, detail_r = _exec_step("授权重提", cmd, step_cb, retries=1)
            if not ok_r:
                # 授权重提 rc=1 但正文是「待达人确认/投放授权已提交」时，
                # 平台只是拒绝重复提交或尚未完成确认，业务语义仍是等待。
                if _is_auth_pending(detail_r):
                    if step_cb:
                        step_cb("授权重提", "等待", f"等待达人确认：{detail_r}")
                    steps.append("授权重提=等待达人确认")
                    return "wait", "；".join(steps)
                if _is_sync_delay(detail_r):
                    if step_cb:
                        step_cb("授权重提", "等待", f"平台同步延迟：{detail_r}")
                    steps.append("授权重提=等待平台同步")
                    return "wait", "；".join(steps)
                if _is_fatal(detail_r):
                    steps.append(f"授权重提=不可重试:{detail_r}")
                    return "fatal", "；".join(steps)
                steps.append(f"授权重提=fail:{detail_r}")
                return "fail", "；".join(steps)
            steps.append("授权重提=ok")
            st, detail = _auth_check(creator, department)
        elapsed = time.monotonic() - t0
        if st == "ok":
            if step_cb:
                step_cb("授权确认", "成功", f"耗时{elapsed:.0f}s {detail}")
            steps.append("授权确认=ok")
        elif st == "wait":
            if step_cb:
                step_cb("授权确认", "等待", detail)
            steps.append(f"授权确认=等待达人确认({detail})")
            return "wait", "；".join(steps)
        else:
            if step_cb:
                step_cb("授权确认", "失败", detail)
            steps.append(f"授权确认=fail:{detail}")
            return "fail", "；".join(steps)

    # 模块3 旧计划检查。白天仅识别已有计划；夜间窗口才允许新增素材。
    chase_plan_id = None
    adopted_new_plan_id = ""    # 收养的已暂停老计划 plan_id（挂 08:00 新计划票）
    adopted_new_plan_name = ""  # 收养后改名的计划名（带投放日期前缀）
    old_plan_matched: int | None = None  # 匹配旧计划条数；0=没投过（48h 门豁免依据）
    if "旧计划处理" in done:
        steps.append("旧计划处理=跳过(已完成)")
        m = re.search(r"chase_plan_id=(\d+)", (step_notes or {}).get("旧计划处理", ""))
        if m:
            chase_plan_id = m.group(1)
        # 断点续跑恢复收养老计划标识（供「暂停待发」挂 08:00 票判定）
        m_ad = re.search(r"adopted_new_plan_id=(\d+)", (step_notes or {}).get("旧计划处理", ""))
        if m_ad:
            adopted_new_plan_id = m_ad.group(1)
        m_adn = re.search(r"adopted_new_plan_name=(\S+)", (step_notes or {}).get("旧计划处理", ""))
        if m_adn:
            adopted_new_plan_name = m_adn.group(1)
        # 断点续跑时从上轮摘要恢复匹配数（「匹配N条」），供 48h 豁免判定
        m2 = re.search(r"匹配(\d+)条", (step_notes or {}).get("旧计划处理", ""))
        if m2:
            old_plan_matched = int(m2.group(1))
    else:
        if step_cb:
            step_cb("旧计划处理", "开始")
        t0 = time.monotonic()
        cmd = [PYTHON, "-m", "巨量千川.plan_sweep",
               "--creator", creator,
               "--product", product or "肥牛",
               "--department", department,
               "--days", "30"]  # 30 天窗：停了 7 天以上的老计划也要认（2026-10-09 盲区）
        apply_existing_materials = ENABLE_EXISTING_PLAN_MATERIALS and delivery_open
        if apply_existing_materials:
            cmd += ["--apply", "--materials-only"]
        rc, out, err = _run(cmd)
        j = _tail_json(out)
        if j is not None:
            old_plan_matched = int(j.get("matched") or 0)
        elapsed = time.monotonic() - t0
        if j is None:
            detail = f"耗时{elapsed:.0f}s rc={rc} {_tail_text(err or out)}"
            if step_cb:
                step_cb("旧计划处理", "失败", detail)
            steps.append(f"旧计划处理=fail:{detail}")
            return "fail", "；".join(steps)
        deleted = j.get("deleted") or []
        added = j.get("material_added") or []
        renamed = j.get("renamed") or []
        warns = j.get("warnings") or []
        fresh_material_blocked = j.get("fresh_material_blocked") or []
        catalog_warnings = []
        # 旧计划扫描返回的平台快照也必须进入 plan_catalog，避免企微表格
        # 加素材/更新日期后，本地数据库仍停留在旧计划名或旧指标。
        for scanned_plan in j.get("plans") or []:
            sync_error = _sync_poll_plan_catalog(
                scanned_plan, department=department, creator_name=creator
            )
            if sync_error:
                catalog_warnings.append(sync_error)
        # 加素材可能同步修改计划名日期前缀，按 plan_id 回写新名称。
        for rename in renamed:
            if not isinstance(rename, dict):
                continue
            sync_error = _sync_poll_plan_catalog(
                {"plan_id": rename.get("plan_id"),
                 "plan_name": rename.get("after") or rename.get("before")},
                department=department, creator_name=creator
            )
            if sync_error:
                catalog_warnings.append(sync_error)
        # 被轮询删除的老计划保留历史，但当前状态同步为「已删除」。
        plans_by_name = {
            str(item.get("plan_name") or ""): item
            for item in (j.get("plans") or [])
            if isinstance(item, dict)
        }
        for deleted_name in deleted:
            item = plans_by_name.get(str(deleted_name), {})
            sync_error = _mark_poll_plan_deleted(
                plan_id=str(item.get("plan_id") or ""),
                plan_name=str(deleted_name or ""),
                department=department,
            )
            if sync_error and "未找到" not in sync_error:
                catalog_warnings.append(sync_error)
        if catalog_warnings:
            warns.extend(catalog_warnings)
        chase_plan_id = j.get("chase_plan_id") or None
        adopted_new_plan_id = str(j.get("adopted_new_plan_id") or "")
        adopted_new_plan_name = str(j.get("adopted_new_plan_name") or "")
        action_desc = (f"加素材{len(added)}条（仅加素材，不删计划）"
                       if apply_existing_materials
                       else "只读检查（新增素材等待夜间窗口）")
        if renamed:
            action_desc += f"，同步更新投放日期前缀{len(renamed)}条"
        summary = f"耗时{elapsed:.0f}s 匹配{j.get('matched', 0)}条 {action_desc}"
        if warns:
            summary += f" 警告:{';'.join(warns)[:120]}"
        if chase_plan_id:
            summary += f" chase_plan_id={chase_plan_id}"
        if adopted_new_plan_id:
            summary += f" adopted_new_plan_id={adopted_new_plan_id}"
            if adopted_new_plan_name:
                summary += f" adopted_new_plan_name={adopted_new_plan_name}"
        if fresh_material_blocked and not added:
            detail = ("待业务员补新素材：旧计划未新增素材；" +
                      "、".join(str(name) for name in fresh_material_blocked[:3]) +
                      " 当前无发布≤72小时的该商品挂车视频")
            if step_cb:
                step_cb("旧计划处理", "等待", detail)
            steps.append(f"旧计划处理=等待:{detail}")
            return "wait", "；".join(steps)
        if chase_plan_id:
            if not apply_existing_materials:
                detail = f"{summary}；已有旧计划，等待 23:00 分类收口后新增素材"
                if step_cb:
                    step_cb("旧计划处理", "等待", detail)
                return "wait", f"等夜间窗口建计划（{delivery_note}；已有旧计划待新增素材）"
        if step_cb:
            step_cb("旧计划处理", "成功", summary)
        steps.append(f"旧计划处理=ok({summary})")
        if chase_plan_id:
            # 已有有效旧计划：夜间只新增素材，不建重复计划，也不执行自动追投。
            if step_cb:
                step_cb("建计划", "成功",
                        "跳过：已有有效旧计划，已按夜间窗口新增素材")
            done.add("建计划")
            steps.append("建计划=跳过(已有有效旧计划)")

    # 模块4 建计划（ENABLE_PUBLISH=True：建成即发布，真实消耗）
    # 建计划和老计划新增素材仅限分类完成后的 23:00-次日07:59；白天只处理授权、识别和回写。
    # 2026-08-23 策略：新计划不再建成即跑量——发布后由「暂停待发」模块立即暂停，
    # 挂 08:00 恢复（8:00 启动当日新视频计划）。
    built_plan_name = ""
    built_plan_id = ""
    if "建计划" not in done and not delivery_open:
        if step_cb:
            step_cb("建计划", "等待", delivery_note)
        return "wait", f"等夜间窗口建计划（{delivery_note}）"
    if "建计划" in done:
        steps.append("建计划=跳过(已完成)")
    else:
        cmd = [PYTHON, "-m", "巨量千川.plan_create",
               "--product", product or "肥牛",
               "--creator", creator,
               "--department", department,
               "--seller", seller or "自动",
               "--select-first-video", "--confirm-risky"]
        if roi:
            # 旧素材通道等特殊票：计划按指定 ROI 目标建立（2026-10-09 用户规则：旧素材 6.5）
            cmd += ["--roi", str(roi)]
        created_ms = re.sub(r"\D", "", created_at or "")
        if created_ms:
            cmd += ["--created-ms", created_ms]
        # 素材 ≤72h 为硬闸：即使该达人+商品从未投过，也不得用老视频建计划。
        cmd += ["--plan-date", resume_date]
        if ENABLE_PUBLISH:
            cmd.append("--publish")
        # 绕开 _exec_step：要从成功输出里解析 plan_name/plan_id 给「暂停待发」
        if step_cb:
            step_cb("建计划", "开始")
        t0 = time.monotonic()
        rc, out, err = _run(cmd)
        elapsed = time.monotonic() - t0
        ok2 = rc == 0
        detail = _tail_text(out if ok2 else (err or out))
        if ok2:
            result_json = _tail_json(out) or {}
            built_plan_name = result_json.get("plan_name") or ""
            built_plan_id = result_json.get("plan_id") or ""
            note = f"耗时{elapsed:.0f}s" + (f" {detail}" if detail else "")
            if built_plan_name:
                note += f" plan_name={built_plan_name}"
                if built_plan_id:
                    note += f" plan_id={built_plan_id}"
            if built_plan_id:
                sync_error = _sync_poll_plan_catalog(
                    {
                        "plan_id": built_plan_id,
                        "plan_name": built_plan_name,
                        "status": "投放中" if ENABLE_PUBLISH else "草稿",
                    },
                    department=department,
                    creator_name=creator,
                )
                if sync_error:
                    note += f"；{sync_error}"
            if step_cb:
                step_cb("建计划", "成功", note)
        else:
            if step_cb:
                step_cb("建计划", "失败",
                        f"耗时{elapsed:.0f}s rc={rc}" + (f" {detail}" if detail else ""))
        if not ok2:
            if "素材超过72小时" in (detail or ""):
                material_wait = "待业务员补新素材：当前无发布≤72小时的该商品挂车视频"
                if step_cb:
                    step_cb("建计划", "等待", material_wait)
                steps.append(f"建计划=等待：{material_wait}")
                return "wait", "；".join(steps)
            if "视频抽屉接口未返回任何视频" in (detail or ""):
                # 达人还没发该商品的挂车主页视频（平台侧无素材）——业务等待态，
                # 不算自动化失败、不消耗重试次数；达人发视频后下轮自动建成。
                # 2026-08-22 用户指认 rC2ylk「这不能是失败的」。
                steps.append("建计划=等待：达人暂无该商品挂车视频，等达人发布")
                return "wait", "；".join(steps)
            if "短视频带货权限" in (detail or ""):
                # 达人缺「商品短视频带货权限」（平台 popover 原文，≠投放授权、
                # ≠橱窗带货权限）——达人侧资质问题；走下方 _PERM_BLOCKED 会
                # 回退授权重查，而投放授权永远显示生效=死循环。直接终态交
                # 业务催达人开通（2026-09-29 实证 49332231067 安安妈妈好物）。
                steps.append("建计划=不可重试:达人无商品短视频带货权限（投放授权已生效，需达人自己在抖音开通该权限，权限≠橱窗带货权限）")
                return "fatal", "；".join(steps)
            if any(k in (detail or "") for k in _PERM_BLOCKED):
                # 勾选框禁用 = 商品投放授权未完全生效：吊销授权确认断点，
                # 下轮重新查授权（只认「商品投放」行），生效后再建计划
                if step_cb:
                    step_cb("授权确认", "等待",
                            f"建计划发现勾选禁用，回退授权复查: {detail[:150]}")
                steps.append("建计划=权限未生效，回退授权等待")
                return "wait", "；".join(steps)
            if _is_fatal(detail):
                steps.append(f"建计划=不可重试:{detail}")
                return "fatal", "；".join(steps)
            steps.append(f"建计划=fail:{detail}")
            return "fail", "；".join(steps)
        steps.append("建计划=ok")

    # 模块4.5 暂停待发（2026-08-23 策略）：新建成的计划立即暂停，挂 08:00 恢复。
    # 断点续跑时从「建计划」断点备注里取 plan_name/plan_id。老计划新素材
    # （建计划被跳过）不暂停——老计划按老池/原窗口运行。
    if "暂停待发" in done:
        steps.append("暂停待发=跳过(已完成)")
    else:
        build_note = (step_notes or {}).get("建计划", "")
        if adopted_new_plan_id:
            # 收养的已暂停老计划（加素材+改名后列新计划）：已是暂停态，
            # 调 pause-one 的 already 分支直接挂 08:00 新计划恢复票（不新建、不撞权限）。
            cmd = [PYTHON, "-m", "巨量千川.plan_power", "pause-one",
                   "--name", adopted_new_plan_name or adopted_new_plan_id,
                   "--department", department, "--from-poll"]
            cmd += ["--resume-date", resume_date]
            cmd += ["--plan-id", adopted_new_plan_id]
            if roi:
                cmd += ["--roi", str(roi)]
            if plan_label:
                cmd += ["--label", plan_label]
            if step_cb:
                step_cb("暂停待发", "开始")
            t0 = time.monotonic()
            rc, out, err = _run(cmd)
            elapsed = time.monotonic() - t0
            j = _tail_json(out)
            if rc == 0 and j and (j.get("queued") or j.get("result") in ("ok", "already")):
                if step_cb:
                    step_cb("暂停待发", "成功", f"耗时{elapsed:.0f}s 收养老计划挂08:00新计划票")
                done.add("暂停待发")
                steps.append("暂停待发=ok(收养老计划，挂08:00恢复)")
            else:
                detail = _tail_text(err or out)
                if step_cb:
                    step_cb("暂停待发", "失败", f"耗时{elapsed:.0f}s rc={rc} {detail}")
                steps.append(f"暂停待发=fail:{detail}")
                return "fail", "；".join(steps)
        elif chase_plan_id or "跳过(已有有效旧计划)" in build_note:
            done.add("暂停待发")
            steps.append("暂停待发=跳过(老计划新素材，按计划原窗口运行)")
        else:
            if not built_plan_name:
                m_name = re.search(r"plan_name=(\S+)", build_note)
                m_id = re.search(r"plan_id=(\d+)", build_note)
                built_plan_name = m_name.group(1) if m_name else ""
                built_plan_id = m_id.group(1) if m_id else ""
            if not built_plan_name:
                # 老断点（策略上线前建成的计划）没有 plan_name 记录：23:00 收口
                # 会统一处理它们，本模块不阻塞，记完成放行。
                done.add("暂停待发")
                steps.append("暂停待发=跳过(无建计划产物记录，由 23:00 收口兜底)")
            else:
                cmd = [PYTHON, "-m", "巨量千川.plan_power", "pause-one",
                       "--name", built_plan_name, "--department", department,
                       "--from-poll"]
                cmd += ["--resume-date", resume_date]
                if built_plan_id:
                    cmd += ["--plan-id", built_plan_id]
                if roi:
                    cmd += ["--roi", str(roi)]
                if plan_label:
                    cmd += ["--label", plan_label]
                if step_cb:
                    step_cb("暂停待发", "开始")
                t0 = time.monotonic()
                rc, out, err = _run(cmd)
                elapsed = time.monotonic() - t0
                j = _tail_json(out)
                if rc == 0 and j and (j.get("queued") or j.get("result") in ("ok", "already")):
                    log_detail = ""
                    if built_plan_id:
                        sync_error = _sync_poll_plan_catalog(
                            {
                                "plan_id": built_plan_id,
                                "plan_name": built_plan_name,
                                "status": "已暂停",
                            },
                            department=department,
                            creator_name=creator,
                        )
                        if sync_error:
                            log_detail = f"；{sync_error}"
                    if step_cb:
                        step_cb("暂停待发", "成功", f"耗时{elapsed:.0f}s 已暂停，挂 08:00 恢复{log_detail}")
                    done.add("暂停待发")
                    steps.append("暂停待发=ok(已暂停，挂08:00恢复)")
                else:
                    detail = _tail_text(err or out)
                    if step_cb:
                        step_cb("暂停待发", "失败", f"耗时{elapsed:.0f}s rc={rc} {detail}")
                    steps.append(f"暂停待发=fail:{detail}")
                    return "fail", "；".join(steps)

    # 模块5 建追投（ENABLE_CHASE=True：真实扣费 100 元/24h；
    # 旧计划有数据时追投目标锁定该旧计划 plan_id，防止追错该达人其他商品的计划）
    if "追投" in done:
        steps.append("追投=跳过(已完成)")
    elif adopted_new_plan_id:
        # 收养的已暂停老计划：刚挂 08:00 新计划票尚未开投，追投无意义，跳过。
        done.add("追投")
        steps.append("追投=跳过(收养老计划列新计划，待08:00开启)")
    elif ENABLE_CHASE:
        cmd = [PYTHON, "-m", "巨量千川.chase_create", "create",
               "--department", department]
        if chase_plan_id:
            cmd += ["--plan-id", chase_plan_id]
        else:
            cmd += ["--creator", creator]
        if step_cb:
            step_cb("追投", "开始")
        t0 = time.monotonic()
        rc, out, err = _run(cmd)
        elapsed = time.monotonic() - t0
        full = (out or "") + "\n" + (err or "")
        if rc == 0:
            if step_cb:
                step_cb("追投", "成功", f"耗时{elapsed:.0f}s {_tail_text(out)}")
            steps.append("追投=ok")
        elif "已有追投任务" in full:
            # 幂等达成：该计划已有追投任务（僵死重试/人工已建场景），
            # 视为追投完成 —— 不算失败、不重复扣费、不消耗重试次数
            if step_cb:
                step_cb("追投", "成功", f"耗时{elapsed:.0f}s 计划已有追投任务，幂等达成")
            steps.append("追投=ok(任务已存在)")
        else:
            detail = f"耗时{elapsed:.0f}s rc={rc} {_tail_text(err or out)}"
            if _is_fatal(detail):
                if step_cb:
                    step_cb("追投", "失败", detail)
                steps.append(f"追投=不可重试:{detail}")
                return "fatal", "；".join(steps)
            if step_cb:
                step_cb("追投", "失败", detail)
            steps.append(f"追投=fail:{detail}")
            return "fail", "；".join(steps)
    else:
        steps.append("追投=暂停（等待新追投标准）")

    return "ok", "；".join(steps)


def process_video_task(creator_name: str = "", product_name: str = "",
                       start_date: str = "", skip_steps=(), step_cb=None, **_kw):
    """视频号投流：填表（停在提交前，需人工到浏览器点提交）。返回 (status, msg)。"""
    if not creator_name:
        return "fail", "缺少达人名称"
    if "填表" in skip_steps:
        return "ok", "视频号填表=跳过(已完成)"

    cmd = [PYTHON, "-m", "视频号投流.submit_order",
           "--keyword", creator_name, "--no-budget", "--template", "肥牛片"]
    d = _normalize_date(start_date)
    if d:
        cmd += ["--start-date", d]
    if step_cb:
        step_cb("填表", "开始")
    t0 = time.monotonic()
    rc, out, err = _run(cmd)
    j = _tail_json(out)
    ok = (j or {}).get("status") == "ok" if j else (rc == 0)
    msg = "视频号填表=" + ("ok" if ok else "fail") + (f"（日期{d}）" if d else "（未设日期）")
    if step_cb:
        step_cb("填表", "成功" if ok else "失败",
                f"耗时{time.monotonic() - t0:.0f}s {_tail_text(err or out)}")
    return ("ok" if ok else "fail"), msg


# 触发字段 -> (函数名, 该字段传给哪个参数)
ACTIONS = {
    "抖音号": ("process_douyin_task", "creator"),
    "达人名称": ("process_video_task", "creator_name"),
}

# 字段标题 -> 函数参数名（创建人/创建时间/进度 是系统/状态字段，不映射）
FIELD_MAP = {
    "抖音号": "creator",
    "合作码": "code",
    "商品ID": "product",
    "业务员": "seller",
    "业务部": "department",
    "备注": "remark",
    "达人名称": "creator_name",
    "商品": "product_name",
    "视频时间": "start_date",
    "创建时间": "created_at",
    "_scheduled_start": "scheduled_start",
}


def run_afternoon_follow_up(scan_slot: str, dry_run: bool = False):
    """14:00/16:00 跟进新计划升档并同步数据库；不执行17:00熔断。"""
    if scan_slot not in ("14:00", "16:00"):
        return False, "下午跟进只支持 14:00/16:00"
    cmd = [PYTHON, "-m", "巨量千川.plan_power", "afternoon-follow-up",
           "--scan-slot", scan_slot,
           # 父进程（日报）自己举着播报忙标，子进程必须跳过等播报空闲，
           # 否则自等 15 分钟（2026-09-03 16pm 档因此迟推半小时）
           "--from-report"]
    if dry_run:
        cmd.append("--dry-run")
    rc, out, err = _run(cmd, timeout=max(STEP_TIMEOUT, 900))
    data = _tail_json(out)
    if rc == 0 and data:
        return True, data
    return False, _tail_text(err or out)


def run_daily_report(time_range: str = "", suffix: str = "", scan_slot: str = ""):
    """每日投放数据汇总：调 巨量千川.daily_report（扫三部门数据，约2-4分钟）。

    time_range: 总览页快捷选项原文（""=今天，"昨天"=昨日全天）；
    suffix: 标题日期后追加的文字（如 "（截至 12:00）"）。
    返回 (ok, data)：ok=True 时 data 为模块输出的 JSON dict（含 markdown 推送文本）；
    ok=False 时 data 为错误摘要字符串。
    """
    cmd = [PYTHON, "-m", "巨量千川.daily_report"]
    if time_range:
        cmd += ["--time-range", time_range]
    if suffix:
        cmd += ["--suffix", suffix]
    if scan_slot:
        cmd += ["--scan-slot", scan_slot]
    if scan_slot in ("22:30", "23:00") and time_range != "昨天":
        cmd += ["--include-started-today"]
    rc, out, err = _run(cmd)
    if rc != 0:
        return False, _tail_text(err or out)
    # stdout 是单行 JSON（浏览器侧噪声都走 stderr）；保险起见取最后一个 JSON 对象行
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("success"):
                return True, data
            return False, str(data.get("message") or "日报模块返回失败")[:300]
    return False, f"日报模块未输出 JSON: {_tail_text(out)}"


def run_qualified_chase():
    """达标达人自动追投（近30天单量 ≥ 三店最高×0.6）：调 巨量千川.qualified_chase。

    返回 (ok, data)：ok=True 时 data 为模块 JSON（chased/skipped/failed 明细）。
    """
    if not ENABLE_QUALIFIED_CHASE:
        return True, {
            "success": True,
            "skipped": True,
            "message": "达标自动追投已关闭",
        }
    if not automated_growth_enabled():
        return True, {
            "success": True,
            "skipped": True,
            "message": "自动增长开关已关闭，未创建达标追投",
        }
    cmd = [PYTHON, "-m", "巨量千川.qualified_chase"]
    rc, out, err = _run(cmd)
    data = _tail_json(out)
    if rc == 0 and data:
        return True, data
    return False, _tail_text(err or out)


def run_budget_raise():
    """ROI>10 且订单>10 的计划预算改不限。返回 (ok, data)。"""
    if not automated_growth_enabled():
        return True, {
            "success": True,
            "skipped": True,
            "message": "自动增长开关已关闭，未调整计划预算",
        }
    cmd = [PYTHON, "-m", "巨量千川.budget_raise", "--apply"]
    rc, out, err = _run(cmd)
    data = _tail_json(out)
    if rc == 0 and data:
        return True, data
    return False, _tail_text(err or out)
