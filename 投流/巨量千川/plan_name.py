"""Shared helpers for stamping a Qianchuan plan name with its delivery date."""
from __future__ import annotations

import datetime
import re
from typing import Any

# Supported historic forms include ``0825_达人_赵`` and ``8.25-达人-赵``.
# A separator after the date is required so an ID/other leading number is never
# mistaken for a date prefix.
_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\d{1,2}\s*[./-]\s*\d{1,2})|(?:\d{3,4}))[_-]"
)


def with_delivery_date_prefix(
    plan_name: str, delivery_date: datetime.date | None = None
) -> str:
    """Return *plan_name* with a normalized ``MMDD_`` delivery-date prefix.

    Existing supported date prefixes are replaced.  Names without one receive
    a prefix so plans promoted by a new material can subsequently be recognized
    as that day's test plans.
    """
    source = (plan_name or "").strip()
    if not source:
        raise ValueError("计划名称为空，无法写入投放日期前缀")

    target_day = delivery_date or datetime.date.today()
    match = _DATE_PREFIX_RE.match(source)
    suffix = source[match.end():] if match else source
    suffix = suffix.strip().lstrip("_-").strip()
    prefix = target_day.strftime("%m%d")
    return f"{prefix}_{suffix}" if suffix else prefix


async def update_plan_name_delivery_date(
    page: Any, delivery_date: datetime.date | None = None
) -> dict[str, Any]:
    """Update the edit-page name input and verify the date-prefix rewrite.

    The caller is responsible for saving the edit page, so ROI/material/name
    changes can be committed together in one save action.
    """
    name_input = page.locator('input[placeholder*="计划名称"]').first
    if await name_input.count() == 0:
        raise RuntimeError("编辑页未找到计划名称输入框")

    before = (await name_input.input_value()).strip()
    after = with_delivery_date_prefix(before, delivery_date)
    if after == before:
        return {"changed": False, "before": before, "after": after}

    await name_input.scroll_into_view_if_needed()
    await name_input.fill(after)
    await name_input.press("Tab")
    await page.wait_for_timeout(300)
    actual = (await name_input.input_value()).strip()
    if actual != after:
        raise RuntimeError(f"计划名称修改后未生效: 预期={after}, 实际={actual}")
    return {"changed": True, "before": before, "after": after}
