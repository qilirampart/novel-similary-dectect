from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import Any

import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from v2_common import now_ts  # noqa: E402


SCHEMA_PATH = ROOT_DIR / "service" / "business_schema_v1.sql"


def connect_business_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_compare_task_item_timing_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(compare_task_items)").fetchall()
    }
    for column_name, column_type in (
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
        ("duration_seconds", "REAL"),
    ):
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE compare_task_items ADD COLUMN {column_name} {column_type}"
            )


def init_business_db(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect_business_db(db_path)
    try:
        conn.executescript(schema_sql)
        _ensure_compare_task_item_timing_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _loads_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_rel_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR.resolve()))
    except Exception:
        return path


def _clip_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    accepted = row["accepted_input_count"]
    completed = int(row["completed_input_count"] or 0)
    failed = int(row["failed_input_count"] or 0)
    pending = None if accepted is None else max(int(accepted) - completed - failed, 0)
    return {
        "task_id": row["task_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "detection_mode": row["detection_mode"],
        "created_by": row["created_by"] or "",
        "source_file_name": row["source_file_name"],
        "source_file_ext": row["source_file_ext"],
        "source_file_path": _safe_rel_path(row["source_file_path"]),
        "source_file_sha256": row["source_file_sha256"],
        "source_file_size": int(row["source_file_size"] or 0),
        "params": _loads_json(row["params_json"]),
        "counts": {
            "accepted": None if accepted is None else int(accepted),
            "completed": completed,
            "failed": failed,
            "pending": pending,
        },
        "worker_name": row["worker_name"] or "",
        "status_message": row["status_message"] or "",
        "error_message": row["error_message"] or "",
        "summary_export_path": _safe_rel_path(row["summary_export_path"]),
        "review_export_path": _safe_rel_path(row["review_export_path"]),
        "result_json_path": _safe_rel_path(row["result_json_path"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
    }


def _task_item_row_to_dict(row: sqlite3.Row, include_payload: bool = False) -> dict[str, Any]:
    data = {
        "result_id": int(row["result_id"]),
        "task_id": row["task_id"],
        "item_order": int(row["item_order"]),
        "source_ref": row["source_ref"] or "",
        "query_text_preview": row["query_text_preview"],
        "status": row["status"],
        "started_at": row["started_at"] if "started_at" in row.keys() else None,
        "finished_at": row["finished_at"] if "finished_at" in row.keys() else None,
        "duration_seconds": None if "duration_seconds" not in row.keys() or row["duration_seconds"] is None else float(row["duration_seconds"]),
        "semantic_status": row["semantic_status"] or "",
        "top1_book_name": row["top1_book_name"] or "",
        "top1_chapter_name": row["top1_chapter_name"] or "",
        "top1_review_label": row["top1_review_label"] or "",
        "top1_confidence_label": row["top1_confidence_label"] or "",
        "top1_fine_score": None if row["top1_fine_score"] is None else float(row["top1_fine_score"]),
        "error_message": row["error_message"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_payload:
        data["query_text"] = row["query_text"]
        data["result_payload"] = _loads_json(row["result_payload_json"])
    return data


def _result_row_to_dict(row: sqlite3.Row, include_payload: bool = False) -> dict[str, Any]:
    data = _task_item_row_to_dict(row, include_payload=include_payload)
    row_keys = set(row.keys())
    data["detection_mode"] = row["detection_mode"] if "detection_mode" in row_keys else ""
    data["task_status"] = row["task_status"] if "task_status" in row_keys else ""
    data["source_file_name"] = row["source_file_name"] if "source_file_name" in row_keys else ""
    data["task_created_at"] = row["task_created_at"] if "task_created_at" in row_keys else None
    data["task_finished_at"] = row["task_finished_at"] if "task_finished_at" in row_keys else None
    data["review"] = {
        "review_status": row["review_status"] if "review_status" in row_keys and row["review_status"] else "",
        "reviewer_name": row["reviewer_name"] if "reviewer_name" in row_keys and row["reviewer_name"] else "",
        "review_note": row["review_note"] if "review_note" in row_keys and row["review_note"] else "",
        "updated_at": row["review_updated_at"] if "review_updated_at" in row_keys else None,
    }
    return data


def create_compare_task(
    db_path: str | Path,
    task_id: str,
    detection_mode: str,
    source_file_name: str,
    source_file_ext: str,
    source_file_path: str,
    source_file_sha256: str,
    source_file_size: int,
    params: dict[str, Any],
    created_by: str = "",
) -> dict[str, Any]:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO compare_tasks (
                task_id,
                status,
                detection_mode,
                created_by,
                source_file_name,
                source_file_ext,
                source_file_path,
                source_file_sha256,
                source_file_size,
                params_json,
                created_at,
                updated_at
            )
            VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                detection_mode,
                created_by,
                source_file_name,
                source_file_ext,
                source_file_path,
                source_file_sha256,
                int(source_file_size),
                json.dumps(params, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, task_id)


def list_compare_tasks(
    db_path: str | Path,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
              FROM compare_tasks
             ORDER BY created_at DESC, task_id DESC
             LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_task_row_to_dict(row) for row in rows]


def get_compare_task(
    db_path: str | Path,
    task_id: str,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM compare_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _task_row_to_dict(row)


def cancel_compare_task(
    db_path: str | Path,
    task_id: str,
    reason: str = "",
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        existing = conn.execute(
            "SELECT status FROM compare_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is None:
            return None
        current_status = str(existing["status"] or "")
        if current_status in {"completed", "failed", "partial_failed", "cancelled"}:
            conn.commit()
            return None
        if current_status == "queued":
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'cancelled',
                       status_message = ?,
                       error_message = '',
                       finished_at = ?,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task cancelled before execution",
                    now,
                    now,
                    now,
                    task_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'cancel_requested',
                       status_message = ?,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task cancellation requested",
                    now,
                    now,
                    task_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, task_id)


def retry_compare_task(
    db_path: str | Path,
    task_id: str,
    new_task_id: str,
    created_by: str = "",
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        source_task = conn.execute(
            "SELECT * FROM compare_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if source_task is None:
            return None
        conn.execute(
            """
            INSERT INTO compare_tasks (
                task_id,
                status,
                detection_mode,
                created_by,
                source_file_name,
                source_file_ext,
                source_file_path,
                source_file_sha256,
                source_file_size,
                params_json,
                created_at,
                updated_at
            )
            VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_task_id,
                source_task["detection_mode"],
                created_by or source_task["created_by"] or "retry",
                source_task["source_file_name"],
                source_task["source_file_ext"],
                source_task["source_file_path"],
                source_task["source_file_sha256"],
                int(source_task["source_file_size"] or 0),
                source_task["params_json"],
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, new_task_id)


def list_compare_task_items(
    db_path: str | Path,
    task_id: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
              FROM compare_task_items
             WHERE task_id = ?
             ORDER BY item_order ASC
             LIMIT ? OFFSET ?
            """,
            (task_id, int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_task_item_row_to_dict(row) for row in rows]


def get_compare_result(
    db_path: str | Path,
    result_id: int,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT i.*,
                   t.detection_mode,
                   t.source_file_name,
                   t.status AS task_status,
                   t.created_at AS task_created_at,
                   t.finished_at AS task_finished_at,
                   r.review_status,
                   r.reviewer_name,
                   r.review_note,
                   r.updated_at AS review_updated_at
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE i.result_id = ?
            """,
            (int(result_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _result_row_to_dict(row, include_payload=True)


def list_compare_results(
    db_path: str | Path,
    limit: int = 20,
    offset: int = 0,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    sort_by: str = "updated_at_desc",
) -> list[dict[str, Any]]:
    order_by = {
        "updated_at_desc": "i.updated_at DESC, i.result_id DESC",
        "score_desc": "i.top1_fine_score DESC, i.updated_at DESC, i.result_id DESC",
    }.get(sort_by, "i.updated_at DESC, i.result_id DESC")

    where_clauses = ["1 = 1"]
    params: list[Any] = []
    if task_id:
        where_clauses.append("i.task_id = ?")
        params.append(task_id)
    if item_status:
        where_clauses.append("i.status = ?")
        params.append(item_status)
    if review_status:
        normalized_review_status = review_status.strip()
        if normalized_review_status == "pending":
            where_clauses.append("COALESCE(r.review_status, '') IN ('', 'pending')")
        else:
            where_clauses.append("COALESCE(r.review_status, '') = ?")
            params.append(normalized_review_status)

    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT i.*,
                   t.detection_mode,
                   t.source_file_name,
                   t.status AS task_status,
                   t.created_at AS task_created_at,
                   t.finished_at AS task_finished_at,
                   r.review_status,
                   r.reviewer_name,
                   r.review_note,
                   r.updated_at AS review_updated_at
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE {" AND ".join(where_clauses)}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
            """,
            (*params, int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_result_row_to_dict(row) for row in rows]


def upsert_compare_task_review(
    db_path: str | Path,
    result_id: int,
    review_status: str,
    reviewer_name: str = "",
    review_note: str = "",
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        existing = conn.execute(
            "SELECT result_id FROM compare_task_items WHERE result_id = ?",
            (int(result_id),),
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            """
            INSERT INTO compare_task_reviews (
                result_id,
                review_status,
                reviewer_name,
                review_note,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(result_id) DO UPDATE SET
                review_status = excluded.review_status,
                reviewer_name = excluded.reviewer_name,
                review_note = excluded.review_note,
                updated_at = excluded.updated_at
            """,
            (
                int(result_id),
                review_status,
                reviewer_name,
                review_note,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_compare_result(db_path, result_id)


def claim_next_compare_task(
    db_path: str | Path,
    worker_name: str,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT task_id
              FROM compare_tasks
             WHERE status = 'queued'
             ORDER BY created_at ASC, task_id ASC
             LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        task_id = row["task_id"]
        now = now_ts()
        updated = conn.execute(
            """
            UPDATE compare_tasks
               SET status = 'running',
                   worker_name = ?,
                   status_message = ?,
                   started_at = COALESCE(started_at, ?),
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
               AND status = 'queued'
            """,
            (
                worker_name,
                "Task claimed by worker. Parsing input file.",
                now,
                now,
                now,
                task_id,
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, task_id)


def set_task_input_count(
    db_path: str | Path,
    task_id: str,
    accepted_input_count: int,
    status_message: str = "",
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET accepted_input_count = ?,
                   status_message = ?,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (
                int(accepted_input_count),
                status_message,
                now,
                now,
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def replace_task_items(
    db_path: str | Path,
    task_id: str,
    items: list[dict[str, Any]],
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute("DELETE FROM compare_task_items WHERE task_id = ?", (task_id,))
        conn.executemany(
            """
            INSERT INTO compare_task_items (
                task_id,
                item_order,
                source_ref,
                query_text,
                query_text_preview,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            [
                (
                    task_id,
                    int(item["item_order"]),
                    str(item.get("source_ref", "")),
                    str(item["query_text"]),
                    _clip_text(str(item["query_text"])),
                    now,
                    now,
                )
                for item in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def mark_task_heartbeat(
    db_path: str | Path,
    task_id: str,
    status_message: str,
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET status_message = ?,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (status_message, now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_task_item_success(
    db_path: str | Path,
    task_id: str,
    item_order: int,
    semantic_status: str,
    top1_book_name: str,
    top1_chapter_name: str,
    top1_review_label: str,
    top1_confidence_label: str,
    top1_fine_score: float | None,
    result_payload: dict[str, Any],
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET status = 'completed',
                   started_at = COALESCE(started_at, ?),
                   finished_at = ?,
                   duration_seconds = CASE
                       WHEN started_at IS NOT NULL THEN
                           (julianday(?) - julianday(started_at)) * 86400.0
                       ELSE duration_seconds
                   END,
                   semantic_status = ?,
                   top1_book_name = ?,
                   top1_chapter_name = ?,
                   top1_review_label = ?,
                   top1_confidence_label = ?,
                   top1_fine_score = ?,
                   result_payload_json = ?,
                   error_message = '',
                   updated_at = ?
             WHERE task_id = ?
               AND item_order = ?
            """,
            (
                now,
                now,
                now,
                semantic_status,
                top1_book_name,
                top1_chapter_name,
                top1_review_label,
                top1_confidence_label,
                top1_fine_score,
                json.dumps(result_payload, ensure_ascii=False),
                now,
                task_id,
                int(item_order),
            ),
        )
        conn.execute(
            """
            UPDATE compare_tasks
               SET completed_input_count = completed_input_count + 1,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_task_item_failure(
    db_path: str | Path,
    task_id: str,
    item_order: int,
    error_message: str,
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET status = 'failed',
                   started_at = COALESCE(started_at, ?),
                   finished_at = ?,
                   duration_seconds = CASE
                       WHEN started_at IS NOT NULL THEN
                           (julianday(?) - julianday(started_at)) * 86400.0
                       ELSE duration_seconds
                   END,
                   error_message = ?,
                   updated_at = ?
             WHERE task_id = ?
               AND item_order = ?
            """,
            (
                now,
                now,
                now,
                error_message,
                now,
                task_id,
                int(item_order),
            ),
        )
        conn.execute(
            """
            UPDATE compare_tasks
               SET failed_input_count = failed_input_count + 1,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_task(
    db_path: str | Path,
    task_id: str,
    status: str,
    status_message: str,
    error_message: str = "",
    summary_export_path: str = "",
    review_export_path: str = "",
    result_json_path: str = "",
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET status = ?,
                   status_message = ?,
                   error_message = ?,
                   summary_export_path = ?,
                   review_export_path = ?,
                   result_json_path = ?,
                   finished_at = ?,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (
                status,
                status_message,
                error_message,
                summary_export_path,
                review_export_path,
                result_json_path,
                now,
                now,
                now,
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
