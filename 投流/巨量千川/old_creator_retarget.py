"""老达人新发视频：删除旧计划 → 新建商品乘方计划 → 追投新素材。

Workflow:
    1. 按旧计划名称在总览页定位旧计划；
    2. 进入编辑页，提取/确认「部门、业务员、抖音号、商品 ID」；
    3. 删除旧计划（需要显式 --confirm-delete）；
    4. 用提取信息新建商品乘方计划，并自动选择第一个新视频；
    5. 对新计划创建素材追投。

Usage:
    # 只查看旧计划信息，不删除
    python -m 巨量千川.old_creator_retarget --old-plan-name "0727_煮妇小承妈_赵"

    # 删除旧计划并新建计划 + 追投（默认不发布）
    python -m 巨量千川.old_creator_retarget --old-plan-name "0727_煮妇小承妈_赵" --confirm-delete

    # 删除旧计划、发布新计划、创建追投
    python -m 巨量千川.old_creator_retarget \
        --old-plan-name "0727_煮妇小承妈_赵" \
        --confirm-delete --publish --confirm-risky
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .chase_create import create_chase
from .plan_create import PlanRequest, WorkflowResult, build_plan_name, create_plan
from .plan_metrics import CDP_URL, DEFAULT_DEPARTMENT, _ensure_cdp, _open_overall, _parse_plan_row, _search_plans
from .session_prepare import ACCOUNT_BY_DEPARTMENT, PRODUCT_MAP, connect_browser, open_fresh_page


class RetargetError(RuntimeError):
    pass


@dataclass
class OldPlanInfo:
    department: str
    seller: str
    creator_id: str
    creator_name: str | None
    product_id: str
    product_name: str | None
    old_plan_id: str
    old_plan_name: str


# 业务部 → 千川账号 ID（用于 URL 反查）
_AAVID_BY_DEPARTMENT = {
    "业务3部": "1859511296951635",
    "业务2部": "1861049584106636",
    "业务1部": "1861049594712072",
}

# 这些账号 ID 会出现在页面各处，不应被误识别为抖音号
_KNOWN_ACCOUNT_IDS = set(_AAVID_BY_DEPARTMENT.values())


async def _body_text(page: Page) -> str:
    return await page.locator("body").evaluate("e => e.innerText")


async def _wait_stable(page: Page, seconds: float = 1.0) -> None:
    await page.wait_for_timeout(int(seconds * 1000))


def _department_from_url(url: str) -> str | None:
    """从编辑页 URL 的 aavid 反查业务部。"""
    match = re.search(r"aavid=(\d+)", url)
    if not match:
        return None
    aavid = match.group(1)
    dept = {v: k for k, v in _AAVID_BY_DEPARTMENT.items()}.get(aavid)
    return dept


def _parse_old_plan_name(name: str) -> tuple[str | None, str | None]:
    """从计划名解析达人显示名和业务员姓氏，并剔除商品名。

    兼容：
        0727_煮妇小承妈_赵
        0401李李小厨房-调
        0601-肉肉妈妈，仔骨-郑  -> 达人="肉肉妈妈", 业务员="郑"
    """
    # 1. 去掉前导日期（4 位或 2 位数字）
    rest = re.sub(r"^\d{2,4}", "", name).strip()
    if not rest:
        return None, None

    # 2. 找到最后一个业务员分隔符（-_）后的业务员姓氏。
    #    分隔符前是「达人名[,商品名...]」
    m = re.search(r"[-_]([^-_]+)$", rest)
    if not m:
        return None, None
    seller = m.group(1).strip()
    creator_raw = rest[: m.start()].strip("-_ \t")

    # 3. 把达人段按中文/英文逗号、顿号等切开，剔除商品名/商品 ID
    parts = re.split(r"[，,、/\\|]", creator_raw)
    cleaned = [
        p.strip().lstrip("-_ \t")
        for p in parts
        if p.strip()
        and p.strip().lstrip("-_ \t") not in PRODUCT_MAP
        and p.strip().lstrip("-_ \t") not in PRODUCT_MAP.values()
    ]
    creator_name = "，".join(cleaned) if cleaned else creator_raw
    return creator_name, seller


async def _search_old_plan(page: Page, plan_name: str, department: str) -> tuple:
    """按计划名称精确搜索并返回行元素 + plan_id。"""
    rows = await _search_plans(page, plan_name, department, by_creator=False)
    if not rows:
        raise RetargetError(f"未找到旧计划: {plan_name}")

    for tr in rows:
        metrics = await _parse_plan_row(tr)
        if metrics is None:  # 畸形行已留 WARNING，跳过
            continue
        if metrics.plan_name == plan_name:
            return tr, metrics.plan_id or ""

    # 兜底：返回第一个可见行
    metrics = await _parse_plan_row(rows[0])
    return rows[0], (metrics.plan_id or "") if metrics else ""


async def _open_edit_page(page: Page, plan_row) -> None:
    """点击旧计划行内的「编辑」按钮，等待跳转到编辑页。"""
    edit_btn = plan_row.get_by_text("编辑", exact=True).first
    if await edit_btn.count() and await edit_btn.is_visible():
        await edit_btn.click(force=True)
    else:
        icon = plan_row.locator('iconpark-icon[name="oc-icon-edit"]').first
        if await icon.count() and await icon.is_visible():
            await icon.evaluate("el => el.click()")
        else:
            handle = await plan_row.element_handle()
            clicked = await page.evaluate(
                """(row) => {
                    const items = row.querySelectorAll('button, a, [role="button"], iconpark-icon');
                    for (const el of items) {
                        const text = el.innerText || el.getAttribute('name') || '';
                        if (text.includes('编辑') || text.includes('icon-edit')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                handle,
            )
            if not clicked:
                raise RetargetError("无法找到旧计划的编辑按钮")

    # 等待跳转到编辑/创建页
    for _ in range(30):
        await _wait_stable(page, 0.5)
        if "overall-product-multi" in page.url or re.search(r"[?&]adId=\d+", page.url):
            return
    raise RetargetError(f"进入编辑页后 URL 不符合预期: {page.url}")


async def _extract_product_id(page: Page) -> tuple[str, str | None]:
    """从编辑页已选商品卡片或页面文本中提取商品 ID 与商品名。"""
    product_id_to_name = {v: k for k, v in PRODUCT_MAP.items()}

    # 先等商品卡片渲染
    try:
        await page.locator(".card-product-item").first.wait_for(state="attached", timeout=8000)
    except Exception:
        pass

    cards = page.locator(".card-product-item")
    count = await cards.count()
    for i in range(count):
        text = await cards.nth(i).inner_text()
        for pid in PRODUCT_MAP.values():
            if pid in text:
                return pid, product_id_to_name.get(pid)
        for name, pid in PRODUCT_MAP.items():
            if name in text:
                return pid, name

    body = await _body_text(page)
    for pid in PRODUCT_MAP.values():
        if pid in body:
            return pid, product_id_to_name.get(pid)
    for name, pid in PRODUCT_MAP.items():
        if name in body:
            return pid, name

    snippet = body[:500].replace("\n", " ")
    raise RetargetError(f"未能从编辑页提取商品 ID。页面文本片段: {snippet}")


async def _open_creator_drawer(page: Page) -> None:
    """点击「管理手动添加抖音号」/「手动添加抖音号」/「添加抖音号」打开右侧抽屉。"""
    for label in ["管理抖音号", "管理手动添加抖音号", "手动添加抖音号", "添加抖音号"]:
        try:
            await _click_btn(page, label, timeout=5.0)
            await _wait_stable(page, 2.0)
            return
        except RetargetError:
            continue

    await page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('button, a, [role="button"]'));
        for (const el of items) {
            if (el.offsetParent !== null) {
                const text = el.innerText || '';
                if (text.includes('管理抖音号') || text.includes('手动添加抖音号') || text.includes('添加抖音号')) {
                    el.click();
                    return;
                }
            }
        }
    }""")
    await _wait_stable(page, 2.0)

    # 等待任意可见 drawer
    for _ in range(10):
        drawers = page.locator(".ovui-drawer")
        for i in range(await drawers.count()):
            if await drawers.nth(i).is_visible():
                return
        await _wait_stable(page, 0.5)
    raise RetargetError("添加抖音号抽屉未在预期时间内打开")


def _looks_like_creator_id(token: str, require_digit: bool = False) -> bool:
    """简单的抖音号启发式：字母数字下划线，长度 4-20，排除商品/账号 ID。"""
    if not re.match(r"^[A-Za-z0-9_]+$", token):
        return False
    if len(token) < 4 or len(token) > 20:
        return False
    if token in _KNOWN_ACCOUNT_IDS:
        return False
    if re.fullmatch(r"\d{19}", token):
        return False
    # 排除长度 >= 10 的纯数字（避免账号 ID 变体）
    if token.isdigit() and len(token) >= 10:
        return False
    if require_digit and not any(ch.isdigit() for ch in token):
        return False
    return True


def _clean_id_number(raw: str) -> str | None:
    """从 'ID：31048806128' 这类文本中提取数字 ID，排除商品/账号 ID。"""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if digits in _KNOWN_ACCOUNT_IDS:
        return None
    if re.fullmatch(r"\d{19}", digits):
        return None
    return digits


async def _extract_creator_id_from_element(page: Page, creator_name: str | None) -> str | None:
    """不打开抽屉，尝试从页面已选达人区域读取抖音号。"""
    if not creator_name:
        return None

    # 找包含达人名的元素，读取其所在容器文本，再从中提取 "ID：xxx"
    text = await page.evaluate(
        """(name) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            const results = [];
            let node;
            while (node = walker.nextNode()) {
                if ((node.innerText || '').includes(name)) {
                    results.push(node.innerText.trim());
                    if (results.length >= 3) break;
                }
            }
            return results.join('\\n---\\n');
        }""",
        creator_name,
    )

    for block in text.split("\n---\n"):
        # 优先匹配 "ID：31048806128" 这类明确标记
        for m in re.finditer(r"ID[：:]\s*(\d+)", block):
            cid = _clean_id_number(m.group(1))
            if cid:
                return cid
        # 兜底：按 token 启发式
        for token in block.split():
            if _looks_like_creator_id(token, require_digit=True):
                return token
        m = re.search(r"[A-Za-z0-9_]{5,}", block)
        if m and _looks_like_creator_id(m.group(0), require_digit=True):
            return m.group(0)
    return None


async def _extract_creator_id_from_drawer(page: Page) -> str | None:
    """打开抖音号抽屉，读取已勾选行的抖音号。"""
    try:
        await _open_creator_drawer(page)
    except Exception:
        return None

    drawer = page.locator(".ovui-drawer").last

    checked_text = await page.evaluate("""() => {
        const drawers = document.querySelectorAll('.ovui-drawer');
        if (drawers.length === 0) return '';
        const last = drawers[drawers.length - 1];
        const rows = last.querySelectorAll('tr, [class*="row"]');
        for (const row of rows) {
            const checkbox = row.querySelector('input[type="checkbox"]');
            if (checkbox && checkbox.checked) {
                return row.innerText.trim();
            }
        }
        return '';
    }""")

    creator_id = ""
    if checked_text:
        # 优先匹配 "ID：xxx"
        for m in re.finditer(r"ID[：:]\s*(\d+)", checked_text):
            cid = _clean_id_number(m.group(1))
            if cid:
                creator_id = cid
                break
        if not creator_id:
            tokens = checked_text.split()
            for token in tokens:
                if _looks_like_creator_id(token, require_digit=False):
                    creator_id = token
                    break
        if not creator_id:
            m = re.search(r"[A-Za-z0-9_]{5,}", checked_text)
            if m and _looks_like_creator_id(m.group(0), require_digit=False):
                creator_id = m.group(0)

    # 关闭抽屉
    close_text = "确定" if creator_id else "取消"
    try:
        close_btn = drawer.get_by_text(close_text, exact=True).first
        if await close_btn.count() and await close_btn.is_enabled():
            await close_btn.click(force=True)
    except Exception:
        pass
    await _wait_stable(page, 1.0)

    return creator_id or None


async def _extract_creator_id_from_body(page: Page, creator_name: str | None) -> str | None:
    """从编辑页 body 文本中，按「达人名 ID：xxx」或「投放抖音号」区块提取抖音号。"""
    body = await _body_text(page)

    # 1. 精确匹配达人名旁边的 ID
    if creator_name:
        for part in creator_name.split("，"):
            part = part.strip()
            if not part:
                continue
            m = re.search(re.escape(part) + r"\s*ID[：:]\s*(\d+)", body)
            if m:
                cid = _clean_id_number(m.group(1))
                if cid:
                    return cid

    # 2. 截取「投放抖音号」区块，找第一个非商品/账号 ID
    section_match = re.search(r"投放抖音号\s*(.*?)\s*(?:管理抖音号|素材|操作|删除)", body, re.S)
    if section_match:
        section = section_match.group(1)
        for m in re.finditer(r"ID[：:]\s*(\d+)", section):
            cid = _clean_id_number(m.group(1))
            if cid:
                return cid

    return None


async def _extract_creator_id(page: Page, creator_name: str | None = None) -> str:
    """先尝试从页面已选区域读取，失败后打开抽屉兜底。"""
    creator_id = await _extract_creator_id_from_element(page, creator_name)
    if creator_id:
        return creator_id

    creator_id = await _extract_creator_id_from_body(page, creator_name)
    if creator_id:
        return creator_id

    creator_id = await _extract_creator_id_from_drawer(page)
    if creator_id:
        return creator_id

    body = await _body_text(page)
    snippet = body[:500].replace("\n", " ")
    raise RetargetError(f"未能从编辑页提取抖音号。页面文本片段: {snippet}")


async def _parse_edit_page(
    page: Page,
    old_plan_name: str,
    requested_department: str,
    fallback_creator_name: str | None,
    fallback_seller: str | None,
) -> OldPlanInfo:
    """在编辑页解析出重建新计划所需的全部信息。"""
    await _wait_stable(page, 2.0)

    department = _department_from_url(page.url) or requested_department
    product_id, product_name = await _extract_product_id(page)

    creator_name, seller = _parse_old_plan_name(old_plan_name)
    if fallback_creator_name:
        creator_name = fallback_creator_name
    if fallback_seller:
        seller = fallback_seller
    creator_id = await _extract_creator_id(page, creator_name)

    # 旧计划 ID：优先从 URL 的 adId 取
    match = re.search(r"adId=(\d+)", page.url)
    old_plan_id = match.group(1) if match else ""
    if not old_plan_id:
        body = await _body_text(page)
        m = re.search(r"ID[：:]?\s*(\d+)", body)
        old_plan_id = m.group(1) if m else ""

    if not creator_id:
        raise RetargetError("未能提取抖音号")
    if not product_id:
        raise RetargetError("未能提取商品 ID")
    if not seller:
        raise RetargetError(
            "未能从旧计划名解析业务员姓氏，请提供 --seller 或 --plan-name"
        )

    return OldPlanInfo(
        department=department,
        seller=seller,
        creator_id=creator_id,
        creator_name=creator_name,
        product_id=product_id,
        product_name=product_name,
        old_plan_id=old_plan_id,
        old_plan_name=old_plan_name,
    )


async def _delete_old_plan(page: Page, plan_row, old_plan_id: str) -> None:
    """在总览页删除旧计划。"""
    delete_btn = plan_row.get_by_text("删除", exact=True).first
    if await delete_btn.count() and await delete_btn.is_visible():
        await delete_btn.click(force=True)
    else:
        icon = plan_row.locator('iconpark-icon[name="oc-icon-delete"]').first
        if await icon.count() and await icon.is_visible():
            await icon.evaluate("el => el.click()")
        else:
            # 尝试「更多」下拉菜单
            more = plan_row.locator('iconpark-icon[name="oc-icon-more"]').first
            if await more.count() and await more.is_visible():
                await more.evaluate("el => el.click()")
                await _wait_stable(page, 0.8)
                delete_opt = page.get_by_text("删除", exact=True).first
                if await delete_opt.count() and await delete_opt.is_visible():
                    await delete_opt.click(force=True)
                else:
                    raise RetargetError("展开更多菜单后未找到删除选项")
            else:
                raise RetargetError("无法找到旧计划的删除按钮")

    # 确认弹窗
    confirm = page.get_by_role("button", name="确定", exact=True).first
    for _ in range(20):
        if await confirm.count() and await confirm.is_visible():
            break
        await _wait_stable(page, 0.3)
    else:
        raise RetargetError("点击删除后未出现确认弹窗")

    await confirm.click(force=True)

    # 等待旧计划行消失
    for _ in range(30):
        rows = page.locator("tr", has_text=old_plan_id)
        visible = False
        for i in range(await rows.count()):
            if await rows.nth(i).is_visible():
                visible = True
                break
        if not visible:
            return
        await _wait_stable(page, 0.5)

    raise RetargetError("旧计划删除后仍在列表中可见")


async def retarget_old_creator(
    old_plan_name: str,
    department: str = DEFAULT_DEPARTMENT,
    publish: bool = False,
    confirm_risky: bool = False,
    confirm_delete: bool = False,
    chase_budget: int = 100,
    chase_hours: float = 24.0,
    creator_name: str | None = None,
    seller: str | None = None,
    new_plan_name: str | None = None,
) -> dict:
    """执行老达人新发视频的完整工作流。"""
    _ensure_cdp()

    async with async_playwright() as pw:
        browser = await connect_browser(pw, timeout=8000)
        context = browser.contexts[0]
        # 复用旧标签可能撞僵尸页（渲染半死时 count/evaluate 永不返回、进程僵死），一律开新页
        page = await open_fresh_page(context)

        # 1. 定位旧计划
        plan_row, old_plan_id = await _search_old_plan(page, old_plan_name, department)
        if not old_plan_id:
            metrics = await _parse_plan_row(plan_row)
            old_plan_id = (metrics.plan_id or "") if metrics else ""

        # 2. 进入编辑页并解析信息
        await _open_edit_page(page, plan_row)
        info = await _parse_edit_page(
            page, old_plan_name, department, creator_name, seller
        )
        if old_plan_id and not info.old_plan_id:
            info.old_plan_id = old_plan_id

        # 3. 未确认删除时，仅返回提取信息
        if not confirm_delete:
            return {
                "status": "extracted",
                "old_plan": {
                    "plan_id": info.old_plan_id,
                    "plan_name": old_plan_name,
                },
                "extracted": asdict(info),
                "message": "已提取旧计划信息，如需删除并重建请添加 --confirm-delete",
            }

        # 4. 回到总览页删除旧计划
        page = await _open_overall(page, info.department)
        plan_row, _ = await _search_old_plan(page, old_plan_name, info.department)
        await _delete_old_plan(page, plan_row, info.old_plan_id)

        # 5. 新建商品乘方计划
        product_key = info.product_name or info.product_id
        request = PlanRequest(
            product_name=product_key,
            creator_id=info.creator_id,
            department=info.department,
            roi=4.5,
            daily_budget=300,
            creator_name=info.creator_name,
            seller=info.seller,
            plan_name=new_plan_name,
            select_first_video=True,
            publish=publish,
            confirm_risky=confirm_risky,
        )
        new_result: WorkflowResult = await create_plan(request)

        # 6. 创建追投
        chase_result = None
        if new_result.status == "publish_confirmed" and new_result.plan_id:
            chase_result = await create_chase(
                main_plan_id=new_result.plan_id,
                department=info.department,
                budget=chase_budget,
                hours=chase_hours,
            )

        return {
            "status": "ok",
            "old_plan": {
                "plan_id": info.old_plan_id,
                "plan_name": old_plan_name,
                "deleted": True,
            },
            "extracted": asdict(info),
            "new_plan": asdict(new_result),
            "chase_task": chase_result,
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="老达人新发视频：删旧计划 → 新建计划 → 追投")
    parser.add_argument("--old-plan-name", required=True, help="旧计划名称")
    parser.add_argument(
        "--department",
        default=DEFAULT_DEPARTMENT,
        choices=tuple(ACCOUNT_BY_DEPARTMENT),
        help="业务部（用于搜索旧计划）",
    )
    parser.add_argument("--publish", action="store_true", help="是否直接发布新计划")
    parser.add_argument(
        "--confirm-risky", action="store_true", help="自动确认平台风险弹窗"
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="确认删除旧计划；不传则只输出提取到的信息",
    )
    parser.add_argument("--chase-budget", type=int, default=100, help="追投预算")
    parser.add_argument("--chase-hours", type=float, default=24.0, help="追投时长（小时）")
    parser.add_argument("--creator-name", help="达人显示名兜底")
    parser.add_argument("--seller", help="业务员姓氏兜底")
    parser.add_argument("--plan-name", help="新计划名称覆盖")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    started = time.monotonic()

    try:
        result = asyncio.run(
            retarget_old_creator(
                old_plan_name=args.old_plan_name,
                department=args.department,
                publish=args.publish,
                confirm_risky=args.confirm_risky,
                confirm_delete=args.confirm_delete,
                chase_budget=args.chase_budget,
                chase_hours=args.chase_hours,
                creator_name=args.creator_name,
                seller=args.seller,
                new_plan_name=args.plan_name,
            )
        )
        elapsed = round(time.monotonic() - started, 2)
        result["elapsed_seconds"] = elapsed
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in ("extracted", "ok") else 1
    except (RetargetError, PlaywrightTimeoutError, ValueError) as exc:
        elapsed = round(time.monotonic() - started, 2)
        print(
            json.dumps(
                {"status": "failed", "message": str(exc), "elapsed_seconds": elapsed},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
