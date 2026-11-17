"""SQLite persistence.  JSON payloads keep the frontend contract lossless."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..core.config import settings
from ..core.ids import now

WRITE_LOCK = threading.RLock()
SHANGHAI = timezone(timedelta(hours=8))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    settings.ensure_dirs()
    with get_conn() as conn:
        conn.executescript(schema)
        conn.commit()


# --------------------------------------------------------------------------- datasets
def _dataset_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"], "kind": record["kind"], "name": record["name"],
        "type": record["type"],
        "evaluation_types_json": _json(record.get("evaluationTypes") or [record["type"]]),
        "modality": record.get("modality"), "pipeline": record.get("pipeline"),
        "task_id": record.get("taskId"), "batch_id": record.get("batchId"),
        "version": record.get("version"), "source": record.get("source"),
        "status": record.get("status"), "mode": record.get("mode"),
        "samples_json": _json(record.get("samples") or []), "count": record.get("count") or 0,
        "sample_hash": record.get("sampleHash"), "standard": record.get("standard"),
        "note": record.get("note"), "created_at": record.get("createdAt"),
        "updated_at": now(),
    }


def _dataset_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "kind": row["kind"], "name": row["name"], "type": row["type"],
        "evaluationTypes": _load(row["evaluation_types_json"], [row["type"]]),
        "modality": row["modality"], "pipeline": row["pipeline"],
        "taskId": row["task_id"], "batchId": row["batch_id"], "version": row["version"],
        "source": row["source"], "status": row["status"], "mode": row["mode"],
        "samples": _load(row["samples_json"], []), "count": row["count"],
        "sampleHash": row["sample_hash"], "standard": row["standard"], "note": row["note"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def save_dataset(record: dict[str, Any]) -> dict[str, Any]:
    data = _dataset_row(record)
    columns = ", ".join(data)
    placeholders = ", ".join(f":{key}" for key in data)
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            f"INSERT INTO datasets ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(id, kind) DO UPDATE SET "
            + ", ".join(f"{key}=excluded.{key}" for key in data if key not in ("id", "kind")),
            data,
        )
        conn.commit()
    return get_dataset(record["kind"], record["id"])  # type: ignore[return-value]


def get_dataset(kind: str, dataset_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM datasets WHERE kind=? AND id=?", (kind, dataset_id)).fetchone()
    return _dataset_from_row(row) if row else None


def list_datasets(kind: str | None = None, type_: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM datasets WHERE 1=1"
    args: list[Any] = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if type_:
        sql += " AND (type=? OR evaluation_types_json LIKE ?)"
        args.extend([type_, f'%"{type_}"%'])
    sql += " ORDER BY updated_at DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_dataset_from_row(row) for row in rows]


def delete_dataset(kind: str, dataset_id: str) -> bool:
    with WRITE_LOCK, get_conn() as conn:
        cur = conn.execute("DELETE FROM datasets WHERE kind=? AND id=?", (kind, dataset_id))
        conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- tasks
def save_task(task: dict[str, Any], *, payload: dict[str, Any] | None = None) -> None:
    body = payload if payload is not None else {
        key: value for key, value in task.items() if key not in ("logs", "feedback")
    }
    data = {
        "id": task["id"], "name": task.get("name") or "", "type": task.get("type") or "",
        "mode": task.get("mode") or "demo", "status": task.get("status") or "draft",
        "step": int(task.get("step") or 0), "created_at": task.get("createdAt"),
        "completed_at": task.get("completedAt"), "error": task.get("error") or "",
        "dataset_id": task.get("datasetId"), "parent_id": task.get("parentId"),
        "payload_json": _json(body), "updated_at": now(),
    }
    columns = ", ".join(data)
    placeholders = ", ".join(f":{key}" for key in data)
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{key}=excluded.{key}" for key in data if key != "id"),
            data,
        )
        conn.commit()


def get_task(task_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        logs = [
            {"time": item["time"], "message": item["message"], "tag": item["tag"]}
            for item in conn.execute(
                "SELECT time, tag, message FROM task_logs WHERE task_id=? ORDER BY seq",
                (task_id,)).fetchall()
        ]
        feedback = [_feedback_from_row(item) for item in conn.execute(
            "SELECT * FROM feedback WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
    task = _load(row["payload_json"], {}) or {}
    task.update({
        "id": row["id"], "name": row["name"], "type": row["type"], "mode": row["mode"],
        "status": row["status"], "step": row["step"], "createdAt": row["created_at"],
        "completedAt": row["completed_at"], "error": row["error"] or "",
        "datasetId": row["dataset_id"], "parentId": row["parent_id"],
        "logs": logs, "feedback": feedback,
    })
    return task


def list_tasks(*, keyword: str = "", type_: str = "", status: str = "",
               page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    count_sql = "SELECT COUNT(*) FROM tasks WHERE 1=1"
    args: list[Any] = []
    if keyword:
        clause = " AND (name LIKE ? OR id LIKE ? OR payload_json LIKE ?)"
        pattern = f"%{keyword}%"
        sql += clause
        count_sql += clause
        args.extend([pattern, pattern, pattern])
    if type_:
        sql += " AND type=?"
        count_sql += " AND type=?"
        args.append(type_)
    if status:
        sql += " AND status=?"
        count_sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
    offset = max(0, (page - 1) * page_size)
    with get_conn() as conn:
        total = conn.execute(count_sql, args).fetchone()[0]
        rows = conn.execute(sql, [*args, page_size, offset]).fetchall()
    return [_summary_from_row(row) for row in rows], int(total)


def _summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
    task = _load(row["payload_json"], {}) or {}
    result = task.get("result") or {}
    summary = result.get("summary") or {}
    return {
        "id": row["id"], "name": row["name"], "type": row["type"], "mode": row["mode"],
        "status": row["status"], "step": row["step"], "createdAt": row["created_at"],
        "completedAt": row["completed_at"], "error": row["error"] or "",
        "datasetName": task.get("datasetName"), "datasetId": row["dataset_id"],
        "sampleCount": len(task.get("samples") or []),
        "accepted": summary.get("accepted"), "issues": summary.get("issues"),
        "passed": summary.get("passed"), "grades": summary.get("grades"),
        "openIssues": None, "parentId": row["parent_id"],
    }


def delete_task(task_id: str) -> bool:
    with WRITE_LOCK, get_conn() as conn:
        conn.execute("DELETE FROM task_logs WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM feedback WHERE task_id=?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    return cur.rowcount > 0


def append_log(task_id: str, tag: str, message: str, time: str | None = None) -> None:
    with WRITE_LOCK, get_conn() as conn:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_logs WHERE task_id=?",
            (task_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO task_logs (task_id, seq, time, tag, message) VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, time or now(), tag, message[:160]),
        )
        conn.commit()


def list_logs(task_id: str, after: int = 0) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT seq, time, tag, message FROM task_logs WHERE task_id=? AND seq>? ORDER BY seq",
            (task_id, after)).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- feedback
def _feedback_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"], "task_id": item["taskId"], "sample_id": item["sampleId"],
        "issue": item.get("issue"), "source_step": item.get("sourceStep"),
        "suggestion": item.get("suggestion"),
        "parameters_json": _json(item.get("parameters") or {}),
        "status": item.get("status") or "pending", "note": item.get("note") or "",
        "parameters_after_json": _json(item.get("parametersAfter")) if item.get("parametersAfter") is not None else None,
        "verification_id": item.get("verificationId"),
        "performance_before_json": _json(item.get("performanceBefore")) if item.get("performanceBefore") is not None else None,
        "performance_after_json": _json(item.get("performanceAfter")) if item.get("performanceAfter") is not None else None,
        "created_at": item.get("createdAt") or now(), "updated_at": now(),
    }


def _feedback_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "taskId": row["task_id"], "sampleId": row["sample_id"],
        "issue": row["issue"], "sourceStep": row["source_step"],
        "suggestion": row["suggestion"], "parameters": _load(row["parameters_json"], {}),
        "status": row["status"], "note": row["note"] or "",
        "parametersAfter": _load(row["parameters_after_json"], None),
        "verificationId": row["verification_id"],
        "performanceBefore": _load(row["performance_before_json"], None),
        "performanceAfter": _load(row["performance_after_json"], None),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def save_feedback(item: dict[str, Any]) -> None:
    data = _feedback_row(item)
    columns = ", ".join(data)
    placeholders = ", ".join(f":{key}" for key in data)
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            f"INSERT INTO feedback ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{key}=excluded.{key}" for key in data if key != "id"),
            data,
        )
        conn.commit()


def replace_feedback(task_id: str, items: list[dict[str, Any]]) -> None:
    """Replace a registered batch atomically, dropping obsolete row identifiers."""
    rows = [_feedback_row(item) for item in items]
    if any(row["task_id"] != task_id for row in rows):
        raise ValueError("Feedback task mismatch")
    with WRITE_LOCK, get_conn() as conn:
        conn.execute("DELETE FROM feedback WHERE task_id=?", (task_id,))
        for row in rows:
            columns = ", ".join(row)
            placeholders = ", ".join(f":{key}" for key in row)
            conn.execute(f"INSERT INTO feedback ({columns}) VALUES ({placeholders})", row)
        conn.commit()


def get_feedback(feedback_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
    return _feedback_from_row(row) if row else None


def list_feedback(*, status: str = "", task_id: str = "",
                  page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    where = " WHERE 1=1"
    args: list[Any] = []
    if status:
        where += " AND status=?"
        args.append(status)
    if task_id:
        where += " AND task_id=?"
        args.append(task_id)
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM feedback" + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM feedback" + where + " ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            [*args, page_size, max(0, (page - 1) * page_size)]).fetchall()
    return [_feedback_from_row(row) for row in rows], int(total)


def open_feedback_count() -> int:
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE status!='verified'").fetchone()[0])


# --------------------------------------------------------------------------- baselines
def _baseline_row(item: dict[str, Any]) -> dict[str, Any]:
    config = dict(item.get('config') or {})
    # Keep evidence in the existing JSON column; old databases need no schema
    # migration and API callers still receive a clean rule configuration.
    if isinstance(item.get('evidence'), dict):
        config['_baselineMetadata'] = {'evidence': item['evidence'],
                                       'isDefault': bool(item.get('isDefault'))}
    return {
        "id": item["id"], "type": item["type"], "model_id": item.get("modelId"),
        "mode": item.get("mode") or "demo", "name": item.get("name") or "",
        "description": item.get("description"), "version": item.get("version") or "1.0",
        "status": item.get("status") or "published",
        "validation_version": item.get("validationVersion"),
        "seed": item.get("seed"), "config_json": _json(config),
        "model_name": item.get("modelName"), "source": item.get("source"),
        "updated_at": item.get("updatedAt") or now(),
    }


def _baseline_from_row(row: sqlite3.Row) -> dict[str, Any]:
    config = _load(row['config_json'], {}) or {}
    metadata = config.pop('_baselineMetadata', {})
    item = {
        "id": row["id"], "type": row["type"], "modelId": row["model_id"],
        "mode": row["mode"], "name": row["name"], "description": row["description"],
        "version": row["version"], "status": row["status"],
        "validationVersion": row["validation_version"], "seed": row["seed"],
        "config": config, "modelName": row["model_name"],
        "source": row["source"], "updatedAt": row["updated_at"],
    }
    if isinstance(metadata, dict) and isinstance(metadata.get('evidence'), dict):
        item.update(evidence=metadata['evidence'], isDefault=bool(metadata.get('isDefault')))
    return item


def save_baseline(item: dict[str, Any]) -> dict[str, Any]:
    data = _baseline_row(item)
    columns = ", ".join(data)
    placeholders = ", ".join(f":{key}" for key in data)
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            f"INSERT INTO baselines ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{key}=excluded.{key}" for key in data if key != "id"),
            data,
        )
        conn.commit()
    return get_baseline(item["id"])  # type: ignore[return-value]


def get_baseline(baseline_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM baselines WHERE id=?", (baseline_id,)).fetchone()
    return _baseline_from_row(row) if row else None


def list_baselines(type_: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM baselines"
    args: list[Any] = []
    if type_:
        sql += " WHERE type=?"
        args.append(type_)
    sql += " ORDER BY type, updated_at"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_baseline_from_row(row) for row in rows]


def delete_baseline(baseline_id: str) -> bool:
    with WRITE_LOCK, get_conn() as conn:
        cur = conn.execute("DELETE FROM baselines WHERE id=?", (baseline_id,))
        conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- operator runs / artifacts
def save_operator_run(item: dict[str, Any]) -> None:
    data = {
        "unique_id": item["uniqueId"], "operator_id": item["operatorId"],
        "status": item["status"], "params_json": _json(item.get("params") or {}),
        "result_json": _json(item.get("result") or {}),
        "workspace": item.get("workspace"), "created_at": item.get("createdAt") or now(),
        "finished_at": item.get("finishedAt"),
    }
    columns = ", ".join(data)
    placeholders = ", ".join(f":{key}" for key in data)
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            f"INSERT INTO operator_runs ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(unique_id, operator_id) DO UPDATE SET "
            + ", ".join(f"{key}=excluded.{key}" for key in data if key not in ("unique_id", "operator_id")),
            data,
        )
        conn.commit()


def get_operator_run(unique_id: str, operator_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM operator_runs WHERE unique_id=? AND operator_id=?",
            (unique_id, operator_id)).fetchone()
    if not row:
        return None
    return {
        "uniqueId": row["unique_id"], "operatorId": row["operator_id"],
        "status": row["status"], "params": _load(row["params_json"], {}),
        "result": _load(row["result_json"], {}), "workspace": row["workspace"],
        "createdAt": row["created_at"], "finishedAt": row["finished_at"],
    }


def add_artifact(owner_type: str, owner_id: str, kind: str, path: str,
                 size: int = 0, sha256: str = "") -> None:
    with WRITE_LOCK, get_conn() as conn:
        conn.execute(
            "INSERT INTO artifacts (owner_type, owner_id, kind, path, size, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_type, owner_id, kind, path, size, sha256, now()),
        )
        conn.commit()


def list_artifacts(owner_type: str, owner_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE owner_type=? AND owner_id=? ORDER BY id",
            (owner_type, owner_id)).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- stats
def stats() -> dict[str, Any]:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='completed'").fetchone()[0]
        draft = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='draft'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0]
        by_type = dict(conn.execute(
            "SELECT type, COUNT(*) FROM tasks GROUP BY type").fetchall())
    return {
        "totalTasks": total, "runningTasks": running, "completedTasks": completed,
        "draftTasks": draft, "failedTasks": failed, "openIssues": open_feedback_count(),
        "byType": by_type,
    }
