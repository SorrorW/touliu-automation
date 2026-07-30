"""SQLite 持久化：最近 7 天的任务记录 + 最近一次监控扫描结果。

数据库文件：webui/assets/webui.db（标准库 sqlite3，无新依赖）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

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
"""


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
    allowed = {"status", "plan_id", "auth_status", "authorized", "message", "creator_name"}
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
