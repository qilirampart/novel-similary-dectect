from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sys

try:
    from pysqlite3 import dbapi2 as sqlite3  # type: ignore
except Exception:
    import sqlite3


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from v2_common import now_ts  # noqa: E402
from v2_common import connect_db  # noqa: E402


SCHEMA_PATH = ROOT_DIR / "service" / "business_schema_v1.sql"
HIGH_RISK_REVIEW_LABEL = "强证据"


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


def _ensure_compare_task_item_source_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(compare_task_items)").fetchall()
    }
    for column_name in (
        "source_short_drama",
        "source_novel_name",
        "source_excel_row",
        "source_episode",
        "source_author",
        "source_platform",
        "source_display_title",
        "source_description",
    ):
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE compare_task_items ADD COLUMN {column_name} TEXT"
            )


def _normalize_compare_task_item_dedupe_component(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "__EMPTY__"


def _build_compare_task_item_dedupe_key(
    *,
    source_short_drama: Any,
    source_novel_name: Any,
    source_ref: Any,
    query_text: Any,
) -> str:
    raw = "\x1f".join(
        (
            _normalize_compare_task_item_dedupe_component(source_short_drama),
            _normalize_compare_task_item_dedupe_component(source_novel_name),
            _normalize_compare_task_item_dedupe_component(source_ref),
            _normalize_compare_task_item_dedupe_component(query_text),
        )
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _ensure_compare_task_item_review_cache_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(compare_task_items)").fetchall()
    }
    for column_name, column_type in (
        ("owner_user_id", "INTEGER"),
        ("dedupe_key", "TEXT"),
    ):
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE compare_task_items ADD COLUMN {column_name} {column_type}"
            )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_task_items_updated_result
            ON compare_task_items (updated_at DESC, result_id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_task_items_owner_updated_result
            ON compare_task_items (owner_user_id, updated_at DESC, result_id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_task_items_owner_dedupe_updated_result
            ON compare_task_items (owner_user_id, dedupe_key, updated_at DESC, result_id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_task_items_owner_score_updated_result
            ON compare_task_items (owner_user_id, top1_fine_score DESC, updated_at DESC, result_id DESC)
        """
    )


def _ensure_compare_task_owner_column(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(compare_tasks)").fetchall()
    }
    if "owner_user_id" not in existing_columns:
        conn.execute("ALTER TABLE compare_tasks ADD COLUMN owner_user_id INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_tasks_owner_created
            ON compare_tasks (owner_user_id, is_deleted, created_at DESC)
        """
    )


def _ensure_compare_task_control_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(compare_tasks)").fetchall()
    }
    for column_name, column_type, default_sql in (
        ("is_deleted", "INTEGER", "DEFAULT 0"),
        ("paused_at", "TEXT", ""),
        ("deleted_at", "TEXT", ""),
    ):
        if column_name not in existing_columns:
            suffix = f" {default_sql}" if default_sql else ""
            conn.execute(
                f"ALTER TABLE compare_tasks ADD COLUMN {column_name} {column_type}{suffix}"
            )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_tasks_visible_status_created
            ON compare_tasks (is_deleted, status, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compare_tasks_visible_owner_created
            ON compare_tasks (owner_user_id, is_deleted, created_at DESC)
        """
    )


def _ensure_default_admin_user(conn: sqlite3.Connection) -> None:
    now = now_ts()
    conn.execute(
        """
        INSERT INTO app_users (
            username,
            password_hash,
            display_name,
            role,
            is_active,
            created_at,
            updated_at
        )
        SELECT ?, ?, ?, 'admin', 1, ?, ?
         WHERE NOT EXISTS (
            SELECT 1 FROM app_users WHERE username = ?
         )
        """,
        (
            "admin",
            hash_user_password("admin123"),
            "管理员",
            now,
            now,
            "admin",
        ),
    )


def _get_admin_user_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT user_id FROM app_users WHERE username = ?",
        ("admin",),
    ).fetchone()
    if row is None:
        return None
    return int(row["user_id"])


def _backfill_compare_task_owner_user_id(conn: sqlite3.Connection) -> None:
    admin_user_id = _get_admin_user_id(conn)
    if admin_user_id is None:
        return
    conn.execute(
        """
        UPDATE compare_tasks
           SET owner_user_id = ?
         WHERE owner_user_id IS NULL
        """,
        (admin_user_id,),
    )


def _backfill_compare_task_item_review_cache(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT i.result_id,
               i.source_short_drama,
               i.source_novel_name,
               i.source_ref,
               i.query_text,
               i.owner_user_id,
               i.dedupe_key,
               t.owner_user_id AS task_owner_user_id
          FROM compare_task_items i
          LEFT JOIN compare_tasks t
            ON t.task_id = i.task_id
         WHERE i.owner_user_id IS NULL
            OR COALESCE(i.dedupe_key, '') = ''
        """
    ).fetchall()
    if not rows:
        return
    conn.executemany(
        """
        UPDATE compare_task_items
           SET owner_user_id = ?,
               dedupe_key = ?
         WHERE result_id = ?
        """,
        [
            (
                None
                if row["task_owner_user_id"] is None
                else int(row["task_owner_user_id"]),
                _build_compare_task_item_dedupe_key(
                    source_short_drama=row["source_short_drama"],
                    source_novel_name=row["source_novel_name"],
                    source_ref=row["source_ref"],
                    query_text=row["query_text"],
                ),
                int(row["result_id"]),
            )
            for row in rows
        ],
    )


def _backfill_compare_task_item_source_metadata(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT DISTINCT t.task_id,
               t.source_file_path,
               t.source_file_ext
          FROM compare_tasks t
          JOIN compare_task_items i
            ON i.task_id = t.task_id
         WHERE t.source_file_ext IN ('.xlsx', '.csv', '.tsv')
           AND (
                COALESCE(i.source_short_drama, '') = ''
             OR (
                    COALESCE(i.source_ref, '') LIKE 'row_%'
                AND COALESCE(i.source_short_drama, '') = ''
                )
             OR COALESCE(i.query_text_preview, '') IN ('当前批次', '当前结果', '历史汇总', '뎠품툽늴')
           )
        """
    ).fetchall()
    if not rows:
        return

    from service.task_input_parser import parse_task_input_file

    for row in rows:
        source_file_path = Path(str(row["source_file_path"] or ""))
        if not source_file_path:
            continue
        if not source_file_path.is_absolute():
            source_file_path = (ROOT_DIR / source_file_path).resolve()
        if not source_file_path.exists():
            continue
        try:
            parsed_items = parse_task_input_file(source_file_path)
        except Exception:
            continue
        if not parsed_items:
            continue
        conn.executemany(
            """
            UPDATE compare_task_items
               SET source_short_drama = ?,
                   source_novel_name = ?,
                   source_excel_row = ?,
                   source_episode = ?,
                   source_author = ?,
                   source_platform = ?,
                   source_display_title = ?,
                   source_description = ?,
                   source_ref = CASE WHEN COALESCE(?, '') <> '' THEN ? ELSE source_ref END,
                   query_text = ?,
                   query_text_preview = ?
             WHERE task_id = ?
               AND item_order = ?
            """,
            [
                (
                    item.source_short_drama,
                    item.source_novel_name,
                    item.source_excel_row,
                    item.source_episode,
                    item.source_author,
                    item.source_platform,
                    item.source_display_title,
                    item.source_description,
                    item.source_ref,
                    item.source_ref,
                    item.query_text,
                    _clip_text(item.query_text),
                    str(row["task_id"]),
                    int(item.item_order),
                )
                for item in parsed_items
            ],
        )


def init_business_db(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect_business_db(db_path)
    try:
        conn.executescript(schema_sql)
        _ensure_compare_task_item_timing_columns(conn)
        _ensure_compare_task_item_source_columns(conn)
        _ensure_compare_task_owner_column(conn)
        _ensure_compare_task_control_columns(conn)
        _ensure_compare_task_item_review_cache_columns(conn)
        _ensure_default_admin_user(conn)
        _backfill_compare_task_owner_user_id(conn)
        _backfill_compare_task_item_source_metadata(conn)
        _backfill_compare_task_item_review_cache(conn)
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


def _clip_text_middle(text: str, limit: int = 2000) -> str:
    compact = str(text or "")
    if limit <= 0 or len(compact) <= limit:
        return compact
    head = max(limit // 2, 1)
    tail = max(limit - head, 1)
    return compact[:head].rstrip() + "\n...\n" + compact[-tail:].lstrip()


def _fetch_chapter_content_by_uid(db_path: str | Path, chapter_uid: int) -> str:
    if int(chapter_uid or 0) <= 0:
        return ""
    try:
        conn = connect_db(db_path)
    except Exception:
        return ""
    try:
        try:
            row = conn.execute(
                """
                SELECT COALESCE(NULLIF(content_clean, ''), NULLIF(content_retrieval, ''), NULLIF(content_raw, '')) AS content
                  FROM chapter_contents
                 WHERE chapter_uid = ?
                """,
                (int(chapter_uid),),
            ).fetchone()
        except sqlite3.Error:
            return ""
    finally:
        conn.close()
    if row is None:
        return ""
    return str(row[0] or "")


def _build_candidate_review_context(
    chapter_text: str,
    *,
    start_offset: int,
    end_offset: int,
    matched_substring: str,
    context_chars: int = 1800,
    chapter_char_limit: int = 12000,
) -> tuple[str, str]:
    source_text = str(chapter_text or "")
    if not source_text:
        return "", ""

    safe_start = max(int(start_offset or 0), 0)
    safe_end = min(max(int(end_offset or 0), safe_start), len(source_text))
    if safe_end <= safe_start:
        safe_end = min(safe_start + 1, len(source_text))

    matched = str(matched_substring or "").strip()
    if matched:
        exact_index = source_text.find(matched)
        if exact_index >= 0:
            safe_start = exact_index
            safe_end = min(exact_index + len(matched), len(source_text))

    center = (safe_start + safe_end) // 2
    context_start = max(center - context_chars // 2, 0)
    context_end = min(context_start + context_chars, len(source_text))
    if context_end - context_start < context_chars and context_start > 0:
        context_start = max(context_end - context_chars, 0)

    context_text = source_text[context_start:context_end].strip()
    chapter_text_for_review = _clip_text_middle(source_text.strip(), limit=chapter_char_limit)
    return context_text, chapter_text_for_review


def _enrich_result_payload_for_review(
    retrieval_db_path: str | Path,
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    fine = result_payload.get("fine")
    if not isinstance(fine, dict):
        return result_payload

    results = fine.get("results")
    if not isinstance(results, list):
        return result_payload

    for item in results:
        if not isinstance(item, dict):
            continue
        best_match = item.get("best_match")
        if not isinstance(best_match, dict):
            continue
        chapter_uid = int(item.get("chapter_uid") or 0)
        if chapter_uid <= 0:
            continue
        chapter_text = _fetch_chapter_content_by_uid(retrieval_db_path, chapter_uid)
        if not chapter_text:
            continue
        review_context_text, chapter_text_for_review = _build_candidate_review_context(
            chapter_text,
            start_offset=int(best_match.get("candidate_start_offset") or 0),
            end_offset=int(best_match.get("candidate_end_offset") or 0),
            matched_substring=str(best_match.get("matched_substring") or ""),
        )
        best_match["candidate_text_full"] = chapter_text_for_review
        best_match["candidate_review_context_text"] = review_context_text or chapter_text_for_review
        best_match["candidate_review_context_start_offset"] = max(
            int(best_match.get("candidate_start_offset") or 0),
            0,
        )
    return result_payload


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


def hash_user_password(password: str) -> str:
    normalized = password.strip()
    if not normalized:
        raise ValueError("password is empty")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _user_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": bool(int(row["is_active"] or 0)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    accepted = row["accepted_input_count"]
    completed = int(row["completed_input_count"] or 0)
    failed = int(row["failed_input_count"] or 0)
    pending = None if accepted is None else max(int(accepted) - completed - failed, 0)
    return {
        "task_id": row["task_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "owner_user_id": int(row["owner_user_id"]) if row["owner_user_id"] is not None else None,
        "is_deleted": bool(int(row["is_deleted"] or 0)) if "is_deleted" in row.keys() else False,
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
        "paused_at": row["paused_at"] if "paused_at" in row.keys() else None,
        "deleted_at": row["deleted_at"] if "deleted_at" in row.keys() else None,
        "finished_at": row["finished_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
    }


def _task_item_row_to_dict(row: sqlite3.Row, include_payload: bool = False) -> dict[str, Any]:
    data = {
        "result_id": int(row["result_id"]),
        "task_id": row["task_id"],
        "item_order": int(row["item_order"]),
        "source_ref": row["source_ref"] or "",
        "source_short_drama": row["source_short_drama"] if "source_short_drama" in row.keys() and row["source_short_drama"] else "",
        "source_novel_name": row["source_novel_name"] if "source_novel_name" in row.keys() and row["source_novel_name"] else "",
        "source_excel_row": row["source_excel_row"] if "source_excel_row" in row.keys() and row["source_excel_row"] else "",
        "source_episode": row["source_episode"] if "source_episode" in row.keys() and row["source_episode"] else "",
        "source_author": row["source_author"] if "source_author" in row.keys() and row["source_author"] else "",
        "source_platform": row["source_platform"] if "source_platform" in row.keys() and row["source_platform"] else "",
        "source_display_title": row["source_display_title"] if "source_display_title" in row.keys() and row["source_display_title"] else "",
        "source_description": row["source_description"] if "source_description" in row.keys() and row["source_description"] else "",
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
    data["owner_user_id"] = int(row["owner_user_id"]) if "owner_user_id" in row_keys and row["owner_user_id"] is not None else None
    data["review"] = {
        "review_status": row["review_status"] if "review_status" in row_keys and row["review_status"] else "",
        "reviewer_name": row["reviewer_name"] if "reviewer_name" in row_keys and row["reviewer_name"] else "",
        "review_note": row["review_note"] if "review_note" in row_keys and row["review_note"] else "",
        "updated_at": row["review_updated_at"] if "review_updated_at" in row_keys else None,
    }
    return data


def create_user(
    db_path: str | Path,
    username: str,
    password: str,
    display_name: str,
    role: str = "operator",
) -> dict[str, Any]:
    normalized_username = username.strip().lower()
    normalized_display_name = display_name.strip() or normalized_username
    if not normalized_username:
        raise ValueError("username is empty")
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO app_users (
                username,
                password_hash,
                display_name,
                role,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                normalized_username,
                hash_user_password(password),
                normalized_display_name,
                role.strip() or "operator",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    user = get_user_by_username(db_path, normalized_username)
    if user is None:
        raise ValueError("failed to create user")
    return user


def list_users(db_path: str | Path) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT user_id,
                   username,
                   display_name,
                   role,
                   is_active,
                   created_at,
                   updated_at,
                   last_login_at
              FROM app_users
             ORDER BY user_id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [_user_row_to_dict(row) for row in rows]


def get_user_by_id(db_path: str | Path, user_id: int) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT user_id,
                   username,
                   display_name,
                   role,
                   is_active,
                   created_at,
                   updated_at,
                   last_login_at
              FROM app_users
             WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _user_row_to_dict(row)


def get_user_by_username(db_path: str | Path, username: str) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT user_id,
                   username,
                   display_name,
                   role,
                   is_active,
                   created_at,
                   updated_at,
                   last_login_at
              FROM app_users
             WHERE username = ?
            """,
            (username.strip().lower(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _user_row_to_dict(row)


def update_user(
    db_path: str | Path,
    user_id: int,
    display_name: str,
    role: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any] | None:
    normalized_display_name = display_name.strip()
    if not normalized_display_name:
        raise ValueError("display_name is empty")
    now = now_ts()
    assignments = ["display_name = ?", "updated_at = ?"]
    params: list[Any] = [normalized_display_name, now]
    if role is not None and role.strip():
        assignments.append("role = ?")
        params.append(role.strip())
    if is_active is not None:
        assignments.append("is_active = ?")
        params.append(1 if is_active else 0)
    params.append(int(user_id))

    conn = connect_business_db(db_path)
    try:
        updated = conn.execute(
            f"""
            UPDATE app_users
               SET {", ".join(assignments)}
             WHERE user_id = ?
            """,
            tuple(params),
        )
        conn.commit()
        if updated.rowcount != 1:
            return None
    finally:
        conn.close()
    return get_user_by_id(db_path, user_id)


def authenticate_user(
    db_path: str | Path,
    username: str,
    password: str,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT user_id,
                   username,
                   password_hash,
                   display_name,
                   role,
                   is_active,
                   created_at,
                   updated_at,
                   last_login_at
              FROM app_users
             WHERE username = ?
            """,
            (username.strip().lower(),),
        ).fetchone()
        if row is None:
            return None
        if not bool(int(row["is_active"] or 0)):
            return None
        if row["password_hash"] != hash_user_password(password):
            return None
        now = now_ts()
        conn.execute(
            "UPDATE app_users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
            (now, now, int(row["user_id"])),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": bool(int(row["is_active"] or 0)),
        "created_at": row["created_at"],
        "updated_at": now,
        "last_login_at": now,
    }


def create_user_session(
    db_path: str | Path,
    user_id: int,
    session_ttl_hours: int = 12,
) -> dict[str, Any]:
    now = datetime.now()
    expires_at = now + timedelta(hours=max(int(session_ttl_hours), 1))
    session_id = uuid4().hex
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO app_user_sessions (
                session_id,
                user_id,
                created_at,
                updated_at,
                expires_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                int(user_id),
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "session_id": session_id,
        "user_id": int(user_id),
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }


def revoke_user_session(
    db_path: str | Path,
    session_id: str,
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE app_user_sessions
               SET revoked_at = COALESCE(revoked_at, ?),
                   updated_at = ?,
                   last_seen_at = ?
             WHERE session_id = ?
            """,
            (now, now, now, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_session_user(
    db_path: str | Path,
    session_id: str,
) -> dict[str, Any] | None:
    now = datetime.now()
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT s.session_id,
                   s.expires_at,
                   s.revoked_at,
                   u.user_id,
                   u.username,
                   u.display_name,
                   u.role,
                   u.is_active,
                   u.created_at,
                   u.updated_at,
                   u.last_login_at
              FROM app_user_sessions s
              JOIN app_users u
                ON u.user_id = s.user_id
             WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"]:
            return None
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return None
        if expires_at <= now:
            return None
        if not bool(int(row["is_active"] or 0)):
            return None
        now_text = now.isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE app_user_sessions
               SET updated_at = ?,
                   last_seen_at = ?
             WHERE session_id = ?
            """,
            (now_text, now_text, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": bool(int(row["is_active"] or 0)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
        "session_id": session_id,
        "expires_at": row["expires_at"],
    }


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
    owner_user_id: int | None = None,
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
                owner_user_id,
                is_deleted,
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
            VALUES (?, 'queued', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                None if owner_user_id is None else int(owner_user_id),
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
    owner_user_id: int | None = None,
) -> list[dict[str, Any]]:
    where_clause = "WHERE COALESCE(is_deleted, 0) = 0"
    params: list[Any] = []
    if owner_user_id is not None:
        where_clause += " AND owner_user_id = ?"
        params.append(int(owner_user_id))
    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT *
              FROM compare_tasks
             {where_clause}
             ORDER BY created_at DESC, task_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_task_row_to_dict(row) for row in rows]


def get_compare_task(
    db_path: str | Path,
    task_id: str,
    owner_user_id: int | None = None,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    where_clause = "WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_deleted:
        where_clause += " AND COALESCE(is_deleted, 0) = 0"
    if owner_user_id is not None:
        where_clause += " AND owner_user_id = ?"
        params.append(int(owner_user_id))
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM compare_tasks {where_clause}",
            tuple(params),
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
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        if owner_user_id is not None:
            owned = conn.execute(
                "SELECT owner_user_id, is_deleted FROM compare_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if (
                owned is None
                or int(owned["owner_user_id"] or 0) != int(owner_user_id)
                or bool(int(owned["is_deleted"] or 0))
            ):
                conn.commit()
                return None
        existing = conn.execute(
            "SELECT status, is_deleted FROM compare_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is None:
            return None
        if bool(int(existing["is_deleted"] or 0)):
            conn.commit()
            return None
        current_status = str(existing["status"] or "")
        if current_status in {"completed", "failed", "partial_failed", "cancelled"}:
            conn.commit()
            return None
        if current_status in {"queued", "paused"}:
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'cancelled',
                       status_message = ?,
                       paused_at = NULL,
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
    return get_compare_task(db_path, task_id, owner_user_id=owner_user_id)


def retry_compare_task(
    db_path: str | Path,
    task_id: str,
    new_task_id: str,
    created_by: str = "",
    owner_user_id: int | None = None,
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
        if bool(int(source_task["is_deleted"] or 0)):
            return None
        if owner_user_id is not None and int(source_task["owner_user_id"] or 0) != int(owner_user_id):
            return None
        conn.execute(
            """
            INSERT INTO compare_tasks (
                task_id,
                status,
                owner_user_id,
                is_deleted,
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
            VALUES (?, 'queued', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_task_id,
                int(source_task["owner_user_id"]) if source_task["owner_user_id"] is not None else None,
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
    return get_compare_task(db_path, new_task_id, owner_user_id=owner_user_id)


def pause_compare_task(
    db_path: str | Path,
    task_id: str,
    reason: str = "",
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT owner_user_id, status, is_deleted
              FROM compare_tasks
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None or bool(int(row["is_deleted"] or 0)):
            conn.commit()
            return None
        if owner_user_id is not None and int(row["owner_user_id"] or 0) != int(owner_user_id):
            conn.commit()
            return None

        current_status = str(row["status"] or "")
        if current_status in {"completed", "failed", "partial_failed", "cancelled", "paused"}:
            conn.commit()
            return None

        if current_status == "queued":
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'paused',
                       status_message = ?,
                       paused_at = ?,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task paused before execution",
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
                   SET status = 'pause_requested',
                       status_message = ?,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task pause requested",
                    now,
                    now,
                    task_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, task_id, owner_user_id=owner_user_id)


def resume_compare_task(
    db_path: str | Path,
    task_id: str,
    reason: str = "",
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT owner_user_id, status, is_deleted
              FROM compare_tasks
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None or bool(int(row["is_deleted"] or 0)):
            conn.commit()
            return None
        if owner_user_id is not None and int(row["owner_user_id"] or 0) != int(owner_user_id):
            conn.commit()
            return None
        current_status = str(row["status"] or "")
        if current_status not in {"paused", "pause_requested"}:
            conn.commit()
            return None

        if current_status == "paused":
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'queued',
                       status_message = ?,
                       paused_at = NULL,
                       finished_at = NULL,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task resumed and returned to queue",
                    now,
                    now,
                    task_id,
                ),
            )
            conn.execute(
                """
                UPDATE compare_task_items
                   SET status = 'queued',
                       updated_at = ?
                 WHERE task_id = ?
                   AND status = 'running'
                """,
                (now, task_id),
            )
        else:
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'running',
                       status_message = ?,
                       paused_at = NULL,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (
                    reason or "task pause request revoked and execution resumed",
                    now,
                    now,
                    task_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(db_path, task_id, owner_user_id=owner_user_id)


def delete_compare_task(
    db_path: str | Path,
    task_id: str,
    reason: str = "",
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT owner_user_id, status, is_deleted
              FROM compare_tasks
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None or bool(int(row["is_deleted"] or 0)):
            conn.commit()
            return None
        if owner_user_id is not None and int(row["owner_user_id"] or 0) != int(owner_user_id):
            conn.commit()
            return None

        next_status = str(row["status"] or "")
        if next_status in {"running", "pause_requested", "cancel_requested"}:
            conn.commit()
            return None

        conn.execute(
            """
            UPDATE compare_tasks
               SET is_deleted = 1,
                   status = ?,
                   status_message = ?,
                   deleted_at = ?,
                   finished_at = COALESCE(finished_at, ?),
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (
                next_status,
                reason or "task deleted by user",
                now,
                now,
                now,
                now,
                task_id,
            ),
        )
        conn.execute(
            """
            UPDATE compare_task_items
               SET owner_user_id = NULL,
                   updated_at = ?
             WHERE task_id = ?
            """,
            (now, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_compare_task(
        db_path,
        task_id,
        owner_user_id=owner_user_id,
        include_deleted=True,
    )


def list_compare_task_items(
    db_path: str | Path,
    task_id: str,
    limit: int = 20,
    offset: int = 0,
    owner_user_id: int | None = None,
) -> list[dict[str, Any]]:
    task = get_compare_task(db_path, task_id, owner_user_id=owner_user_id)
    if task is None:
        return []
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


def get_compare_task_item_stats(
    db_path: str | Path,
    task_id: str,
    owner_user_id: int | None = None,
) -> dict[str, int]:
    task = get_compare_task(db_path, task_id, owner_user_id=owner_user_id)
    if task is None:
        return {
            "item_total": 0,
            "high_risk_count": 0,
            "semantic_fallback_count": 0,
        }

    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS item_total,
                   SUM(CASE WHEN top1_review_label = ? THEN 1 ELSE 0 END) AS high_risk_count,
                   SUM(CASE WHEN semantic_status = 'fallback_lexical_only' THEN 1 ELSE 0 END) AS semantic_fallback_count
              FROM compare_task_items
             WHERE task_id = ?
            """,
            (HIGH_RISK_REVIEW_LABEL, task_id),
        ).fetchone()
    finally:
        conn.close()

    return {
        "item_total": int(row["item_total"] or 0) if row is not None else 0,
        "high_risk_count": int(row["high_risk_count"] or 0) if row is not None else 0,
        "semantic_fallback_count": int(row["semantic_fallback_count"] or 0) if row is not None else 0,
    }


def count_compare_task_items(
    db_path: str | Path,
    task_id: str,
) -> int:
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
              FROM compare_task_items
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["total"] or 0) if row is not None else 0


def list_compare_task_input_items(
    db_path: str | Path,
    task_id: str,
) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT item_order,
                   source_ref,
                   source_short_drama,
                   source_novel_name,
                   source_excel_row,
                   source_episode,
                   source_author,
                   source_platform,
                   source_display_title,
                   source_description,
                   query_text,
                   status
              FROM compare_task_items
             WHERE task_id = ?
             ORDER BY item_order ASC
            """,
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "item_order": int(row["item_order"]),
            "source_ref": str(row["source_ref"] or ""),
            "source_short_drama": str(row["source_short_drama"] or ""),
            "source_novel_name": str(row["source_novel_name"] or ""),
            "source_excel_row": str(row["source_excel_row"] or ""),
            "source_episode": str(row["source_episode"] or ""),
            "source_author": str(row["source_author"] or ""),
            "source_platform": str(row["source_platform"] or ""),
            "source_display_title": str(row["source_display_title"] or ""),
            "source_description": str(row["source_description"] or ""),
            "query_text": str(row["query_text"] or ""),
            "status": str(row["status"] or ""),
        }
        for row in rows
    ]


def requeue_running_task_items(
    db_path: str | Path,
    task_id: str,
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        _requeue_running_task_items_on_conn(conn, task_id=task_id, now=now)
        conn.commit()
    finally:
        conn.close()


def _requeue_running_task_items_on_conn(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    now: str,
) -> int:
    updated = conn.execute(
        """
        UPDATE compare_task_items
           SET status = 'queued',
               started_at = CASE WHEN status = 'running' THEN NULL ELSE started_at END,
               finished_at = NULL,
               duration_seconds = NULL,
               updated_at = ?
         WHERE task_id = ?
           AND status = 'running'
        """,
        (now, task_id),
    )
    return max(int(updated.rowcount or 0), 0)


def _recover_interrupted_tasks_on_conn(
    conn: sqlite3.Connection,
    *,
    now: str,
) -> dict[str, int]:
    summary = {
        "running_to_queued": 0,
        "cancel_requested_to_cancelled": 0,
        "pause_requested_to_paused": 0,
        "requeued_item_count": 0,
    }
    rows = conn.execute(
        """
        SELECT task_id, status
          FROM compare_tasks
         WHERE COALESCE(is_deleted, 0) = 0
           AND status IN ('running', 'cancel_requested', 'pause_requested')
         ORDER BY created_at ASC, task_id ASC
        """
    ).fetchall()
    for row in rows:
        task_id = str(row["task_id"] or "")
        current_status = str(row["status"] or "")
        if not task_id or not current_status:
            continue
        summary["requeued_item_count"] += _requeue_running_task_items_on_conn(
            conn,
            task_id=task_id,
            now=now,
        )
        if current_status == "running":
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'queued',
                       worker_name = '',
                       status_message = ?,
                       paused_at = NULL,
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                   AND COALESCE(is_deleted, 0) = 0
                """,
                (
                    "Task recovered after worker interruption and returned to queue.",
                    now,
                    now,
                    task_id,
                ),
            )
            summary["running_to_queued"] += 1
            continue
        if current_status == "cancel_requested":
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = 'cancelled',
                       worker_name = '',
                       status_message = ?,
                       paused_at = NULL,
                       error_message = '',
                       finished_at = COALESCE(finished_at, ?),
                       updated_at = ?,
                       last_heartbeat_at = ?
                 WHERE task_id = ?
                   AND COALESCE(is_deleted, 0) = 0
                """,
                (
                    "Task cancelled after worker interruption recovery.",
                    now,
                    now,
                    now,
                    task_id,
                ),
            )
            summary["cancel_requested_to_cancelled"] += 1
            continue
        conn.execute(
            """
            UPDATE compare_tasks
               SET status = 'paused',
                   worker_name = '',
                   status_message = ?,
                   paused_at = COALESCE(paused_at, ?),
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
               AND COALESCE(is_deleted, 0) = 0
            """,
            (
                "Task paused after worker interruption recovery.",
                now,
                now,
                now,
                task_id,
            ),
        )
        summary["pause_requested_to_paused"] += 1
    return summary


def recover_interrupted_tasks(
    db_path: str | Path,
) -> dict[str, int]:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        summary = _recover_interrupted_tasks_on_conn(conn, now=now)
        conn.commit()
        return summary
    finally:
        conn.close()


def mark_task_paused(
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
               SET status = 'paused',
                   status_message = ?,
                   paused_at = ?,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
               AND COALESCE(is_deleted, 0) = 0
            """,
            (
                status_message,
                now,
                now,
                now,
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_compare_result(
    db_path: str | Path,
    result_id: int,
    retrieval_db_path: str | Path | None = None,
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    owner_filter = ""
    params: list[Any] = [int(result_id)]
    if owner_user_id is not None:
        owner_filter = " AND t.owner_user_id = ?"
        params.append(int(owner_user_id))
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT i.*,
                   t.detection_mode,
                   t.source_file_name,
                   t.owner_user_id,
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
               AND COALESCE(t.is_deleted, 0) = 0
               AND 1 = 1
            """
            + owner_filter,
            tuple(params),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = _result_row_to_dict(row, include_payload=True)
    if retrieval_db_path:
        result["result_payload"] = _enrich_result_payload_for_review(
            retrieval_db_path,
            result_payload=dict(result.get("result_payload") or {}),
        )
    return result


def _build_compare_results_filters(
    *,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    dedupe_latest: bool = False,
    owner_user_id: int | None = None,
    text_filter: str = "",
    exclude_cleared: bool = False,
    candidate_score_threshold: float | None = None,
) -> tuple[str, list[Any]]:
    where_clauses = ["1 = 1"]
    params: list[Any] = []

    if owner_user_id is not None:
        where_clauses.append("t.owner_user_id = ?")
        params.append(int(owner_user_id))
    where_clauses.append("COALESCE(t.is_deleted, 0) = 0")
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
    if exclude_cleared:
        where_clauses.append("COALESCE(r.review_status, '') <> 'cleared'")
    if candidate_score_threshold is not None:
        where_clauses.append("(i.top1_fine_score IS NULL OR i.top1_fine_score >= ?)")
        params.append(float(candidate_score_threshold))
    if text_filter.strip():
        keyword = f"%{text_filter.strip()}%"
        where_clauses.append(
            """
            (
                COALESCE(i.query_text_preview, '') LIKE ?
                OR COALESCE(i.source_short_drama, '') LIKE ?
                OR COALESCE(i.source_novel_name, '') LIKE ?
                OR COALESCE(i.source_display_title, '') LIKE ?
                OR COALESCE(i.top1_book_name, '') LIKE ?
                OR COALESCE(i.top1_chapter_name, '') LIKE ?
                OR COALESCE(i.task_id, '') LIKE ?
                OR COALESCE(i.top1_review_label, '') LIKE ?
                OR COALESCE(i.source_author, '') LIKE ?
            )
            """
        )
        params.extend([keyword] * 9)

    if dedupe_latest:
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                  FROM compare_task_items newer
                  JOIN compare_tasks newer_task
                    ON newer_task.task_id = newer.task_id
                 WHERE newer.result_id != i.result_id
                   AND COALESCE(NULLIF(newer.source_short_drama, ''), '__EMPTY__') = COALESCE(NULLIF(i.source_short_drama, ''), '__EMPTY__')
                   AND COALESCE(NULLIF(newer.source_novel_name, ''), '__EMPTY__') = COALESCE(NULLIF(i.source_novel_name, ''), '__EMPTY__')
                   AND COALESCE(NULLIF(newer.source_ref, ''), '__EMPTY__') = COALESCE(NULLIF(i.source_ref, ''), '__EMPTY__')
                   AND COALESCE(NULLIF(newer.query_text, ''), '__EMPTY__') = COALESCE(NULLIF(i.query_text, ''), '__EMPTY__')
                   AND newer_task.owner_user_id = t.owner_user_id
                   AND (
                        newer.updated_at > i.updated_at
                        OR (newer.updated_at = i.updated_at AND newer.result_id > i.result_id)
                   )
            )
            """
        )

    return " AND ".join(where_clauses), params


def _build_compare_results_item_scan_filters(
    *,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    text_filter: str = "",
    exclude_cleared: bool = False,
    item_alias: str = "i",
    candidate_score_threshold: float | None = None,
) -> tuple[str, list[Any]]:
    where_clauses = ["1 = 1"]
    params: list[Any] = []
    where_clauses.append(
        f"""
        EXISTS (
            SELECT 1
              FROM compare_tasks t_visible
             WHERE t_visible.task_id = {item_alias}.task_id
               AND COALESCE(t_visible.is_deleted, 0) = 0
        )
        """
    )

    if task_id:
        where_clauses.append(f"{item_alias}.task_id = ?")
        params.append(task_id)
    if item_status:
        where_clauses.append(f"{item_alias}.status = ?")
        params.append(item_status)
    if review_status:
        normalized_review_status = review_status.strip()
        if normalized_review_status == "pending":
            where_clauses.append(
                f"""
                NOT EXISTS (
                    SELECT 1
                      FROM compare_task_reviews r_status
                     WHERE r_status.result_id = {item_alias}.result_id
                       AND COALESCE(r_status.review_status, '') NOT IN ('', 'pending')
                )
                """
            )
        else:
            where_clauses.append(
                f"""
                EXISTS (
                    SELECT 1
                      FROM compare_task_reviews r_status
                     WHERE r_status.result_id = {item_alias}.result_id
                       AND COALESCE(r_status.review_status, '') = ?
                )
                """
            )
            params.append(normalized_review_status)
    if exclude_cleared:
        where_clauses.append(
            f"""
            NOT EXISTS (
                SELECT 1
                  FROM compare_task_reviews r_clear
                 WHERE r_clear.result_id = {item_alias}.result_id
                   AND r_clear.review_status = 'cleared'
            )
            """
        )
    if candidate_score_threshold is not None:
        where_clauses.append(
            f"({item_alias}.top1_fine_score IS NULL OR {item_alias}.top1_fine_score >= ?)"
        )
        params.append(float(candidate_score_threshold))
    if text_filter.strip():
        keyword = f"%{text_filter.strip()}%"
        where_clauses.append(
            f"""
            (
                COALESCE({item_alias}.query_text_preview, '') LIKE ?
                OR COALESCE({item_alias}.source_short_drama, '') LIKE ?
                OR COALESCE({item_alias}.source_novel_name, '') LIKE ?
                OR COALESCE({item_alias}.source_display_title, '') LIKE ?
                OR COALESCE({item_alias}.top1_book_name, '') LIKE ?
                OR COALESCE({item_alias}.top1_chapter_name, '') LIKE ?
                OR COALESCE({item_alias}.task_id, '') LIKE ?
                OR COALESCE({item_alias}.top1_review_label, '') LIKE ?
                OR COALESCE({item_alias}.source_author, '') LIKE ?
            )
            """
        )
        params.extend([keyword] * 9)

    return " AND ".join(where_clauses), params


def _list_compare_results_owner_fast(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    task_id: str,
    item_status: str,
    review_status: str,
    dedupe_latest: bool,
    owner_user_id: int,
    sort_by: str,
    text_filter: str,
    exclude_cleared: bool,
    candidate_score_threshold: float | None,
) -> list[dict[str, Any]]:
    filter_sql, filter_params = _build_compare_results_item_scan_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        item_alias="i",
        candidate_score_threshold=candidate_score_threshold,
    )
    if sort_by == "score_desc":
        if dedupe_latest:
            rows = conn.execute(
                f"""
                WITH latest_tokens AS (
                    SELECT dedupe_key,
                           MAX(updated_at || '|' || printf('%020d', result_id)) AS latest_token
                      FROM compare_task_items INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
                     WHERE owner_user_id = ?
                     GROUP BY dedupe_key
                ),
                latest_rows AS (
                    SELECT i.result_id,
                           i.top1_fine_score,
                           i.updated_at
                      FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
                      JOIN latest_tokens lt
                        ON lt.dedupe_key = i.dedupe_key
                       AND (i.updated_at || '|' || printf('%020d', i.result_id)) = lt.latest_token
                     WHERE i.owner_user_id = ?
                ),
                page_ids AS (
                    SELECT i.result_id,
                           i.top1_fine_score,
                           i.updated_at
                      FROM latest_rows lr
                      JOIN compare_task_items i
                        ON i.result_id = lr.result_id
                     WHERE {filter_sql}
                     ORDER BY i.top1_fine_score DESC, i.updated_at DESC, i.result_id DESC
                     LIMIT ? OFFSET ?
                )
                SELECT i.*,
                       t.detection_mode,
                       t.source_file_name,
                       t.owner_user_id,
                       t.status AS task_status,
                       t.created_at AS task_created_at,
                       t.finished_at AS task_finished_at,
                       r.review_status,
                       r.reviewer_name,
                       r.review_note,
                       r.updated_at AS review_updated_at
                  FROM page_ids p
                  JOIN compare_task_items i
                    ON i.result_id = p.result_id
                  JOIN compare_tasks t
                    ON t.task_id = i.task_id
                  LEFT JOIN compare_task_reviews r
                    ON r.result_id = i.result_id
                 ORDER BY p.top1_fine_score DESC, p.updated_at DESC, p.result_id DESC
                """,
                (int(owner_user_id), int(owner_user_id), *filter_params, int(limit), int(offset)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                WITH page_ids AS (
                    SELECT i.result_id,
                           i.top1_fine_score,
                           i.updated_at
                      FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_score_updated_result
                     WHERE i.owner_user_id = ?
                       AND {filter_sql}
                     ORDER BY i.top1_fine_score DESC, i.updated_at DESC, i.result_id DESC
                     LIMIT ? OFFSET ?
                )
                SELECT i.*,
                       t.detection_mode,
                       t.source_file_name,
                       t.owner_user_id,
                       t.status AS task_status,
                       t.created_at AS task_created_at,
                       t.finished_at AS task_finished_at,
                       r.review_status,
                       r.reviewer_name,
                       r.review_note,
                       r.updated_at AS review_updated_at
                  FROM page_ids p
                  JOIN compare_task_items i
                    ON i.result_id = p.result_id
                  JOIN compare_tasks t
                    ON t.task_id = i.task_id
                  LEFT JOIN compare_task_reviews r
                    ON r.result_id = i.result_id
                 ORDER BY p.top1_fine_score DESC, p.updated_at DESC, p.result_id DESC
                """,
                (int(owner_user_id), *filter_params, int(limit), int(offset)),
            ).fetchall()
        return [_result_row_to_dict(row) for row in rows]

    dedupe_sql = ""
    if dedupe_latest:
        dedupe_sql = """
            AND NOT EXISTS (
                SELECT 1
                  FROM compare_task_items newer INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
                 WHERE newer.owner_user_id = i.owner_user_id
                   AND newer.dedupe_key = i.dedupe_key
                   AND newer.result_id != i.result_id
                   AND (
                        newer.updated_at > i.updated_at
                        OR (newer.updated_at = i.updated_at AND newer.result_id > i.result_id)
                   )
            )
        """

    rows = conn.execute(
        f"""
        WITH page_ids AS (
            SELECT i.result_id,
                   i.updated_at
              FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_updated_result
             WHERE i.owner_user_id = ?
               AND {filter_sql}
               {dedupe_sql}
             ORDER BY i.updated_at DESC, i.result_id DESC
             LIMIT ? OFFSET ?
        )
        SELECT i.*,
               t.detection_mode,
               t.source_file_name,
               t.owner_user_id,
               t.status AS task_status,
               t.created_at AS task_created_at,
               t.finished_at AS task_finished_at,
               r.review_status,
               r.reviewer_name,
               r.review_note,
               r.updated_at AS review_updated_at
          FROM page_ids p
          JOIN compare_task_items i
            ON i.result_id = p.result_id
          JOIN compare_tasks t
            ON t.task_id = i.task_id
          LEFT JOIN compare_task_reviews r
            ON r.result_id = i.result_id
         ORDER BY p.updated_at DESC, p.result_id DESC
        """,
        (int(owner_user_id), *filter_params, int(limit), int(offset)),
    ).fetchall()
    return [_result_row_to_dict(row) for row in rows]


def _count_compare_results_owner_fast(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    item_status: str,
    review_status: str,
    dedupe_latest: bool,
    owner_user_id: int,
    text_filter: str,
    exclude_cleared: bool,
    candidate_score_threshold: float | None,
) -> int:
    filter_sql, filter_params = _build_compare_results_item_scan_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        item_alias="i",
        candidate_score_threshold=candidate_score_threshold,
    )
    if not dedupe_latest:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
              FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_updated_result
             WHERE i.owner_user_id = ?
               AND {filter_sql}
            """,
            (int(owner_user_id), *filter_params),
        ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    row = conn.execute(
        f"""
        WITH latest_tokens AS (
            SELECT dedupe_key,
                   MAX(updated_at || '|' || printf('%020d', result_id)) AS latest_token
              FROM compare_task_items INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
             WHERE owner_user_id = ?
             GROUP BY dedupe_key
        ),
        latest_rows AS (
            SELECT i.result_id
              FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
              JOIN latest_tokens lt
                ON lt.dedupe_key = i.dedupe_key
               AND (i.updated_at || '|' || printf('%020d', i.result_id)) = lt.latest_token
             WHERE i.owner_user_id = ?
        )
        SELECT COUNT(*) AS total
          FROM latest_rows lr
          JOIN compare_task_items i
            ON i.result_id = lr.result_id
         WHERE {filter_sql}
        """,
        (int(owner_user_id), int(owner_user_id), *filter_params),
    ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def _summarize_compare_results_owner_fast(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    item_status: str,
    review_status: str,
    dedupe_latest: bool,
    owner_user_id: int,
    text_filter: str,
    exclude_cleared: bool,
    candidate_score_threshold: float | None,
) -> dict[str, int]:
    filter_sql, filter_params = _build_compare_results_item_scan_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        item_alias="i",
        candidate_score_threshold=candidate_score_threshold,
    )
    if not dedupe_latest:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(r.review_status, '') IN ('', 'pending') THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'confirmed_high_risk' THEN 1 ELSE 0 END) AS confirmed_high_risk_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'needs_followup' THEN 1 ELSE 0 END) AS needs_followup_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'false_positive' THEN 1 ELSE 0 END) AS false_positive_count
              FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_updated_result
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE i.owner_user_id = ?
               AND {filter_sql}
            """,
            (int(owner_user_id), *filter_params),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            WITH latest_tokens AS (
                SELECT dedupe_key,
                       MAX(updated_at || '|' || printf('%020d', result_id)) AS latest_token
                  FROM compare_task_items INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
                 WHERE owner_user_id = ?
                 GROUP BY dedupe_key
            ),
            latest_rows AS (
                SELECT i.result_id
                  FROM compare_task_items i INDEXED BY idx_compare_task_items_owner_dedupe_updated_result
                  JOIN latest_tokens lt
                    ON lt.dedupe_key = i.dedupe_key
                   AND (i.updated_at || '|' || printf('%020d', i.result_id)) = lt.latest_token
                 WHERE i.owner_user_id = ?
            )
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(r.review_status, '') IN ('', 'pending') THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'confirmed_high_risk' THEN 1 ELSE 0 END) AS confirmed_high_risk_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'needs_followup' THEN 1 ELSE 0 END) AS needs_followup_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'false_positive' THEN 1 ELSE 0 END) AS false_positive_count
              FROM latest_rows lr
              JOIN compare_task_items i
                ON i.result_id = lr.result_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE {filter_sql}
            """,
            (int(owner_user_id), int(owner_user_id), *filter_params),
        ).fetchone()

    return {
        "total": int(row["total"] or 0) if row is not None else 0,
        "pending": int(row["pending_count"] or 0) if row is not None else 0,
        "confirmed_high_risk": int(row["confirmed_high_risk_count"] or 0) if row is not None else 0,
        "needs_followup": int(row["needs_followup_count"] or 0) if row is not None else 0,
        "false_positive": int(row["false_positive_count"] or 0) if row is not None else 0,
    }


def list_compare_results(
    db_path: str | Path,
    limit: int = 20,
    offset: int = 0,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    sort_by: str = "updated_at_desc",
    dedupe_latest: bool = False,
    owner_user_id: int | None = None,
    text_filter: str = "",
    exclude_cleared: bool = False,
    candidate_score_threshold: float | None = None,
) -> list[dict[str, Any]]:
    order_by = {
        "updated_at_desc": "i.updated_at DESC, i.result_id DESC",
        "score_desc": "i.top1_fine_score DESC, i.updated_at DESC, i.result_id DESC",
    }.get(sort_by, "i.updated_at DESC, i.result_id DESC")

    if owner_user_id is not None and sort_by in {"updated_at_desc", "score_desc"}:
        conn = connect_business_db(db_path)
        try:
            return _list_compare_results_owner_fast(
                conn,
                limit=limit,
                offset=offset,
                task_id=task_id,
                item_status=item_status,
                review_status=review_status,
                dedupe_latest=dedupe_latest,
                owner_user_id=int(owner_user_id),
                sort_by=sort_by,
                text_filter=text_filter,
                exclude_cleared=exclude_cleared,
                candidate_score_threshold=candidate_score_threshold,
            )
        finally:
            conn.close()

    where_sql, params = _build_compare_results_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        dedupe_latest=dedupe_latest,
        owner_user_id=owner_user_id,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        candidate_score_threshold=candidate_score_threshold,
    )

    conn = connect_business_db(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT i.*,
                   t.detection_mode,
                   t.source_file_name,
                   t.owner_user_id,
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
             WHERE {where_sql}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
            """,
            (*params, int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_result_row_to_dict(row) for row in rows]


def count_compare_results(
    db_path: str | Path,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    dedupe_latest: bool = False,
    owner_user_id: int | None = None,
    text_filter: str = "",
    exclude_cleared: bool = False,
    candidate_score_threshold: float | None = None,
) -> int:
    if owner_user_id is not None:
        conn = connect_business_db(db_path)
        try:
            return _count_compare_results_owner_fast(
                conn,
                task_id=task_id,
                item_status=item_status,
                review_status=review_status,
                dedupe_latest=dedupe_latest,
                owner_user_id=int(owner_user_id),
                text_filter=text_filter,
                exclude_cleared=exclude_cleared,
                candidate_score_threshold=candidate_score_threshold,
            )
        finally:
            conn.close()

    where_sql, params = _build_compare_results_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        dedupe_latest=dedupe_latest,
        owner_user_id=owner_user_id,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        candidate_score_threshold=candidate_score_threshold,
    )
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
    finally:
        conn.close()
    return int(row["total"] or 0) if row is not None else 0


def summarize_compare_results(
    db_path: str | Path,
    task_id: str = "",
    item_status: str = "",
    review_status: str = "",
    dedupe_latest: bool = False,
    owner_user_id: int | None = None,
    text_filter: str = "",
    exclude_cleared: bool = False,
    candidate_score_threshold: float | None = None,
) -> dict[str, int]:
    if owner_user_id is not None:
        conn = connect_business_db(db_path)
        try:
            return _summarize_compare_results_owner_fast(
                conn,
                task_id=task_id,
                item_status=item_status,
                review_status=review_status,
                dedupe_latest=dedupe_latest,
                owner_user_id=int(owner_user_id),
                text_filter=text_filter,
                exclude_cleared=exclude_cleared,
                candidate_score_threshold=candidate_score_threshold,
            )
        finally:
            conn.close()

    where_sql, params = _build_compare_results_filters(
        task_id=task_id,
        item_status=item_status,
        review_status=review_status,
        dedupe_latest=dedupe_latest,
        owner_user_id=owner_user_id,
        text_filter=text_filter,
        exclude_cleared=exclude_cleared,
        candidate_score_threshold=candidate_score_threshold,
    )
    conn = connect_business_db(db_path)
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(r.review_status, '') IN ('', 'pending') THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'confirmed_high_risk' THEN 1 ELSE 0 END) AS confirmed_high_risk_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'needs_followup' THEN 1 ELSE 0 END) AS needs_followup_count,
                   SUM(CASE WHEN COALESCE(r.review_status, '') = 'false_positive' THEN 1 ELSE 0 END) AS false_positive_count
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
    finally:
        conn.close()

    return {
        "total": int(row["total"] or 0) if row is not None else 0,
        "pending": int(row["pending_count"] or 0) if row is not None else 0,
        "confirmed_high_risk": int(row["confirmed_high_risk_count"] or 0) if row is not None else 0,
        "needs_followup": int(row["needs_followup_count"] or 0) if row is not None else 0,
        "false_positive": int(row["false_positive_count"] or 0) if row is not None else 0,
    }


def upsert_compare_task_review(
    db_path: str | Path,
    result_id: int,
    review_status: str,
    reviewer_name: str = "",
    review_note: str = "",
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        existing = conn.execute(
            """
            SELECT i.result_id,
                   t.owner_user_id,
                   t.is_deleted
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
             WHERE i.result_id = ?
            """,
            (int(result_id),),
        ).fetchone()
        if existing is None:
            return None
        if bool(int(existing["is_deleted"] or 0)):
            return None
        if owner_user_id is not None and int(existing["owner_user_id"] or 0) != int(owner_user_id):
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
    return get_compare_result(db_path, result_id, owner_user_id=owner_user_id)


def clear_pending_review_results(
    db_path: str | Path,
    owner_user_id: int,
) -> int:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        pending_rows = conn.execute(
            """
            SELECT i.result_id
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              LEFT JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE t.owner_user_id = ?
               AND COALESCE(t.is_deleted, 0) = 0
               AND COALESCE(r.review_status, '') IN ('', 'pending')
            """,
            (int(owner_user_id),),
        ).fetchall()
        result_ids = [int(row["result_id"]) for row in pending_rows]
        if not result_ids:
            conn.commit()
            return 0

        conn.executemany(
            """
            INSERT INTO compare_task_reviews (
                result_id,
                review_status,
                reviewer_name,
                review_note,
                created_at,
                updated_at
            )
            VALUES (?, 'cleared', '', 'queue cleared by user', ?, ?)
            ON CONFLICT(result_id) DO UPDATE SET
                review_status = 'cleared',
                reviewer_name = '',
                review_note = 'queue cleared by user',
                updated_at = excluded.updated_at
            """,
            [
                (
                    result_id,
                    now,
                    now,
                )
                for result_id in result_ids
            ],
        )
        conn.commit()
        return len(result_ids)
    finally:
        conn.close()


def claim_next_compare_task(
    db_path: str | Path,
    worker_name: str,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _recover_interrupted_tasks_on_conn(conn, now=now_ts())
        row = conn.execute(
            """
            SELECT task_id
              FROM compare_tasks
             WHERE status = 'queued'
               AND COALESCE(is_deleted, 0) = 0
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
                   paused_at = NULL,
                   started_at = COALESCE(started_at, ?),
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
               AND status = 'queued'
               AND COALESCE(is_deleted, 0) = 0
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
        task_row = conn.execute(
            "SELECT owner_user_id FROM compare_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        owner_user_id = (
            None
            if task_row is None or task_row["owner_user_id"] is None
            else int(task_row["owner_user_id"])
        )
        conn.execute("DELETE FROM compare_task_items WHERE task_id = ?", (task_id,))
        conn.executemany(
            """
            INSERT INTO compare_task_items (
                task_id,
                item_order,
                owner_user_id,
                dedupe_key,
                source_ref,
                source_short_drama,
                source_novel_name,
                source_excel_row,
                source_episode,
                source_author,
                source_platform,
                source_display_title,
                source_description,
                query_text,
                query_text_preview,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            [
                (
                    task_id,
                    int(item["item_order"]),
                    owner_user_id,
                    _build_compare_task_item_dedupe_key(
                        source_short_drama=item.get("source_short_drama", ""),
                        source_novel_name=item.get("source_novel_name", ""),
                        source_ref=item.get("source_ref", ""),
                        query_text=item["query_text"],
                    ),
                    str(item.get("source_ref", "")),
                    str(item.get("source_short_drama", "")),
                    str(item.get("source_novel_name", "")),
                    str(item.get("source_excel_row", "")),
                    str(item.get("source_episode", "")),
                    str(item.get("source_author", "")),
                    str(item.get("source_platform", "")),
                    str(item.get("source_display_title", "")),
                    str(item.get("source_description", "")),
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
