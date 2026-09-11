from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from service.business_store import (
    BACKGROUND_DB_BUSY_TIMEOUT_MS,
    connect_business_db,
    try_begin_business_write,
)
from v2_common import now_ts


def _loads_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _task_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["params"] = _loads_json(data.pop("params_json", ""))
    data["counts"] = {
        "accepted": int(data.get("accepted_input_count") or 0),
        "completed": int(data.get("completed_input_count") or 0),
        "failed": int(data.get("failed_input_count") or 0),
    }
    return data


def create_drama_subtitle_task(
    *,
    db_path: str | Path,
    task_id: str,
    source_file_name: str,
    source_file_ext: str,
    source_file_path: str,
    source_file_sha256: str,
    source_file_size: int,
    owner_user_id: int | None,
    created_by: str,
    params: dict[str, Any] | None = None,
    accepted_input_count: int = 0,
) -> dict[str, Any]:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO drama_subtitle_tasks(
                task_id, status, owner_user_id, created_by,
                source_file_name, source_file_ext, source_file_path,
                source_file_sha256, source_file_size, params_json,
                created_at, updated_at
            ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                owner_user_id,
                created_by,
                source_file_name,
                source_file_ext,
                source_file_path,
                source_file_sha256,
                int(source_file_size),
                json.dumps(params or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        if accepted_input_count > 0:
            conn.execute(
                "UPDATE drama_subtitle_tasks SET accepted_input_count = ? WHERE task_id = ?",
                (int(accepted_input_count), task_id),
            )
        conn.execute(
            "UPDATE drama_subtitle_tasks SET queue_entered_at = ? WHERE task_id = ?",
            (now, task_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM drama_subtitle_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError("failed to create drama subtitle task")
        return _task_row_to_dict(row)
    finally:
        conn.close()


def get_drama_subtitle_task(
    db_path: str | Path,
    task_id: str,
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        sql = "SELECT * FROM drama_subtitle_tasks WHERE task_id = ? AND is_deleted = 0"
        params: list[object] = [task_id]
        if owner_user_id is not None:
            sql += " AND owner_user_id = ?"
            params.append(owner_user_id)
        row = conn.execute(sql, params).fetchone()
        return None if row is None else _task_row_to_dict(row)
    finally:
        conn.close()


def list_drama_subtitle_tasks(
    db_path: str | Path,
    *,
    owner_user_id: int | None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        sql = "SELECT * FROM drama_subtitle_tasks WHERE is_deleted = 0"
        params: list[object] = []
        if owner_user_id is not None:
            sql += " AND owner_user_id = ?"
            params.append(owner_user_id)
        sql += " ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_task_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def claim_next_drama_subtitle_task(
    db_path: str | Path,
    *,
    worker_name: str,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(
        db_path,
        busy_timeout_ms=BACKGROUND_DB_BUSY_TIMEOUT_MS,
    )
    try:
        queued = conn.execute(
            """
            SELECT 1
              FROM drama_subtitle_tasks
             WHERE is_deleted = 0
               AND status = 'queued'
             LIMIT 1
            """
        ).fetchone()
        if queued is None:
            return None
        if not try_begin_business_write(conn, operation="subtitle_claim"):
            return None
        row = conn.execute(
            """
            SELECT task_id
              FROM drama_subtitle_tasks
             WHERE is_deleted = 0
               AND status = 'queued'
             ORDER BY created_at ASC, task_id ASC
             LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        task_id = str(row["task_id"])
        lease_token = uuid4().hex
        updated = conn.execute(
            """
            UPDATE drama_subtitle_tasks
               SET status = 'running', worker_name = ?, worker_lease_token = ?,
                   started_at = COALESCE(started_at, ?),
                   updated_at = ?, last_heartbeat_at = ?, status_message = 'Preparing subtitle inputs.'
             WHERE task_id = ? AND status = 'queued' AND is_deleted = 0
            """,
            (worker_name, lease_token, now, now, now, task_id),
        ).rowcount
        if updated != 1:
            conn.commit()
            return None
        claimed = conn.execute("SELECT * FROM drama_subtitle_tasks WHERE task_id = ?", (task_id,)).fetchone()
        conn.commit()
        if claimed is None:
            return None
        result = _task_row_to_dict(claimed)
        result["_worker_lease_token"] = lease_token
        return result
    finally:
        conn.close()


def replace_drama_subtitle_task_items(
    *,
    db_path: str | Path,
    task_id: str,
    items: list[dict[str, Any]],
    worker_lease_token: str | None = None,
) -> None:
    now = now_ts()
    rows = [
        (
            task_id,
            int(item["item_order"]),
            str(item.get("source_ref") or ""),
            str(item.get("source_short_drama") or ""),
            str(item.get("source_episode") or ""),
            str(item.get("source_author") or ""),
            str(item.get("source_platform") or ""),
            str(item.get("source_display_title") or ""),
            str(item.get("source_description") or ""),
            str(item.get("source_excel_row") or ""),
            str(item.get("source_video_id") or ""),
            str(item.get("source_channel") or ""),
            str(item.get("source_upload_date") or ""),
            str(item.get("source_caption_language") or ""),
            str(item.get("source_caption_source") or ""),
            int(item["source_segment_order"])
            if str(item.get("source_segment_order") or "").strip().isdigit()
            else None,
            str(item.get("source_time_start") or ""),
            str(item.get("source_time_end") or ""),
            str(item.get("source_text_original") or item.get("query_text") or ""),
            str(item["query_text"]),
            str(item["query_text"])[:180],
            "queued",
            now,
            now,
        )
        for item in items
    ]
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if worker_lease_token is not None:
            lease_row = conn.execute(
                "SELECT 1 FROM drama_subtitle_tasks WHERE task_id = ? AND worker_lease_token = ? AND status IN ('running', 'pause_requested', 'cancel_requested')",
                (task_id, worker_lease_token),
            ).fetchone()
            if lease_row is None:
                conn.commit()
                return
        conn.execute("DELETE FROM drama_subtitle_task_items WHERE task_id = ?", (task_id,))
        conn.executemany(
            """
            INSERT INTO drama_subtitle_task_items(
                task_id, item_order, source_ref, source_short_drama, source_episode,
                source_author, source_platform, source_display_title, source_description,
                source_excel_row, source_video_id, source_channel, source_upload_date,
                source_caption_language, source_caption_source, source_segment_order,
                source_time_start, source_time_end, source_text_original,
                query_text, query_text_preview, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            """
            UPDATE drama_subtitle_tasks
               SET accepted_input_count = ?, completed_input_count = 0, failed_input_count = 0,
                   updated_at = ?, last_heartbeat_at = ?, status_message = 'Subtitle inputs prepared.'
             WHERE task_id = ?
               AND (? IS NULL OR worker_lease_token = ?)
            """,
            (len(rows), now, now, task_id, worker_lease_token, worker_lease_token),
        )
        conn.commit()
    finally:
        conn.close()


def list_drama_subtitle_task_items(
    db_path: str | Path,
    task_id: str,
    *,
    status: str = "",
) -> list[dict[str, Any]]:
    conn = connect_business_db(db_path)
    try:
        sql = """
            SELECT i.*,
                   COALESCE(r.review_status, 'pending') AS review_status,
                   COALESCE(r.reviewer_name, '') AS reviewer_name,
                   COALESCE(r.review_note, '') AS review_note,
                   r.updated_at AS review_updated_at
              FROM drama_subtitle_task_items i
              LEFT JOIN drama_subtitle_task_reviews r ON r.task_item_id = i.task_item_id
             WHERE i.task_id = ?
        """
        params: list[object] = [task_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY item_order"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_drama_subtitle_task_item(
    db_path: str | Path,
    task_item_id: int,
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    conn = connect_business_db(db_path)
    try:
        sql = """
            SELECT i.*,
                   COALESCE(r.review_status, 'pending') AS review_status,
                   COALESCE(r.reviewer_name, '') AS reviewer_name,
                   COALESCE(r.review_note, '') AS review_note,
                   r.updated_at AS review_updated_at
              FROM drama_subtitle_task_items i
              JOIN drama_subtitle_tasks t ON t.task_id = i.task_id
              LEFT JOIN drama_subtitle_task_reviews r ON r.task_item_id = i.task_item_id
             WHERE i.task_item_id = ? AND t.is_deleted = 0
        """
        params: list[object] = [task_item_id]
        if owner_user_id is not None:
            sql += " AND t.owner_user_id = ?"
            params.append(owner_user_id)
        row = conn.execute(sql, params).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def upsert_drama_subtitle_task_review(
    *,
    db_path: str | Path,
    task_item_id: int,
    review_status: str,
    reviewer_name: str,
    review_note: str,
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    normalized_status = review_status.strip()
    if normalized_status not in {"pending", "confirmed_high_risk", "needs_followup", "false_positive"}:
        raise ValueError(f"unsupported subtitle review status: {normalized_status!r}")
    existing = get_drama_subtitle_task_item(db_path, task_item_id, owner_user_id=owner_user_id)
    if existing is None:
        return None
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO drama_subtitle_task_reviews(
                task_item_id, review_status, reviewer_name, review_note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_item_id) DO UPDATE SET
                review_status = excluded.review_status,
                reviewer_name = excluded.reviewer_name,
                review_note = excluded.review_note,
                updated_at = excluded.updated_at
            """,
            (task_item_id, normalized_status, reviewer_name, review_note, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_drama_subtitle_task_item(db_path, task_item_id, owner_user_id=owner_user_id)


def save_drama_subtitle_task_item_outcome(
    *,
    db_path: str | Path,
    task_id: str,
    item_order: int,
    status: str,
    duration_seconds: float,
    query_language_code: str,
    query_language_confidence: float,
    result_payload: dict[str, Any] | None = None,
    error_message: str = "",
    worker_lease_token: str | None = None,
) -> None:
    payload = result_payload or {}
    decision = payload.get("decision") if isinstance(payload, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    is_matched = decision.get("status") == "matched" and decision.get("matched") is True
    candidates = payload.get("candidates") if isinstance(payload, dict) else []
    selected_rank = int(decision.get("candidate_rank") or 0)
    selected = next(
        (candidate for candidate in candidates if isinstance(candidate, dict) and int(candidate.get("rank") or 0) == selected_rank),
        {},
    ) if is_matched and isinstance(candidates, list) else {}
    evidence = selected.get("evidence") if isinstance(selected, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        if worker_lease_token is not None:
            lease_row = conn.execute(
                "SELECT 1 FROM drama_subtitle_tasks WHERE task_id = ? AND worker_lease_token = ? AND status IN ('running', 'pause_requested', 'cancel_requested')",
                (task_id, worker_lease_token),
            ).fetchone()
            if lease_row is None:
                conn.commit()
                return
        conn.execute(
            """
            UPDATE drama_subtitle_task_items
               SET status = ?, finished_at = ?, duration_seconds = ?,
                   query_language_code = ?, query_language_confidence = ?,
                   matched_book_id = ?, matched_book_name = ?, matched_episode_order = ?,
                   matched_language_code = ?, lexical_score = ?, evidence_window_uid = ?,
                   evidence_time_start = ?, evidence_time_end = ?, result_payload_json = ?,
                   error_message = ?, updated_at = ?
             WHERE task_id = ? AND item_order = ?
               AND (? IS NULL OR EXISTS (
                   SELECT 1 FROM drama_subtitle_tasks t
                    WHERE t.task_id = drama_subtitle_task_items.task_id
                      AND t.worker_lease_token = ?
               ))
            """,
            (
                status,
                now,
                duration_seconds,
                query_language_code,
                query_language_confidence,
                str(selected.get("book_id") or ""),
                str(selected.get("book_name") or ""),
                selected.get("episode_order"),
                str(selected.get("language_code") or ""),
                selected.get("best_lexical_score"),
                str(evidence.get("window_uid") or ""),
                str(evidence.get("time_start") or ""),
                str(evidence.get("time_end") or ""),
                json.dumps(payload, ensure_ascii=False),
                error_message,
                now,
                task_id,
                item_order,
                worker_lease_token,
                worker_lease_token,
            ),
        )
        summary = conn.execute(
            """
            SELECT COUNT(*) AS accepted,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
              FROM drama_subtitle_task_items WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE drama_subtitle_tasks
               SET completed_input_count = ?, failed_input_count = ?, updated_at = ?, last_heartbeat_at = ?
             WHERE task_id = ?
               AND (? IS NULL OR worker_lease_token = ?)
            """,
            (
                int(summary["completed"] or 0),
                int(summary["failed"] or 0),
                now,
                now,
                task_id,
                worker_lease_token,
                worker_lease_token,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def finish_drama_subtitle_task(
    *,
    db_path: str | Path,
    task_id: str,
    status: str,
    status_message: str,
    error_message: str = "",
    worker_lease_token: str | None = None,
) -> None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            """
            UPDATE drama_subtitle_tasks
               SET status = ?, status_message = ?, error_message = ?, finished_at = ?,
                   updated_at = ?, last_heartbeat_at = ?, worker_lease_token = NULL
             WHERE task_id = ?
               AND (? IS NULL OR worker_lease_token = ?)
            """,
            (
                status,
                status_message,
                error_message,
                now,
                now,
                now,
                task_id,
                worker_lease_token,
                worker_lease_token,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def settle_drama_subtitle_task_for_worker_shutdown(
    *,
    db_path: str | Path,
    task_id: str,
    worker_lease_token: str | None = None,
) -> str:
    """Persist a deterministic task state before a subtitle worker exits."""
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM drama_subtitle_tasks WHERE task_id = ? AND is_deleted = 0 AND (? IS NULL OR worker_lease_token = ?)",
            (task_id, worker_lease_token, worker_lease_token),
        ).fetchone()
        if row is None:
            conn.commit()
            return ""

        current_status = str(row["status"] or "")
        if current_status == "cancel_requested":
            next_status = "cancelled"
            conn.execute(
                """
                UPDATE drama_subtitle_tasks
                   SET status = ?, worker_name = '', worker_lease_token = NULL,
                       status_message = 'Subtitle task cancelled while the worker was stopping.',
                       finished_at = COALESCE(finished_at, ?), updated_at = ?, last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (next_status, now, now, now, task_id),
            )
        elif current_status == "pause_requested":
            next_status = "paused"
            conn.execute(
                """
                UPDATE drama_subtitle_tasks
                   SET status = ?, worker_name = '', worker_lease_token = NULL,
                       status_message = 'Subtitle task paused while the worker was stopping.',
                       paused_at = COALESCE(paused_at, ?), updated_at = ?, last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (next_status, now, now, now, task_id),
            )
        else:
            next_status = "queued"
            conn.execute(
                """
                UPDATE drama_subtitle_tasks
                   SET status = ?, worker_name = '', worker_lease_token = NULL,
                       status_message = 'Subtitle task returned to queue because the worker is stopping.',
                       paused_at = NULL, finished_at = NULL, queue_entered_at = ?,
                       updated_at = ?, last_heartbeat_at = ?
                 WHERE task_id = ?
                """,
                (next_status, now, now, now, task_id),
            )
        conn.commit()
        return next_status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_drama_subtitle_task_control(
    *,
    db_path: str | Path,
    task_id: str,
    action: str,
    owner_user_id: int | None = None,
) -> dict[str, Any] | None:
    now = now_ts()
    conn = connect_business_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sql = "SELECT * FROM drama_subtitle_tasks WHERE task_id = ? AND is_deleted = 0"
        params: list[object] = [task_id]
        if owner_user_id is not None:
            sql += " AND owner_user_id = ?"
            params.append(owner_user_id)
        row = conn.execute(sql, params).fetchone()
        if row is None:
            conn.commit()
            return None
        status = str(row["status"] or "")

        if action == "pause":
            if status == "queued":
                conn.execute(
                    "UPDATE drama_subtitle_tasks SET status = 'paused', paused_at = ?, updated_at = ?, status_message = 'Task paused before execution.' WHERE task_id = ?",
                    (now, now, task_id),
                )
            elif status == "running":
                conn.execute(
                    "UPDATE drama_subtitle_tasks SET status = 'pause_requested', updated_at = ?, status_message = 'Pause requested. The active subtitle item will finish first.' WHERE task_id = ?",
                    (now, task_id),
                )
            else:
                raise ValueError(f"cannot pause drama subtitle task in status {status!r}")
        elif action == "resume":
            if status != "paused":
                raise ValueError(f"cannot resume drama subtitle task in status {status!r}")
            conn.execute(
                "UPDATE drama_subtitle_tasks SET status = 'queued', paused_at = NULL, queue_entered_at = ?, updated_at = ?, status_message = 'Task queued for resume.' WHERE task_id = ?",
                (now, now, task_id),
            )
        elif action == "cancel":
            if status == "queued":
                conn.execute(
                    "UPDATE drama_subtitle_tasks SET status = 'cancelled', finished_at = ?, updated_at = ?, status_message = 'Task cancelled before execution.' WHERE task_id = ?",
                    (now, now, task_id),
                )
            elif status in {"running", "pause_requested"}:
                conn.execute(
                    "UPDATE drama_subtitle_tasks SET status = 'cancel_requested', updated_at = ?, status_message = 'Cancellation requested. The active subtitle item will finish first.' WHERE task_id = ?",
                    (now, task_id),
                )
            else:
                raise ValueError(f"cannot cancel drama subtitle task in status {status!r}")
        elif action == "delete":
            if status in {"running", "pause_requested", "cancel_requested"}:
                raise ValueError("cancel or pause the running subtitle task before deleting it")
            conn.execute(
                "UPDATE drama_subtitle_tasks SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE task_id = ?",
                (now, now, task_id),
            )
        else:
            raise ValueError(f"unsupported drama subtitle task action: {action}")

        updated = conn.execute("SELECT * FROM drama_subtitle_tasks WHERE task_id = ?", (task_id,)).fetchone()
        conn.commit()
        return None if updated is None else _task_row_to_dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_interrupted_drama_subtitle_tasks(
    db_path: str | Path,
    *,
    stale_after_seconds: float,
) -> dict[str, int]:
    """Return abandoned subtitle tasks to a safe terminal or queued state."""
    now = now_ts()
    now_dt = datetime.fromisoformat(now)
    summary = {
        "running_to_queued": 0,
        "cancel_requested_to_cancelled": 0,
        "pause_requested_to_paused": 0,
    }
    conn = connect_business_db(
        db_path,
        busy_timeout_ms=BACKGROUND_DB_BUSY_TIMEOUT_MS,
    )
    try:
        candidates = conn.execute(
            """
            SELECT last_heartbeat_at
              FROM drama_subtitle_tasks
             WHERE is_deleted = 0
               AND status IN ('running', 'pause_requested', 'cancel_requested')
            """
        ).fetchall()
        has_stale_candidate = False
        for candidate in candidates:
            heartbeat = str(candidate["last_heartbeat_at"] or "")
            try:
                heartbeat_dt = datetime.fromisoformat(heartbeat)
                if (now_dt - heartbeat_dt).total_seconds() >= max(float(stale_after_seconds), 1.0):
                    has_stale_candidate = True
                    break
            except ValueError:
                has_stale_candidate = True
                break
        if not has_stale_candidate:
            return summary
        if not try_begin_business_write(conn, operation="subtitle_recovery"):
            return summary
        rows = conn.execute(
            """
            SELECT task_id, status, last_heartbeat_at
              FROM drama_subtitle_tasks
             WHERE is_deleted = 0
               AND status IN ('running', 'pause_requested', 'cancel_requested')
            """
        ).fetchall()
        for row in rows:
            heartbeat = str(row["last_heartbeat_at"] or "")
            try:
                heartbeat_dt = datetime.fromisoformat(heartbeat)
                stale = (now_dt - heartbeat_dt).total_seconds() >= max(float(stale_after_seconds), 1.0)
            except ValueError:
                stale = True
            if not stale:
                continue
            task_id = str(row["task_id"])
            status = str(row["status"])
            if status == "running":
                conn.execute(
                    """
                    UPDATE drama_subtitle_tasks
                       SET status = 'queued', worker_name = '', worker_lease_token = NULL,
                           queue_entered_at = ?,
                           updated_at = ?, last_heartbeat_at = ?,
                           status_message = 'Subtitle task recovered after worker interruption and returned to queue.'
                     WHERE task_id = ?
                    """,
                    (now, now, now, task_id),
                )
                summary["running_to_queued"] += 1
            elif status == "cancel_requested":
                conn.execute(
                    """
                    UPDATE drama_subtitle_tasks
                       SET status = 'cancelled', worker_name = '', worker_lease_token = NULL,
                           finished_at = ?, updated_at = ?,
                           last_heartbeat_at = ?, status_message = 'Subtitle task cancelled after worker interruption recovery.'
                     WHERE task_id = ?
                    """,
                    (now, now, now, task_id),
                )
                summary["cancel_requested_to_cancelled"] += 1
            else:
                conn.execute(
                    """
                    UPDATE drama_subtitle_tasks
                       SET status = 'paused', worker_name = '', worker_lease_token = NULL,
                           paused_at = ?, updated_at = ?,
                           last_heartbeat_at = ?, status_message = 'Subtitle task paused after worker interruption recovery.'
                     WHERE task_id = ?
                    """,
                    (now, now, now, task_id),
                )
                summary["pause_requested_to_paused"] += 1
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
