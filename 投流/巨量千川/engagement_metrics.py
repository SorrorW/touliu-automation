"""Read-only plan engagement metrics via Qianchuan's detail data API."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from .session_prepare import connect_browser, ensure_qianchuan_page, open_fresh_page


_STAT_QUERY_PATH = "/ad/api/data/v1/common/statQuery"
_REFER = "ecp,7325382559167168538,7359144207337963547"


def _account_id(page_url: str) -> str:
    return (parse_qs(urlparse(page_url).query).get("aavid") or [""])[0]


async def _query_today_engagement(
    page, plan_id: str, report_date: dt.date | None = None
) -> dict[str, int] | None:
    advertiser_id = _account_id(page.url)
    if not advertiser_id:
        return None
    now = dt.datetime.now().replace(microsecond=0)
    target_date = report_date or now.date()
    start_time = dt.datetime.combine(target_date, dt.time.min)
    # Historical reports cover the complete day; today's report ends at now.
    end_time = now if target_date == now.date() else start_time + dt.timedelta(days=1)
    payload = {
        "DataSetKey": "roi2_boost_show",
        "Dimensions": ["advertiser_id"],
        "Filters": {
            "ConditionRelationshipType": 1,
            "Conditions": [
                {"Field": "advertiser_id", "Operator": 7, "Values": [advertiser_id]},
                {"Field": "ad_id", "Operator": 7, "Values": [str(plan_id)]},
                {"Field": "marketing_goal", "Operator": 7, "Values": ["1"]},
            ],
        },
        "Metrics": [
            "boost_support_inc_show_cnt",
            "boost_support_inc_click_cnt",
            "roi2_boost_support_inc_pay_order_count",
        ],
        "refer": _REFER,
        "DrillDimensions": [["roi2_boost"]],
        "StartTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    response = await page.evaluate(
        """async ({payload, advertiserId}) => {
            const response = await fetch(`/ad/api/data/v1/common/statQuery?aavid=${encodeURIComponent(advertiserId)}`, {
                method: 'POST', credentials: 'include',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify(payload),
            });
            return {status: response.status, body: await response.json()};
        }""",
        {"payload": payload, "advertiserId": advertiser_id},
    )
    if int(response.get("status") or 0) != 200:
        return {"_error": f"http={response.get('status')}"}
    body = response.get("body") or {}
    totals = ((body.get("data") or {}).get("StatsData") or {}).get("Totals") or {}
    if not isinstance(totals, dict) or not totals:
        return {"_error": json.dumps(body, ensure_ascii=False)[:500]}

    def value(name: str) -> int | None:
        metric = totals.get(name)
        if not isinstance(metric, dict) or metric.get("Value") is None:
            return None
        return int(metric["Value"])

    impressions = value("boost_support_inc_show_cnt")
    clicks = value("boost_support_inc_click_cnt")
    if impressions is None or clicks is None:
        return {"_error": f"missing metrics: {','.join(totals)}"}
    return {
        "impression_count": impressions,
        "click_count": clicks,
        "detail_order_count": value("roi2_boost_support_inc_pay_order_count") or 0,
    }


async def enrich_plan_records(
    records_by_department: dict[str, list[dict]],
    report_date: dt.date | None = None,
) -> dict[str, int]:
    """Populate today's exposure and click fields for non-paused plan records.

    The API calls are read-only. One existing Qianchuan tab is reused and restored
    after the scan, so the enrichment does not accumulate browser tabs.
    """
    counts = {"queried": 0, "completed": 0, "unavailable": 0}
    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        page = next((p for p in context.pages if "qianchuan.jinritemai.com" in p.url), None)
        created_page = page is None
        if page is None:
            page = await open_fresh_page(context)
        original_url = page.url
        try:
            for department, records in records_by_department.items():
                page = await ensure_qianchuan_page(page, department, timeout=30)
                # Account switching can finish the route before the detail
                # data runtime is ready; wait briefly before issuing statQuery.
                await page.wait_for_timeout(1500)
                for record in records:
                    status = str(record.get("status") or "")
                    # Budget-exhausted plans stay in the report, so they need the
                    # same read-only detail metrics as active new/potential plans.
                    if (
                        ("暂停" in status and record.get("pause_category") != "budget_exhausted")
                        or not record.get("plan_id")
                    ):
                        continue
                    counts["queried"] += 1
                    try:
                        metrics = await asyncio.wait_for(
                            _query_today_engagement(
                                page, str(record["plan_id"]), report_date=report_date
                            ),
                            timeout=30,
                        )
                    except Exception:
                        metrics = None
                    if metrics is None or metrics.get("_error"):
                        if metrics and metrics.get("_error"):
                            record["engagement_error"] = metrics["_error"]
                        counts["unavailable"] += 1
                        continue
                    record.update(metrics)
                    counts["completed"] += 1
        finally:
            if created_page:
                await page.close()
            elif page.url != original_url:
                try:
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass
    return counts


async def _main(department: str, plan_id: str) -> int:
    record = {"plan_id": plan_id, "status": "投放中"}
    result = await enrich_plan_records({department: [record]})
    print(json.dumps({"result": result, "record": record}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读读取计划今日曝光和点击")
    parser.add_argument("--department", default="业务1部")
    parser.add_argument("--plan-id", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(_main(args.department, args.plan_id)))
