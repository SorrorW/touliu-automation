"""SQLite 持久化：最近 7 天的任务记录 + 最近一次监控扫描结果。

数据库文件：webui/assets/webui.db（标准库 sqlite3，无新依赖）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any

from 巨量千川.lifecycle import NEW_PLAN_ROI_TARGET, classify_plan_lifecycle, is_new_plan_roi

DB_FILE = Path(__file__).with_name("assets") / "webui.db"
KEEP_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    submitted_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    result_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    department TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_deps (
    department TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bigwatch_evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id TEXT NOT NULL,
    department TEXT NOT NULL,
    creator_name TEXT,
    fields_json TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_id TEXT,
    auth_status TEXT,
    authorized INTEGER,
    message TEXT,
    updated_at REAL NOT NULL,
    UNIQUE(creator_id, department)
);
CREATE TABLE IF NOT EXISTS plan_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_record_id TEXT NOT NULL UNIQUE,
    source_platform TEXT NOT NULL DEFAULT 'wecom_douyin',
    creator_id TEXT NOT NULL,
    department TEXT NOT NULL,
    product TEXT,
    seller TEXT,
    task_status TEXT NOT NULL,
    plan_label TEXT,
    plan_id TEXT,
    plan_name TEXT,
    latest_stage TEXT,
    latest_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registry_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    FOREIGN KEY(registry_id) REFERENCES plan_registry(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_registry_updated ON plan_registry(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_events_registry ON plan_events(registry_id, created_at DESC);
CREATE TABLE IF NOT EXISTS plan_catalog (
    plan_id TEXT PRIMARY KEY,
    department TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    creator_name TEXT,
    plan_status TEXT,
    delivery_type TEXT,
    budget REAL,
    spent REAL,
    roi REAL,
    orders INTEGER,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    net_deal_amount REAL,
    plan_date TEXT,
    category TEXT NOT NULL,
    category_reason TEXT NOT NULL,
    manual_label TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    scanned_at REAL NOT NULL,
    plan_status TEXT,
    budget REAL,
    spent REAL,
    roi REAL,
    orders INTEGER,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    net_deal_amount REAL,
    FOREIGN KEY(plan_id) REFERENCES plan_catalog(plan_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_catalog_seen ON plan_catalog(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_catalog_category ON plan_catalog(category);
CREATE INDEX IF NOT EXISTS idx_plan_snapshots_plan ON plan_metric_snapshots(plan_id, scanned_at DESC);
"""

# 计划每日标签：每次扫描（12:00/17:00/23:00/手动 monitor）的分类结果按日留痕（2026-08-24）。
# 独立常量：record_plan_daily_labels 在 init_db 未跑过的进程里用它兜底建表。
_PLAN_DAILY_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS plan_daily_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    label_date TEXT NOT NULL,
    scan_slot TEXT NOT NULL,
    department TEXT,
    plan_name TEXT,
    category TEXT NOT NULL,
    reason TEXT,
    roi REAL,
    orders INTEGER,
    spent REAL,
    roi_target REAL,
    daily_budget REAL,
    plan_status TEXT,
    scanned_at REAL NOT NULL,
    UNIQUE(plan_id, label_date, scan_slot)
);
CREATE INDEX IF NOT EXISTS idx_plan_daily_labels_date ON plan_daily_labels(label_date, category);
"""
_SCHEMA += _PLAN_DAILY_LABELS_DDL

# 计划每日看板：一行 = 一个计划一天，四个扫描档位各一列标签，到点覆盖当天列（2026-08-24）。
# 直观呈现一个计划当日的分类流转轨迹；换日即新行，天然保留每日变化历史。
_PLAN_DAILY_BOARD_DDL = """
CREATE TABLE IF NOT EXISTS plan_daily_board (
    plan_id TEXT NOT NULL,
    label_date TEXT NOT NULL,
    plan_name TEXT,
    department TEXT,
    roi_target REAL,
    daily_budget REAL,
    label_08 TEXT,
    label_12 TEXT,
    label_17 TEXT,
    label_23 TEXT,
    last_roi REAL,
    last_orders INTEGER,
    last_spent REAL,
    last_status TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (plan_id, label_date)
);
CREATE INDEX IF NOT EXISTS idx_plan_daily_board_date ON plan_daily_board(label_date, department);
"""
_SCHEMA += _PLAN_DAILY_BOARD_DDL

# 主号四大主品「自动拉取达人」快照（2026-10-13 需求：每日 09:10/13:30/17:30 三跑，
# 播报新增/删除达人）。snap_date 列存 'YYYY-MM-DD HH:MM'（ISO 字典序可比），
# 一天多快照；diff 永远对「上一张快照」。
_MAIN_PULL_DDL = """
CREATE TABLE IF NOT EXISTS main_pull_snapshots (
    snap_date TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    plan_key TEXT NOT NULL,
    creator_name TEXT,
    bind_type TEXT,
    bind_status TEXT,
    dept TEXT,
    seller TEXT,
    PRIMARY KEY (snap_date, creator_id, plan_key)
);
CREATE INDEX IF NOT EXISTS idx_main_pull_date ON main_pull_snapshots(snap_date);
"""
_SCHEMA += _MAIN_PULL_DDL


def save_main_pull_snapshot(rows: list[dict], snap_date: str) -> int:
    """写入一张主号达人绑定快照（同 snap_date 重复跑=覆盖）。返回写入条数。
    snap_date 为 'YYYY-MM-DD HH:MM'（2026-10-13 起一天三跑，时间戳级）。"""
    with _connect() as conn:
        conn.executescript(_MAIN_PULL_DDL)
        conn.execute("DELETE FROM main_pull_snapshots WHERE snap_date=?", (snap_date,))
        for r in rows:
            conn.execute(
                """INSERT INTO main_pull_snapshots
                   (snap_date, creator_id, plan_key, creator_name, bind_type,
                    bind_status, dept, seller)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (snap_date, str(r.get("creator_id") or ""), str(r.get("plan_key") or ""),
                 str(r.get("creator_name") or ""), str(r.get("bind_type") or ""),
                 str(r.get("bind_status") or ""), str(r.get("dept") or ""),
                 str(r.get("seller") or "")),
            )
    return len(rows)


def load_main_pull_snapshot(snap_date: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        conn.executescript(_MAIN_PULL_DDL)
        rows = conn.execute(
            "SELECT * FROM main_pull_snapshots WHERE snap_date=?", (snap_date,)
        ).fetchall()
    return [dict(r) for r in rows]


def latest_main_pull_date_before(snap_date: str) -> str | None:
    with _connect() as conn:
        conn.executescript(_MAIN_PULL_DDL)
        row = conn.execute(
            "SELECT MAX(snap_date) d FROM main_pull_snapshots WHERE snap_date < ?",
            (snap_date,)).fetchone()
    return row["d"] if row and row["d"] else None


def get_main_pull_binding_map():
    """有效关系按 (主号,达人,商品,计划) 索引，绝不跨商品折叠。"""
    from 巨量千川.main_product_store import load_binding_map
    return load_binding_map()


def get_main_pull_alive_ids(product_id: str) -> tuple[set[str], str | None]:
    """兼容查询必须显式指定主品，禁止跨商品使用达人集合。"""
    alive, stamp=get_main_pull_alive_map(product_id)
    return set(alive),stamp


def get_main_pull_alive_map(product_id: str) -> tuple[dict[str, dict], str | None]:
    """单个主品的有效关系；完整四维索引使用 get_main_pull_binding_map。"""
    from 巨量千川.main_products import product_route
    from datetime import datetime
    route=product_route(product_id)
    if route is None:
        raise ValueError("必须提供四主品商品ID，不能仅按达人判定")
    rows=[r for key,r in get_main_pull_binding_map().items()
          if (key[0],key[2],key[3])==(route.account_id,route.product_id,route.plan_id)]
    stamp=max((r["verified_at"] for r in rows),default=0)
    return ({r["creator_id"]:{**r,"name":r.get("creator_name", ""),"plan_key":route.name}
             for r in rows},datetime.fromtimestamp(stamp).isoformat(timespec="seconds") if stamp else None)

# 扫描档位 → 看板列。非整点档位（手动扫描）按时间归入当前档：05-11点→08，
# 11-15点→12，15-21点→17，其余→23，与定时动作的执行时刻对齐。
_BOARD_SLOT_COLUMNS = {"08:00": "label_08", "12:00": "label_12",
                       "17:00": "label_17", "23:00": "label_23"}


def board_slot_column(scan_slot: str) -> str:
    """扫描档位归一化为看板列名；无法解析的档位不归板。"""
    slot = (scan_slot or "").strip()
    if slot in _BOARD_SLOT_COLUMNS:
        return _BOARD_SLOT_COLUMNS[slot]
    try:
        hour = int(slot.split(":")[0])
    except (ValueError, IndexError):
        return ""
    if 5 <= hour < 11:
        return "label_08"
    if 11 <= hour < 15:
        return "label_12"
    if 15 <= hour < 21:
        return "label_17"
    return "label_23"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表 + 清理 7 天前的任务记录。应用启动时调用。"""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # 老库迁移：补 started_at 列（拿到浏览器锁、真正开始执行的时间）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "started_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")
        # 老库迁移：补 auth_checked_at 列（待建计划最近一次授权查询时间）
        todo_cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_todos)").fetchall()}
        if "auth_checked_at" not in todo_cols:
            conn.execute("ALTER TABLE plan_todos ADD COLUMN auth_checked_at REAL")
        catalog_cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_catalog)").fetchall()}
        if "plan_date" not in catalog_cols:
            conn.execute("ALTER TABLE plan_catalog ADD COLUMN plan_date TEXT")
        # 老库迁移：补 roi_target 列（对账引擎核对参数漂移，2026-08-24）
        if "roi_target" not in catalog_cols:
            conn.execute("ALTER TABLE plan_catalog ADD COLUMN roi_target REAL")
        conn.execute(
            "DELETE FROM jobs WHERE submitted_at < ?",
            (time.time() - KEEP_DAYS * 86400,),
        )
        # 上次进程退出时留下的排队/执行中任务已不会继续，统一标记为已终止
        conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE status IN ('queued','running')",
            (time.time(),),
        )
        # 一次性迁移：scan_deps 为空时，把旧 scans 表最近一条按部门灌入（保留原时间戳）
        if conn.execute("SELECT COUNT(*) AS c FROM scan_deps").fetchone()["c"] == 0:
            latest = conn.execute(
                "SELECT created_at, report_json FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest:
                try:
                    old_report = json.loads(latest["report_json"])
                except json.JSONDecodeError:
                    old_report = {}
                for dep, data in (old_report.items() if isinstance(old_report, dict) else []):
                    if isinstance(data, dict) and not data.get("error"):
                        conn.execute(
                            "INSERT OR IGNORE INTO scan_deps (department, created_at, report_json) VALUES (?, ?, ?)",
                            (dep, latest["created_at"], json.dumps(data, ensure_ascii=False)),
                        )


# ---------------------------------------------------------------------------
# 任务记录
# ---------------------------------------------------------------------------


def insert_job(name: str, submitted_at: float) -> int:
    """登记新任务（queued，拿到浏览器锁后转 running），返回自增 job_id。"""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (name, status, submitted_at) VALUES (?, 'queued', ?)",
            (name, submitted_at),
        )
        return int(cur.lastrowid)


def mark_job_running(job_id: int, started_at: float) -> None:
    """任务拿到浏览器锁、真正开始执行：queued → running 并记录开始时间。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (started_at, job_id),
        )


def update_job(
    job_id: int,
    *,
    status: str,
    finished_at: float,
    result: Any = None,
    error: str | None = None,
) -> None:
    result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, finished_at=?, result_json=?, error=? WHERE id=?",
            (status, finished_at, result_json, error, job_id),
        )


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": result,
        "error": row["error"],
    }


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    """最近 N 条任务（新→旧）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def get_job(job_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


# ---------------------------------------------------------------------------
# 扫描结果
# ---------------------------------------------------------------------------


def save_scan(department: str, report: Any) -> None:
    """按部门分别保存扫描结果；扫描失败的部门不覆盖旧数据。"""
    if not isinstance(report, dict):
        return
    now = time.time()
    with _connect() as conn:
        for dep, data in report.items():
            if not isinstance(data, dict) or data.get("error"):
                continue
            conn.execute(
                """INSERT INTO scan_deps (department, created_at, report_json) VALUES (?, ?, ?)
                   ON CONFLICT(department) DO UPDATE SET
                     created_at=excluded.created_at, report_json=excluded.report_json""",
                (dep, now, json.dumps(data, ensure_ascii=False)),
            )


def load_last_scan() -> dict[str, Any] | None:
    """按部门合并的最近扫描：{departments: {dep: {data, scanned_at}}}；无记录返回 None。"""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM scan_deps").fetchall()
    if not rows:
        return None
    return {
        "departments": {
            row["department"]: {
                "data": json.loads(row["report_json"]),
                "scanned_at": row["created_at"],
            }
            for row in rows
        }
    }


# ---------------------------------------------------------------------------
# 大达人评估结果
# ---------------------------------------------------------------------------


def save_eval(report: Any) -> None:
    """保存一次大达人评估结果。"""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO bigwatch_evals (created_at, report_json) VALUES (?, ?)",
            (time.time(), json.dumps(report, ensure_ascii=False)),
        )


def load_last_eval() -> dict[str, Any] | None:
    """最近一次大达人评估：{evaluated_at, report}；无记录返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM bigwatch_evals ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "evaluated_at": row["created_at"],
        "report": json.loads(row["report_json"]),
    }


# ---------------------------------------------------------------------------
# 待建计划（建计划流程未完成的达人，右侧面板数据源）
# ---------------------------------------------------------------------------


def _row_to_todo(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "creator_id": row["creator_id"],
        "department": row["department"],
        "creator_name": row["creator_name"],
        "fields": json.loads(row["fields_json"]),
        "status": row["status"],
        "plan_id": row["plan_id"],
        "auth_status": row["auth_status"],
        "authorized": bool(row["authorized"]) if row["authorized"] is not None else None,
        "message": row["message"],
        "updated_at": row["updated_at"],
        "auth_checked_at": row["auth_checked_at"],
    }


def upsert_plan_todo(
    *,
    creator_id: str,
    department: str,
    creator_name: str | None,
    fields: dict,
    status: str,
    plan_id: str | None = None,
    message: str | None = None,
) -> int:
    """按 (creator_id, department) 去重写入/更新待建计划条目，返回 id。

    auth_status/authorized 不在此更新（由授权查询单独维护），upsert 时保留原值。
    """
    with _connect() as conn:
        conn.execute(
            """INSERT INTO plan_todos
               (creator_id, department, creator_name, fields_json, status, plan_id, message, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(creator_id, department) DO UPDATE SET
                 creator_name=excluded.creator_name,
                 fields_json=excluded.fields_json,
                 status=excluded.status,
                 plan_id=excluded.plan_id,
                 message=excluded.message,
                 updated_at=excluded.updated_at""",
            (
                creator_id,
                department,
                creator_name,
                json.dumps(fields, ensure_ascii=False),
                status,
                plan_id,
                message,
                time.time(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM plan_todos WHERE creator_id=? AND department=?",
            (creator_id, department),
        ).fetchone()
        return int(row["id"])


def list_plan_todos(include_done: bool = False) -> list[dict[str, Any]]:
    """待建计划列表（默认不含 done），最近更新在前。"""
    sql = "SELECT * FROM plan_todos"
    if not include_done:
        sql += " WHERE status != 'done'"
    sql += " ORDER BY updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_todo(r) for r in rows]


def get_plan_todo(todo_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM plan_todos WHERE id=?", (todo_id,)).fetchone()
    return _row_to_todo(row) if row else None


def update_plan_todo(todo_id: int, **fields: Any) -> None:
    """更新指定列（status/plan_id/auth_status/authorized/message）。"""
    allowed = {"status", "plan_id", "auth_status", "authorized", "message", "creator_name", "auth_checked_at"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"不允许更新的列: {key}")
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.append(time.time())
    values.append(todo_id)
    with _connect() as conn:
        conn.execute(f"UPDATE plan_todos SET {', '.join(sets)} WHERE id=?", values)


def delete_plan_todo(todo_id: int) -> bool:
    """删除条目；不存在返回 False。"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM plan_todos WHERE id=?", (todo_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 企微投流计划台账（自动化状态不占用业务员备注）
# ---------------------------------------------------------------------------


def upsert_plan_registry(
    *,
    source_record_id: str,
    fields: dict[str, Any],
    task_status: str,
    stage: str,
    message: str = "",
    plan_label: str | None = None,
    plan_id: str | None = None,
    plan_name: str | None = None,
    event_status: str = "info",
) -> int:
    """登记企微任务及其最新自动化状态，并追加一条可追溯事件。

    企业微信只保留业务员原始字段；授权、建计划和计划分类全部落在本地台账。
    """
    now = time.time()
    creator = str(fields.get("抖音号") or "").strip()
    department = str(fields.get("业务部") or "").strip()
    product = str(fields.get("商品ID") or "").strip()
    seller = str(fields.get("业务员") or "").strip()
    metadata = json.dumps(fields, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO plan_registry
               (source_record_id, creator_id, department, product, seller, task_status,
                plan_label, plan_id, plan_name, latest_stage, latest_message,
                metadata_json, first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_record_id) DO UPDATE SET
                 creator_id=excluded.creator_id,
                 department=excluded.department,
                 product=excluded.product,
                 seller=excluded.seller,
                 task_status=excluded.task_status,
                 plan_label=COALESCE(excluded.plan_label, plan_registry.plan_label),
                 plan_id=COALESCE(excluded.plan_id, plan_registry.plan_id),
                 plan_name=COALESCE(excluded.plan_name, plan_registry.plan_name),
                 latest_stage=excluded.latest_stage,
                 latest_message=excluded.latest_message,
                 metadata_json=excluded.metadata_json,
                 updated_at=excluded.updated_at""",
            (
                source_record_id, creator, department, product, seller, task_status,
                plan_label, plan_id, plan_name, stage, message[:500], metadata, now, now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM plan_registry WHERE source_record_id=?", (source_record_id,)
        ).fetchone()
        registry_id = int(row["id"])
        conn.execute(
            """INSERT INTO plan_events (registry_id, created_at, stage, status, message)
               VALUES (?, ?, ?, ?, ?)""",
            (registry_id, now, stage, event_status, message[:500]),
        )
    return registry_id


def _row_to_plan_registry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_record_id": row["source_record_id"],
        "source_platform": row["source_platform"],
        "creator_id": row["creator_id"],
        "department": row["department"],
        "product": row["product"],
        "seller": row["seller"],
        "task_status": row["task_status"],
        "plan_label": row["plan_label"],
        "plan_id": row["plan_id"],
        "plan_name": row["plan_name"],
        "latest_stage": row["latest_stage"],
        "latest_message": row["latest_message"],
        "metadata": json.loads(row["metadata_json"]),
        "first_seen_at": row["first_seen_at"],
        "updated_at": row["updated_at"],
    }


def list_plan_registry(limit: int = 100) -> list[dict[str, Any]]:
    """返回最近更新的企微投流台账，供桌面端和故障排查使用。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM plan_registry ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_plan_registry(row) for row in rows]


def get_plan_registry_events(source_record_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT e.created_at, e.stage, e.status, e.message
               FROM plan_events e
               JOIN plan_registry r ON r.id=e.registry_id
               WHERE r.source_record_id=?
               ORDER BY e.id DESC LIMIT ?""",
            (source_record_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 全量计划库（千川扫描结果，按 plan_id 去重）
# ---------------------------------------------------------------------------


_CATEGORY_NAMES = {
    "new_plan": "新计划测试",
    "old_pool": "老计划池",
    "key_plan": "重点计划",
    "potential_plan": "潜力计划",
    "manual_review": "待人工判定",
    "invalid_plan": "无效计划",
}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _plan_date_from_name(plan_name: str) -> str | None:
    """从常见计划名前缀解析 M/D 或 MMDD；无法确认时返回 None。"""
    name = (plan_name or "").strip()
    match = re.match(r"^(\d{1,2})\s*[._/-]\s*(\d{1,2})(?:\D|$)", name)
    if not match:
        match = re.match(r"^(\d{2})(\d{2})(?:[_-]|\D)", name)
    if not match:
        match = re.match(r"^(\d)(\d{2})(?:[_-]|\D)", name)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    try:
        return date(date.today().year, month, day).isoformat()
    except ValueError:
        return None


def _classify_plan(plan: dict[str, Any], plan_date: str | None, manual_label: str | None) -> tuple[str, str]:
    """按当前业务规则归类，不产生暂停、启用或追投动作。"""
    roi = _number(plan.get("roi"))
    roi_target = _number(plan.get("roi_target"))
    orders = plan.get("orders")
    orders = int(orders) if isinstance(orders, (int, float)) else None
    name = str(plan.get("plan_name") or "")
    # ROI 4.5 代表仍按新计划（也包括老计划新素材）测试。名称日期只作为
    # 兼容来源，不能让跨日的 ROI 4.5 测试计划被提前归入老计划池。
    # 2026-10-09：旧素材通道初始 ROI 6.5 同样按新计划节奏（is_new_plan_roi 认 4.5/6.5）。
    is_test_new = (
        plan_date == date.today().isoformat()
        or (roi_target is not None and is_new_plan_roi(roi_target))
    )
    decision = classify_plan_lifecycle(
        roi=roi,
        orders=orders,
        is_today_new=is_test_new,
        manual_label=manual_label,
        name_label=name,
    )
    return decision.category, decision.reason


def sync_plan_catalog(report: dict[str, Any]) -> int:
    """将一次全量扫描写入计划库和指标快照，返回同步的计划数量。"""
    if not isinstance(report, dict):
        return 0
    now = time.time()
    synced = 0
    with _connect() as conn:
        for department, payload in report.items():
            if not isinstance(payload, dict) or payload.get("error"):
                continue
            for plan in payload.get("plans") or []:
                plan_id = str(plan.get("plan_id") or "").strip()
                if not plan_id:
                    continue
                existing = conn.execute(
                    "SELECT first_seen_at, manual_label FROM plan_catalog WHERE plan_id=?", (plan_id,)
                ).fetchone()
                first_seen_at = float(existing["first_seen_at"]) if existing else now
                manual_label = existing["manual_label"] if existing else None
                plan_date = _plan_date_from_name(str(plan.get("plan_name") or ""))
                category, reason = _classify_plan(plan, plan_date, manual_label)
                values = (
                    plan_id, department, str(plan.get("plan_name") or ""),
                    plan.get("creator_name"), plan.get("status"), plan.get("delivery_type"),
                    _number(plan.get("budget")), _number(plan.get("spent")), _number(plan.get("roi")),
                    int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                    int(plan.get("impression_count") or 0), int(plan.get("click_count") or 0),
                    _number(plan.get("net_deal_amount")), plan_date, category, reason, first_seen_at, now,
                )
                conn.execute(
                    """INSERT INTO plan_catalog
                       (plan_id, department, plan_name, creator_name, plan_status, delivery_type,
                        budget, spent, roi, orders, impressions, clicks, net_deal_amount, plan_date,
                        category, category_reason, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(plan_id) DO UPDATE SET
                         department=excluded.department, plan_name=excluded.plan_name,
                         creator_name=excluded.creator_name, plan_status=excluded.plan_status,
                         delivery_type=excluded.delivery_type, budget=excluded.budget,
                         spent=excluded.spent, roi=excluded.roi, orders=excluded.orders,
                         impressions=excluded.impressions, clicks=excluded.clicks,
                         net_deal_amount=excluded.net_deal_amount, plan_date=excluded.plan_date,
                         category=excluded.category,
                         category_reason=excluded.category_reason, last_seen_at=excluded.last_seen_at""",
                    values,
                )
                conn.execute(
                    """INSERT INTO plan_metric_snapshots
                       (plan_id, scanned_at, plan_status, budget, spent, roi, orders, impressions, clicks, net_deal_amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (plan_id, now, plan.get("status"), _number(plan.get("budget")),
                     _number(plan.get("spent")), _number(plan.get("roi")),
                     int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                     int(plan.get("impression_count") or 0), int(plan.get("click_count") or 0),
                     _number(plan.get("net_deal_amount"))),
                )
                synced += 1
    return synced


def upsert_plan_from_poll(
    plan: dict[str, Any],
    *,
    department: str = "",
    creator_name: str = "",
    source: str = "wecom_poll",
) -> bool:
    """将企微轮询发现/创建/更新的计划增量写入 ``plan_catalog``。

    轮询拿到的字段通常是不完整的（例如旧计划扫描只有计划名、ID、消耗和
    展示数），因此只覆盖本次明确提供的字段，保留数据库已有的 ROI、订单、
    预算等监控指标，避免用 ``None`` 把事实数据抹掉。
    """
    if not isinstance(plan, dict):
        return False
    plan_id = str(plan.get("plan_id") or "").strip()
    if not plan_id:
        return False

    def _present(*keys: str) -> bool:
        return any(key in plan and plan.get(key) is not None for key in keys)

    def _text(current: Any, *keys: str) -> str | None:
        for key in keys:
            if key in plan and plan.get(key) is not None:
                value = str(plan.get(key) or "").strip()
                return value or None
        return current

    def _number_or_current(current: Any, *keys: str) -> float | None:
        for key in keys:
            if key in plan and plan.get(key) is not None:
                return _number(plan.get(key))
        return _number(current)

    def _int_or_current(current: Any, *keys: str) -> int | None:
        for key in keys:
            if key in plan and plan.get(key) is not None:
                value = plan.get(key)
                return int(value) if isinstance(value, (int, float)) else None
        return int(current) if isinstance(current, (int, float)) else None

    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM plan_catalog WHERE plan_id=?", (plan_id,)
        ).fetchone()
        first_seen_at = float(existing["first_seen_at"]) if existing else now
        manual_label = existing["manual_label"] if existing else None

        merged = {
            "department": (
                str(department or "").strip()
                or (str(existing["department"] or "").strip() if existing else "")
            ),
            "plan_name": _text(existing["plan_name"] if existing else None, "plan_name") or "",
            "creator_name": _text(
                existing["creator_name"] if existing else None, "creator_name"
            ),
            "status": _text(existing["plan_status"] if existing else None, "status", "plan_status"),
            "delivery_type": _text(
                existing["delivery_type"] if existing else None, "delivery_type"
            ),
            "budget": _number_or_current(existing["budget"] if existing else None, "budget"),
            "spent": _number_or_current(existing["spent"] if existing else None, "spent"),
            "roi": _number_or_current(existing["roi"] if existing else None, "roi"),
            "orders": _int_or_current(existing["orders"] if existing else None, "orders"),
            "impressions": _int_or_current(
                existing["impressions"] if existing else None,
                "impressions", "impression_count",
            ) or 0,
            "clicks": _int_or_current(
                existing["clicks"] if existing else None,
                "clicks", "click_count",
            ) or 0,
            "net_deal_amount": _number_or_current(
                existing["net_deal_amount"] if existing else None, "net_deal_amount"
            ),
        }
        if creator_name:
            merged["creator_name"] = str(creator_name).strip()
        plan_date = _plan_date_from_name(merged["plan_name"])
        category, reason = _classify_plan(merged, plan_date, manual_label)
        if plan.get("category"):
            category = str(plan["category"])
        if plan.get("category_reason"):
            reason = str(plan["category_reason"])

        conn.execute(
            """INSERT INTO plan_catalog
               (plan_id, department, plan_name, creator_name, plan_status, delivery_type,
                budget, spent, roi, orders, impressions, clicks, net_deal_amount, plan_date,
                category, category_reason, manual_label, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(plan_id) DO UPDATE SET
                 department=excluded.department, plan_name=excluded.plan_name,
                 creator_name=COALESCE(excluded.creator_name, plan_catalog.creator_name),
                 plan_status=COALESCE(excluded.plan_status, plan_catalog.plan_status),
                 delivery_type=COALESCE(excluded.delivery_type, plan_catalog.delivery_type),
                 budget=COALESCE(excluded.budget, plan_catalog.budget),
                 spent=COALESCE(excluded.spent, plan_catalog.spent),
                 roi=COALESCE(excluded.roi, plan_catalog.roi),
                 orders=COALESCE(excluded.orders, plan_catalog.orders),
                 impressions=excluded.impressions, clicks=excluded.clicks,
                 net_deal_amount=COALESCE(excluded.net_deal_amount, plan_catalog.net_deal_amount),
                 plan_date=excluded.plan_date, category=excluded.category,
                 category_reason=excluded.category_reason,
                 manual_label=COALESCE(excluded.manual_label, plan_catalog.manual_label),
                 last_seen_at=excluded.last_seen_at""",
            (
                plan_id, merged["department"], merged["plan_name"], merged["creator_name"],
                merged["status"], merged["delivery_type"], merged["budget"], merged["spent"],
                merged["roi"], merged["orders"], merged["impressions"], merged["clicks"],
                merged["net_deal_amount"], plan_date, category, reason, manual_label,
                first_seen_at, now,
            ),
        )

        # 只有轮询确实带回指标时才留快照；纯建计划事件不制造空指标快照。
        metric_keys = (
            "status", "budget", "spent", "roi", "orders", "impressions",
            "impression_count", "clicks", "click_count", "net_deal_amount",
        )
        if _present(*metric_keys):
            conn.execute(
                """INSERT INTO plan_metric_snapshots
                   (plan_id, scanned_at, plan_status, budget, spent, roi, orders,
                    impressions, clicks, net_deal_amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, now, merged["status"], merged["budget"], merged["spent"],
                    merged["roi"], merged["orders"], merged["impressions"],
                    merged["clicks"], merged["net_deal_amount"],
                ),
            )
    return True


def mark_plan_deleted_from_poll(
    *, plan_id: str = "", plan_name: str = "", department: str = ""
) -> bool:
    """保留被轮询删除的老计划历史，但把当前状态同步为「已删除」。"""
    plan_id = str(plan_id or "").strip()
    plan_name = str(plan_name or "").strip()
    if not plan_id and not plan_name:
        return False
    now = time.time()
    with _connect() as conn:
        if plan_id:
            cur = conn.execute(
                """UPDATE plan_catalog
                   SET plan_status='已删除', last_seen_at=?
                   WHERE plan_id=?""",
                (now, plan_id),
            )
        else:
            cur = conn.execute(
                """UPDATE plan_catalog
                   SET plan_status='已删除', last_seen_at=?
                   WHERE plan_name=? AND (?='' OR department=?)""",
                (now, plan_name, department, department),
            )
    return bool(cur.rowcount)


def sync_lifecycle_catalog(report: dict[str, Any]) -> int:
    """把 plan_power.monitor 的分类结果原样写入计划库（含 roi_target 与快照）。

    与 sync_plan_catalog 的区别：category/reason 直接用生命周期模块的判定，
    不在 webui 侧重新归类，保证数据库与自动化执行口径一致。
    """
    if not isinstance(report, dict):
        return 0
    now = time.time()
    synced = 0
    with _connect() as conn:
        for department, payload in report.items():
            # plan_power.monitor 的 plans 值是 list（webui 扫描是 {"plans": [...]} 字典），两种都收
            if isinstance(payload, dict):
                if payload.get("error"):
                    continue
                plans = payload.get("plans") or []
            elif isinstance(payload, list):
                plans = payload
            else:
                continue
            for plan in plans:
                plan_id = str(plan.get("plan_id") or "").strip()
                if not plan_id:
                    continue
                existing = conn.execute(
                    "SELECT first_seen_at FROM plan_catalog WHERE plan_id=?", (plan_id,)
                ).fetchone()
                first_seen_at = float(existing["first_seen_at"]) if existing else now
                plan_date = _plan_date_from_name(str(plan.get("plan_name") or ""))
                values = (
                    plan_id, department, str(plan.get("plan_name") or ""),
                    None, plan.get("status"), None,
                    None, _number(plan.get("spent")), _number(plan.get("roi")),
                    int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                    plan_date,
                    str(plan.get("category") or ""), str(plan.get("reason") or ""),
                    _number(plan.get("roi_target")), first_seen_at, now,
                )
                conn.execute(
                    """INSERT INTO plan_catalog
                       (plan_id, department, plan_name, creator_name, plan_status, delivery_type,
                        budget, spent, roi, orders, plan_date,
                        category, category_reason, roi_target, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(plan_id) DO UPDATE SET
                         department=excluded.department, plan_name=excluded.plan_name,
                         plan_status=excluded.plan_status,
                         spent=excluded.spent, roi=excluded.roi, orders=excluded.orders,
                         plan_date=excluded.plan_date,
                         category=excluded.category, category_reason=excluded.category_reason,
                         roi_target=excluded.roi_target, last_seen_at=excluded.last_seen_at""",
                    values,
                )
                conn.execute(
                    """INSERT INTO plan_metric_snapshots
                       (plan_id, scanned_at, plan_status, budget, spent, roi, orders, impressions, clicks, net_deal_amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (plan_id, now, plan.get("status"), None,
                     _number(plan.get("spent")), _number(plan.get("roi")),
                     int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                     0, 0, None),
                )
                synced += 1
    return synced


def record_plan_daily_labels(report: dict[str, Any], *, scan_slot: str,
                             label_date: str | None = None) -> int:
    """把一次扫描的分类标签按日留痕（2026-08-24 起：每次扫描时分类落库）。

    入参与 sync_lifecycle_catalog 同为 monitor 风格 report（department → 条目 list
    或 {"plans": [...]}）；同 plan_id + label_date + scan_slot 重跑覆盖，不堆重复行。
    """
    if not isinstance(report, dict) or not scan_slot:
        return 0
    day = label_date or date.today().isoformat()
    now = time.time()
    recorded = 0
    with _connect() as conn:
        # plan_power 独立进程调用时 init_db 未必跑过，先确保表存在（幂等）
        conn.executescript(_PLAN_DAILY_LABELS_DDL)
        conn.executescript(_PLAN_DAILY_BOARD_DDL)
        board_col = board_slot_column(scan_slot)
        for department, payload in report.items():
            if isinstance(payload, dict):
                if payload.get("error"):
                    continue
                plans = payload.get("plans") or []
            elif isinstance(payload, list):
                plans = payload
            else:
                continue
            for plan in plans:
                plan_id = str(plan.get("plan_id") or "").strip()
                category = str(plan.get("category") or "").strip()
                if not plan_id or not category:
                    continue
                conn.execute(
                    """INSERT INTO plan_daily_labels
                       (plan_id, label_date, scan_slot, department, plan_name,
                        category, reason, roi, orders, spent,
                        roi_target, daily_budget, plan_status, scanned_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(plan_id, label_date, scan_slot) DO UPDATE SET
                         department=excluded.department, plan_name=excluded.plan_name,
                         category=excluded.category, reason=excluded.reason,
                         roi=excluded.roi, orders=excluded.orders, spent=excluded.spent,
                         roi_target=excluded.roi_target, daily_budget=excluded.daily_budget,
                         plan_status=excluded.plan_status, scanned_at=excluded.scanned_at""",
                    (plan_id, day, scan_slot,
                     str(plan.get("department") or department),
                     str(plan.get("plan_name") or ""), category,
                     str(plan.get("reason") or ""),
                     _number(plan.get("roi")),
                     int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                     _number(plan.get("spent")),
                     _number(plan.get("roi_target")),
                     _number(plan.get("daily_budget")),
                     plan.get("status"), now),
                )
                recorded += 1
                if board_col:
                    # 每日看板：覆盖当天该档位标签列，同时刷新最新指标与参数
                    conn.execute(
                        f"""INSERT INTO plan_daily_board
                           (plan_id, label_date, plan_name, department,
                            roi_target, daily_budget, {board_col},
                            last_roi, last_orders, last_spent, last_status, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(plan_id, label_date) DO UPDATE SET
                             plan_name=excluded.plan_name, department=excluded.department,
                             roi_target=excluded.roi_target, daily_budget=excluded.daily_budget,
                             {board_col}=excluded.{board_col},
                             last_roi=excluded.last_roi, last_orders=excluded.last_orders,
                             last_spent=excluded.last_spent, last_status=excluded.last_status,
                             updated_at=excluded.updated_at""",
                        (plan_id, day, str(plan.get("plan_name") or ""),
                         str(plan.get("department") or department),
                         _number(plan.get("roi_target")),
                         _number(plan.get("daily_budget")), category,
                         _number(plan.get("roi")),
                         int(plan["orders"]) if isinstance(plan.get("orders"), (int, float)) else None,
                         _number(plan.get("spent")), plan.get("status"), now),
                    )
    return recorded


def list_plan_daily_labels(
    label_date: str | None = None, *, active_only: bool = False
) -> list[dict[str, Any]]:
    """Return per-slot daily lifecycle labels for report/history consumers.

    ``active_only`` means the plan was投放中 at that scan slot, which is the
    evidence needed by the night report to include plans that were later paused.
    """
    day = label_date or date.today().isoformat()
    with _connect() as conn:
        conn.executescript(_PLAN_DAILY_LABELS_DDL)
        sql = "SELECT * FROM plan_daily_labels WHERE label_date=?"
        params: list[Any] = [day]
        if active_only:
            sql += " AND plan_status LIKE ?"
            params.append("%投放中%")
        sql += " ORDER BY scanned_at DESC, scan_slot DESC, plan_name"
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_latest_label_roi(plan_ids: list[str]) -> dict[str, float]:
    """每个计划最近一天看板行的 roi_target（建票时的意图值）。

    2026-10-09 旧素材通道 ROI 6.5：归位/漂移检测不能只认 _LABEL_ROIS 的
    分类默认值（new_plan=4.5），否则 6.5 计划被当「参数漂移」打回 4.5。
    意图值来源=pause-one 落看板时写入的 roi_target。
    """
    out: dict[str, float] = {}
    ids = [str(p).strip() for p in plan_ids if str(p or "").strip()]
    if not ids:
        return out
    with _connect() as conn:
        conn.executescript(_PLAN_DAILY_BOARD_DDL)
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT plan_id, roi_target, MAX(label_date) AS d
                FROM plan_daily_board
                WHERE plan_id IN ({marks}) AND roi_target IS NOT NULL
                GROUP BY plan_id""",
            ids,
        ).fetchall()
        for row in rows:
            try:
                out[str(row["plan_id"])] = float(row["roi_target"])
            except (TypeError, ValueError):
                continue
    return out


def list_plan_daily_board(label_date: str | None = None) -> list[dict[str, Any]]:
    """每日看板：一行一个计划，四个扫描档位的标签变化一目了然。"""
    day = label_date or date.today().isoformat()
    with _connect() as conn:
        conn.executescript(_PLAN_DAILY_BOARD_DDL)
        rows = conn.execute(
            """SELECT * FROM plan_daily_board WHERE label_date=?
               ORDER BY department, COALESCE(label_23, label_17, label_12, label_08), plan_name""",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_plan_catalog(limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM plan_catalog ORDER BY last_seen_at DESC LIMIT ?", (limit,)
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["category_name"] = _CATEGORY_NAMES.get(item["category"], item["category"])
        items.append(item)
    return items
