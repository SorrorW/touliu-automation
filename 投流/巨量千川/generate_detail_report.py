"""Create a full local plan-detail report from a saved read-only scan."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .daily_report import CATEGORY_META, _category_summary, _plan_line


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "wecom-agent" / "logs" / "manual_daily_report_20260820.json"
OUTPUT = ROOT / "wecom-agent" / "logs" / "daily_detail_report_20260820_1603.md"


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    lines = ["# 投放逐条明细", "", "数据时间：2026-08-20 16:03（只读扫描）"]
    lines.append("说明：当前千川总览未返回曝光、点击字段，报告不将其显示为 0。")
    for department, data in source["departments"].items():
        lines.extend(["", f"## {department}"])
        lines.append(
            f"消耗 {data['spent']:,.2f} 元，成交 {data['orders']} 单，"
            f"ROI {data['roi']:g}，共 {data['plans']} 条计划"
        )
        for key, title in CATEGORY_META:
            plans = [
                plan for plan in copy.deepcopy(data.get("categories", {}).get(key, []))
                if "暂停" not in str(plan.get("status") or "")
            ]
            summary = _category_summary(plans)
            lines.extend(["", f"### {title}（{summary['count']} 条）"])
            for plan in sorted(
                plans,
                key=lambda item: (-(item.get("spent") or 0), -(item.get("orders") or 0), item.get("plan_name") or ""),
            ):
                # The saved scan used the previous default 0 values when the
                # columns were unavailable. Preserve that uncertainty.
                if plan.get("impression_count") == 0 and plan.get("click_count") == 0:
                    plan["impression_count"] = None
                    plan["click_count"] = None
                lines.append(
                    _plan_line(
                        plan,
                        include_department=False,
                        include_details=key != "old_pool",
                    )
                )
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
