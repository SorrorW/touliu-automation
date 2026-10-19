"""主号关系、执行断点与动作账本。读取不建表；写入和复核快照使用事务。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from .main_products import MAIN_AAVID, WORKFLOW_VERSION, binding_key

DB_FILE = Path(__file__).resolve().parents[1] / "webui/assets/webui.db"
DDL = """
CREATE TABLE IF NOT EXISTS main_product_scans (
 account_id TEXT NOT NULL, product_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 verified_at REAL NOT NULL, row_count INTEGER NOT NULL,
 PRIMARY KEY(account_id,product_id,plan_id));
CREATE TABLE IF NOT EXISTS main_product_bindings (
 account_id TEXT NOT NULL, creator_id TEXT NOT NULL, product_id TEXT NOT NULL,
 plan_id TEXT NOT NULL, creator_name TEXT, bind_type TEXT, bind_status TEXT,
 verified_at REAL NOT NULL,
 PRIMARY KEY(account_id,creator_id,product_id,plan_id));
CREATE TABLE IF NOT EXISTS main_product_workflows (
 account_id TEXT NOT NULL, creator_id TEXT NOT NULL, product_id TEXT NOT NULL,
 plan_id TEXT NOT NULL, version TEXT NOT NULL, progress TEXT NOT NULL,
 detail TEXT, result_json TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(account_id,creator_id,product_id,plan_id));
CREATE TABLE IF NOT EXISTS main_product_record_sync (
 sheet_id TEXT NOT NULL, record_id TEXT NOT NULL, account_id TEXT NOT NULL,
 creator_id TEXT NOT NULL, product_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 version TEXT NOT NULL, progress TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0,
 detail TEXT, updated_at REAL NOT NULL,
 PRIMARY KEY(sheet_id,record_id));
CREATE TABLE IF NOT EXISTS main_product_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
 account_id TEXT NOT NULL, creator_id TEXT NOT NULL, product_id TEXT NOT NULL,
 plan_id TEXT NOT NULL, version TEXT NOT NULL, step TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT);
"""

def read_rows(sql, params=()):
    if not DB_FILE.exists():
        return []
    with closing(sqlite3.connect(DB_FILE.resolve().as_uri()+"?mode=ro", uri=True, timeout=15)) as c:
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise

def connect():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.executescript(DDL)
    return c

def validate_scan(route, rows, complete):
    if not complete:
        raise ValueError("扫描不完整，不得保存缺失结论")
    seen = set()
    for r in rows:
        if (r.get("account_id"), r.get("product_id"), r.get("plan_id")) != (route.account_id, route.product_id, route.plan_id):
            raise ValueError("快照账户/商品/计划不匹配")
        cid = r.get("creator_id")
        if not cid or cid in seen or r.get("bind_status") not in ("在投", "已删除"):
            raise ValueError("快照达人行缺失、重复或状态未知")
        seen.add(cid)

def save_scan(route, rows, *, complete, verified_at=None):
    validate_scan(route, rows, complete)
    stamp = verified_at or time.time()
    with closing(connect()) as c, c:
        c.execute("DELETE FROM main_product_bindings WHERE account_id=? AND product_id=? AND plan_id=?", (route.account_id, route.product_id, route.plan_id))
        c.executemany("INSERT INTO main_product_bindings VALUES (?,?,?,?,?,?,?,?)", [
            (route.account_id, r["creator_id"], route.product_id, route.plan_id,
             r.get("creator_name", ""), r.get("bind_type", ""), r["bind_status"], stamp) for r in rows])
        c.execute("INSERT OR REPLACE INTO main_product_scans VALUES (?,?,?,?,?)", (route.account_id, route.product_id, route.plan_id, stamp, len(rows)))

def load_binding_map():
    rows = read_rows("SELECT * FROM main_product_bindings WHERE bind_status='在投'")
    return {(r['account_id'], r['creator_id'], r['product_id'], r['plan_id']): r for r in rows}

def workflow(creator, route):
    rows = read_rows("SELECT * FROM main_product_workflows WHERE account_id=? AND creator_id=? AND product_id=? AND plan_id=? AND version=?", (*binding_key(creator, route), WORKFLOW_VERSION))
    return json.loads(rows[0]["result_json"]) if rows else None

def event(creator, route, step, status, detail=""):
    with closing(connect()) as c, c:
        c.execute("INSERT INTO main_product_events (created_at,account_id,creator_id,product_id,plan_id,version,step,status,detail) VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), *binding_key(creator, route), WORKFLOW_VERSION, step, status, detail[:1600]))

def save_result(creator, route, result):
    # Cooperation codes and credentials are never persisted in this ledger.
    payload = {k: v for k, v in result.items() if k in ('progress','detail','step','binding','auth','verified_at','outcome')}
    with closing(connect()) as c, c:
        c.execute("INSERT OR REPLACE INTO main_product_workflows VALUES (?,?,?,?,?,?,?,?,?)", (*binding_key(creator, route), WORKFLOW_VERSION, result['progress'], result.get('detail','')[:1600], json.dumps(payload, ensure_ascii=False), time.time()))

def stage_record(sheet_id, rid, creator, route, result):
    with closing(connect()) as c, c:
        c.execute("INSERT OR REPLACE INTO main_product_record_sync VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sheet_id, rid, *binding_key(creator, route), WORKFLOW_VERSION, result['progress'], 0, result.get('detail','')[:1600], time.time()))

def mark_synced(sheet_id, rid):
    with closing(connect()) as c, c:
        c.execute("UPDATE main_product_record_sync SET synced=1,updated_at=? WHERE sheet_id=? AND record_id=?", (time.time(), sheet_id, rid))

def pending_sync(sheet_id):
    return read_rows("SELECT * FROM main_product_record_sync WHERE sheet_id=? AND synced=0 AND version=?", (sheet_id, WORKFLOW_VERSION))
