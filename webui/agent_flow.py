"""文本建计划 agent 流程：解析操作员粘贴的文本，自动跑 授权 → 建计划 → 建追投。

解析有两条路径：
- LLM（deepseek/kimi，已配置 API Key 时优先）：自由文本 → 结构化字段
- 规则解析（兜底）：按「键：值」行提取，字段有固定标签

输入格式（键顺序任意，可选行可缺，冒号中英文均可）::

    创建投流计划
    部门：业务2部
    业务员：张三
    抖音号：12345678901
    合作码：10000000002
    商品ID：2000000000000000001
"""

from __future__ import annotations

import json
import re
from typing import Any

from 巨量千川.auth import STATUS_OK, authorize_creator, check_authorization
from 巨量千川.chase_create import create_chase
from 巨量千川.daily_high_performer_monitor import CHASE_BUDGET, CHASE_HOURS
from 巨量千川.plan_create import PlanRequest, create_plan
from 巨量千川.session_prepare import ACCOUNT_BY_DEPARTMENT, lookup_product

from .llm import LLMError, chat, load_config
from .logging_setup import get_logger

_log = get_logger(__name__)

# 支持的文本键 → 内部字段名（同义词都列出来）
_KEY_MAP = {
    "部门": "department",
    "业务员": "seller",
    "抖音号": "creator_id",
    "抖音号ID": "creator_id",
    "合作码": "code",
    "商品ID": "product",
    "商品": "product",
    "达人名": "creator_name",
    "达人": "creator_name",
    "达人显示名": "creator_name",
    "ROI": "roi",
    "roi": "roi",
    "预算": "budget",
    "发布": "publish",
}

_REQUIRED = ["department", "seller", "creator_id", "product"]

_LLM_SYSTEM = """你是投流任务文本解析器。从用户文本中提取建投放计划所需字段，只输出 JSON，不要任何解释。
字段（缺失的可选项省略）：
- department: 部门，必须是 业务1部/业务2部/业务3部 之一
- seller: 业务员（原样，如 张三、李四）
- creator_id: 抖音号 ID（必填）
- code: 合作码（可选，纯数字）
- product: 商品ID（纯数字原样）或商品名（商品A/商品B/商品C/商品D）
- creator_name: 达人显示名（可选）
- roi: ROI 目标（可选，数字）
- budget: 日预算（可选，整数）
- publish: 是否直接发布（可选，布尔；文本明确说发布才为 true）
只输出一个 JSON 对象。"""


class BriefParseError(ValueError):
    """文本解析失败，message 直接展示给操作员。"""


def validate_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化字段（规则解析和 LLM 解析共用）。

    Raises:
        BriefParseError: 缺必填字段或字段值非法，message 为中文原因。
    """
    missing = [
        {"department": "部门", "seller": "业务员", "creator_id": "抖音号", "product": "商品ID/商品"}[k]
        for k in _REQUIRED
        if not raw.get(k) or not str(raw[k]).strip()
    ]
    if missing:
        raise BriefParseError("缺少必填项: " + "、".join(missing))

    department = str(raw["department"]).strip()
    if department not in ACCOUNT_BY_DEPARTMENT:
        raise BriefParseError(
            f"部门「{department}」无效（可选: {'/'.join(ACCOUNT_BY_DEPARTMENT)}）"
        )

    product = str(raw["product"]).strip()
    if lookup_product(product) is None:
        raise BriefParseError(f"商品「{product}」无法识别（填商品ID或 商品A/商品B/商品C/商品D）")

    roi = 7.3
    if raw.get("roi") not in (None, ""):
        try:
            roi = float(raw["roi"])
        except (TypeError, ValueError) as exc:
            raise BriefParseError(f"ROI「{raw['roi']}」不是数字") from exc

    budget = 300
    if raw.get("budget") not in (None, ""):
        try:
            budget = int(float(raw["budget"]))
        except (TypeError, ValueError) as exc:
            raise BriefParseError(f"预算「{raw['budget']}」不是整数") from exc

    publish_raw = raw.get("publish")
    if isinstance(publish_raw, bool):
        publish = publish_raw
    else:
        publish = str(publish_raw or "").strip() in {"是", "yes", "Y", "true", "1"}

    creator_id = str(raw["creator_id"]).strip()
    return {
        "department": department,
        "seller": str(raw["seller"]).strip(),
        "creator_id": creator_id,
        "code": str(raw["code"]).strip() if raw.get("code") else None,
        "product": product,
        "creator_name": (str(raw["creator_name"]).strip() if raw.get("creator_name") else None),
        "roi": roi,
        "budget": budget,
        "publish": publish,
    }


def parse_brief(text: str) -> dict[str, Any]:
    """规则解析：按「键：值」行提取字段。"""
    raw: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z一-鿿]+)\s*[：:]\s*(.+?)\s*$", line)
        if not match:
            continue  # 标题行（如"创建投流计划"）等无冒号行直接忽略
        key, value = match.group(1).strip(), match.group(2).strip()
        field_name = _KEY_MAP.get(key) or _KEY_MAP.get(key.replace(" ", ""))
        if field_name:
            raw[field_name] = value
    return validate_fields(raw)


def parse_brief_llm(text: str) -> dict[str, Any]:
    """LLM 解析：自由文本 → JSON 字段 → 校验。"""
    config = load_config()
    answer = chat(config, _LLM_SYSTEM, text)
    # 允许模型把 JSON 包在 ```json ... ``` 里
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if not match:
        raise BriefParseError(f"LLM 未返回 JSON: {answer[:150]}")
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise BriefParseError(f"LLM 返回的 JSON 无法解析: {str(exc)[:100]}") from exc
    if not isinstance(raw, dict):
        raise BriefParseError("LLM 返回的不是 JSON 对象")
    return validate_fields(raw)


def parse_auto(text: str) -> dict[str, Any]:
    """自动选择解析路径：有 API Key 先走 LLM，失败回退规则解析。"""
    config = load_config()
    if config.get("api_key"):
        try:
            fields = parse_brief_llm(text)
            _log.info("LLM 解析成功: %s（%s）", config["provider"], config["model"])
            return {
                "fields": fields,
                "parser_used": config["provider"],
                "parse_note": f"由 {config['provider']}（{config['model']}）解析",
            }
        except (LLMError, BriefParseError) as exc:
            _log.warning("LLM 解析失败，回退规则解析: %s", exc)
            try:
                fields = parse_brief(text)
            except BriefParseError:
                raise BriefParseError(f"LLM 解析失败（{exc}），规则解析也未通过") from exc
            return {
                "fields": fields,
                "parser_used": "规则解析",
                "parse_note": f"LLM 解析失败（{exc}），已回退规则解析",
            }
    return {
        "fields": parse_brief(text),
        "parser_used": "规则解析",
        "parse_note": "未配置 API Key，使用规则解析",
    }


def _step(step: str, status: str, detail: str = "") -> dict[str, str]:
    return {"step": step, "status": status, "detail": detail}


async def run_plan_flow(fields: dict[str, Any], progress_cb=None) -> dict[str, Any]:
    """按字段顺序执行 授权 → 建计划 → 建追投，任一步失败即中止。

    progress_cb: Callable[[int, int], None]，每步结束后上报 (已完成步数, 3)。

    Returns:
        {"ok": bool, "steps": [{"step", "status": ok/skip/failed, "detail"}]}
    """
    steps: list[dict[str, str]] = []
    creator = fields["creator_id"]
    department = fields["department"]
    plan_id = None  # 建计划成功后回填，供待建计划面板补建追投
    auth_info = None  # 授权步结果，供待建计划面板显示授权状态

    def _report() -> None:
        if progress_cb:
            progress_cb(len(steps), 3)

    def _done(ok: bool) -> dict[str, Any]:
        _report()
        return {"ok": ok, "steps": steps, "plan_id": plan_id, "auth": auth_info}

    # 1. 授权
    try:
        if fields["code"]:
            auth_result = await authorize_creator(creator, fields["code"], department)
            auth_info = {
                "douyin_auth": auth_result.get("douyin_auth"),
                "toufang_auth": auth_result.get("toufang_auth"),
                "authorized": (
                    auth_result.get("douyin_auth") == STATUS_OK
                    and auth_result.get("toufang_auth") == STATUS_OK
                ),
                "creator_name": auth_result.get("creator_name"),
            }
            ok = auth_result.get("success", False)
            steps.append(_step("授权申请", "ok" if ok else "failed", auth_result.get("message", "")))
            if not ok:
                return _done(False)
        else:
            auth_result = await check_authorization(creator, department)
            auth_info = {
                "douyin_auth": auth_result.get("douyin_auth"),
                "toufang_auth": auth_result.get("toufang_auth"),
                "authorized": auth_result.get("authorized"),
                "creator_name": auth_result.get("creator_name"),
            }
            if auth_result.get("authorized"):
                steps.append(_step("授权检查", "ok", "两项授权均已生效"))
            else:
                steps.append(_step("授权检查", "failed", "授权未生效，请在文本中补充合作码"))
                return _done(False)
    except Exception as exc:
        steps.append(_step("授权", "failed", str(exc)))
        return _done(False)
    _report()

    # 2. 建投放计划
    try:
        request = PlanRequest(
            product_name=fields["product"],
            creator_id=creator,
            department=department,
            roi=fields["roi"],
            daily_budget=fields["budget"],
            creator_name=fields["creator_name"],
            seller=fields["seller"],
            select_first_video=True,
            publish=fields["publish"],
            confirm_risky=True,  # 发布时自动确认平台风险弹窗
        )
        result = await create_plan(request)
        plan_id = result.plan_id
        plan_name = result.plan_name or ""
        if result.status == "failed" or (fields["publish"] and not plan_id):
            steps.append(_step("建投放计划", "failed", result.message or "创建失败"))
            return _done(False)
        detail = f"{plan_name} (ID: {plan_id})" if plan_id else f"{plan_name}（未发布）"
        steps.append(_step("建投放计划", "ok", detail))
    except Exception as exc:
        steps.append(_step("建投放计划", "failed", str(exc)))
        return _done(False)
    _report()

    # 3. 建追投（未发布拿不到 plan_id 时跳过）
    if not plan_id:
        steps.append(_step("建追投", "skip", "计划未发布、无 plan_id，发布后可用一键追投补建"))
        return _done(True)
    try:
        chase = await create_chase(
            main_plan_id=plan_id,
            budget=CHASE_BUDGET,
            hours=CHASE_HOURS,
            department=department,
        )
        steps.append(_step("建追投", "ok", f"task_id: {chase.get('task_id')}（{CHASE_BUDGET}元/{CHASE_HOURS}小时）"))
        _log.info("追投创建成功: plan_id=%s task_id=%s（%s元/%s小时）",
                  plan_id, chase.get("task_id"), CHASE_BUDGET, CHASE_HOURS)
    except Exception as exc:
        steps.append(_step("建追投", "failed", str(exc)))
        return _done(False)

    return _done(True)
