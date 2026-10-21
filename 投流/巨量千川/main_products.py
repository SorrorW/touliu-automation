"""主号四主品唯一配置源；仅商品 ID 决定路由，不使用部门或商品模糊名称。"""
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit

MAIN_AAVID = "1762338766025739"
MAIN_ACCOUNT = "主号"
WORKFLOW_VERSION = "main-products-v2-materials"

@dataclass(frozen=True)
class MainProduct:
    name: str
    product_id: str
    plan_id: str
    account_id: str = MAIN_AAVID

MAIN_PRODUCTS = (
    MainProduct("肥牛主品", "3625226808611490412", "1839594740761991"),
    MainProduct("牛仔骨", "3634294397346170956", "1876932727172643"),
    MainProduct("上脑", "3735391724588106065", "1876932571532615"),
    MainProduct("牛板腱", "3562896022277644951", "1876929956358355"),
)
MAIN_PLANS = {p.name: p.plan_id for p in MAIN_PRODUCTS}
PRODUCT_BY_ID = {p.product_id: p for p in MAIN_PRODUCTS}
PRODUCT_BY_PLAN = {p.plan_id: p for p in MAIN_PRODUCTS}
PRODUCT_BY_NAME = {p.name: p for p in MAIN_PRODUCTS}

def clean_id(value):
    return re.sub(r"[\s,，]", "", str(value or ""))

def product_route(product_id):
    return PRODUCT_BY_ID.get(clean_id(product_id))

def binding_key(creator_id, route):
    return (route.account_id, clean_id(creator_id), route.product_id, route.plan_id)

def check_identity(url, body, account_id=MAIN_AAVID, plan_id=None):
    query = parse_qs(urlsplit(url).query)
    if query.get("aavid") != [account_id] or "/login" in url:
        raise RuntimeError("平台账号 URL 不符，禁止授权或绑定")
    ids = re.findall(r"ID[:：]\s*([0-9A-Za-z_]+)", body[:1800])
    if not ids or ids[0] != account_id:
        raise RuntimeError("平台当前账号 ID 未核实，禁止授权或绑定")
    if plan_id and (query.get("adId") != [plan_id] or plan_id not in ids[1:]):
        raise RuntimeError("平台计划 ID 未核实，禁止提交")

async def verify_account(page, account_id=MAIN_AAVID, plan_id=None):
    body = await page.locator("body").inner_text()
    check_identity(page.url, body, account_id, plan_id)
    return body
