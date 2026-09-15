from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Any
from uuid import uuid4

from service.cover_monitor.importer import WorkbookImportParser

try:
    from pysqlite3 import dbapi2 as sqlite3  # type: ignore
except Exception:
    import sqlite3


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 11
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _required_text(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _parse_aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _public_cleanup_plan(row: Any, item_rows: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("execution_token_hash", None)
    result.pop("worker_lease_hash", None)
    result["policy"] = json.loads(str(result.pop("policy_json")))
    result["items"] = [dict(item) for item in item_rows]
    return result


def _cleanup_lease_hash(value: str) -> str:
    raw_token = _required_text(value, "worker_lease_token")
    if len(raw_token) > 512:
        raise ValueError("cleanup worker lease is invalid")
    return sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoverAccessScope:
    workspace_key: str
    user_id: int

    def __post_init__(self) -> None:
        if not self.workspace_key.strip():
            raise ValueError("workspace_key is required")
        if int(self.user_id) <= 0:
            raise ValueError("user_id must be positive")


def connect_cover_db(
    path: str | Path,
    *,
    configure_wal: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=max(int(busy_timeout_ms), 1) / 1000.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(int(busy_timeout_ms), 0)}")
    if configure_wal:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_cover_db(path: str | Path) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect_cover_db(path, configure_wal=True) as conn:
        conn.executescript(schema)
        asset_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(cover_assets)").fetchall()
        }
        if "storage_backend" not in asset_columns:
            conn.execute(
                """
                ALTER TABLE cover_assets ADD COLUMN storage_backend TEXT NOT NULL
                    DEFAULT 'local' CHECK (storage_backend IN ('local', 'oss'))
                """
            )
        cleanup_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(cover_cleanup_runs)").fetchall()
        }
        if "execution_token_hash" not in cleanup_columns:
            conn.execute("ALTER TABLE cover_cleanup_runs ADD COLUMN execution_token_hash TEXT")
        if "execution_token_expires_at" not in cleanup_columns:
            conn.execute(
                "ALTER TABLE cover_cleanup_runs ADD COLUMN execution_token_expires_at TEXT"
            )
        if "worker_lease_hash" not in cleanup_columns:
            conn.execute("ALTER TABLE cover_cleanup_runs ADD COLUMN worker_lease_hash TEXT")
        if "worker_name" not in cleanup_columns:
            conn.execute("ALTER TABLE cover_cleanup_runs ADD COLUMN worker_name TEXT")
        if "last_heartbeat_at" not in cleanup_columns:
            conn.execute("ALTER TABLE cover_cleanup_runs ADD COLUMN last_heartbeat_at TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO cover_schema_versions (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )


class CoverMonitorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        init_cover_db(self.path)

    def _connect(self) -> sqlite3.Connection:
        return connect_cover_db(self.path)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_overview(self, scope: CoverAccessScope) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        with self._connect() as conn:
            channel_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_channels WHERE workspace_key = ? AND active = 1",
                    (workspace_key,),
                ).fetchone()[0]
            )
            video_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_videos WHERE workspace_key = ?",
                    (workspace_key,),
                ).fetchone()[0]
            )
            pending_review_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM cover_risk_cases
                     WHERE workspace_key = ? AND current_status IN ('open', 'needs_review')
                    """,
                    (workspace_key,),
                ).fetchone()[0]
            )
            distribution = {"safe": 0, "review": 0, "risk": 0, "unknown": 0}
            for row in conn.execute(
                """
                SELECT overall_risk, COUNT(*) AS item_count
                  FROM cover_detections
                 WHERE workspace_key = ?
                 GROUP BY overall_risk
                """,
                (workspace_key,),
            ).fetchall():
                distribution[str(row["overall_risk"])] = int(row["item_count"])
            latest_run = self._row(
                conn.execute(
                    """
                    SELECT run_id, status, trigger_type, intensity,
                           total_item_count, completed_item_count, failed_item_count,
                           status_message, created_at, started_at, finished_at
                      FROM cover_runs
                     WHERE workspace_key = ?
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    (workspace_key,),
                ).fetchone()
            )
        return {
            "channel_count": channel_count,
            "video_count": video_count,
            "risk_count": distribution["risk"],
            "pending_review_count": pending_review_count,
            "risk_distribution": distribution,
            "latest_run": latest_run,
        }

    def list_risk_cases(
        self,
        scope: CoverAccessScope,
        *,
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_status = str(status or "").strip()
        allowed_statuses = {
            "open",
            "needs_review",
            "confirmed_rectified",
            "false_positive",
            "unavailable",
            "closed",
        }
        if normalized_status and normalized_status not in allowed_statuses:
            raise ValueError("unsupported risk case status")
        safe_limit = min(max(int(limit), 1), 100)
        safe_offset = max(int(offset), 0)
        where = "risk_case.workspace_key = ?"
        params: list[Any] = [workspace_key]
        if normalized_status:
            where += " AND risk_case.current_status = ?"
            params.append(normalized_status)
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cover_risk_cases AS risk_case WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT risk_case.*, video.video_id, video.title AS video_title,
                       video.video_url, video.thumbnail_url,
                       opened.overall_risk AS opened_risk,
                       opened.summary AS opened_summary,
                       opened.evidence AS opened_evidence,
                       opened.confidence AS opened_confidence,
                       opened.asset_id AS opened_asset_id,
                       (
                           SELECT event.event_type FROM cover_case_events AS event
                            WHERE event.case_id = risk_case.case_id
                            ORDER BY event.created_at DESC, event.case_event_id DESC LIMIT 1
                       ) AS latest_event_type
                  FROM cover_risk_cases AS risk_case
                  JOIN cover_videos AS video ON video.video_pk = risk_case.video_pk
                  JOIN cover_detections AS opened ON opened.detection_id = risk_case.opened_detection_id
                 WHERE {where}
                 ORDER BY risk_case.updated_at DESC, risk_case.case_id DESC
                 LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def review_risk_case(
        self,
        scope: CoverAccessScope,
        case_id: str,
        *,
        action: str,
        reason: str,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        case_id = _required_text(case_id, "case_id")
        action = _required_text(action, "action")
        reason = _required_text(reason, "reason")[:1000]
        transitions = {
            "confirm_rectified": "confirmed_rectified",
            "false_positive": "false_positive",
            "keep_open": "needs_review",
            "mark_unavailable": "unavailable",
            "reopen": "needs_review",
        }
        if action not in transitions:
            raise ValueError("unsupported risk case review action")
        event_types = {
            "confirm_rectified": "confirmed_rectified",
            "false_positive": "false_positive",
            "keep_open": "kept_open",
            "mark_unavailable": "marked_unavailable",
            "reopen": "reopened",
        }
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            risk_case = conn.execute(
                """
                SELECT * FROM cover_risk_cases
                 WHERE case_id = ? AND workspace_key = ?
                """,
                (case_id, workspace_key),
            ).fetchone()
            if risk_case is None:
                conn.rollback()
                raise LookupError("cover risk case not found")
            current_status = str(risk_case["current_status"])
            active = current_status in {"open", "needs_review"}
            if action == "reopen":
                if active:
                    conn.rollback()
                    raise ValueError("risk case is already open")
            elif not active:
                conn.rollback()
                raise ValueError("risk case is already closed")

            latest_system_event = conn.execute(
                """
                SELECT detection_id, event_type FROM cover_case_events
                 WHERE case_id = ? AND actor_type = 'system' AND detection_id IS NOT NULL
                 ORDER BY created_at DESC, case_event_id DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            candidate_is_current = (
                latest_system_event is not None
                and str(latest_system_event["event_type"]) == "rectification_candidate"
            )
            if action == "confirm_rectified" and not candidate_is_current:
                conn.rollback()
                raise ValueError("当前案件没有可确认的换图整改候选")
            detection_id = (
                str(latest_system_event["detection_id"])
                if action == "confirm_rectified" and latest_system_event is not None
                else str(risk_case["opened_detection_id"])
            )
            target_status = transitions[action]
            closed_at = None if target_status == "needs_review" else now
            conn.execute(
                """
                UPDATE cover_risk_cases
                   SET current_status = ?, updated_at = ?, closed_at = ?
                 WHERE case_id = ? AND workspace_key = ?
                """,
                (target_status, now, closed_at, case_id, workspace_key),
            )
            conn.execute(
                """
                INSERT INTO cover_case_reviews (
                    case_id, detection_id, action, reason,
                    reviewed_by_user_id, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (case_id, detection_id, action, reason, int(scope.user_id), now),
            )
            conn.execute(
                """
                INSERT INTO cover_case_events (
                    case_id, detection_id, event_type, actor_type,
                    actor_user_id, reason, created_at
                ) VALUES (?, ?, ?, 'user', ?, ?, ?)
                """,
                (case_id, detection_id, event_types[action], int(scope.user_id), reason, now),
            )
            result = conn.execute(
                "SELECT * FROM cover_risk_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            conn.commit()
        if result is None:
            raise RuntimeError("risk case review did not return a row")
        return dict(result)

    def get_risk_case_detail(
        self,
        scope: CoverAccessScope,
        case_id: str,
    ) -> dict[str, Any] | None:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        case_id = _required_text(case_id, "case_id")
        with self._connect() as conn:
            risk_case = conn.execute(
                """
                SELECT risk_case.*, video.video_id, video.title AS video_title,
                       video.video_url, video.thumbnail_url,
                       opened.overall_risk AS opened_risk,
                       opened.summary AS opened_summary,
                       opened.evidence AS opened_evidence,
                       opened.confidence AS opened_confidence,
                       opened.asset_id AS opened_asset_id
                  FROM cover_risk_cases AS risk_case
                  JOIN cover_videos AS video ON video.video_pk = risk_case.video_pk
                  JOIN cover_detections AS opened ON opened.detection_id = risk_case.opened_detection_id
                 WHERE risk_case.case_id = ? AND risk_case.workspace_key = ?
                """,
                (case_id, workspace_key),
            ).fetchone()
            if risk_case is None:
                return None
            events = conn.execute(
                """
                SELECT event.case_event_id, event.detection_id, event.event_type,
                       event.actor_type, event.actor_user_id, event.reason, event.created_at,
                       detection.overall_risk, detection.risk_tags_json,
                       detection.summary, detection.evidence, detection.confidence,
                       detection.provider, detection.model, detection.duration_seconds,
                       asset.asset_id, asset.content_sha256, asset.storage_key,
                       asset.original_url, asset.fetched_url, asset.width, asset.height,
                       asset.fetched_at
                  FROM cover_case_events AS event
                  LEFT JOIN cover_detections AS detection
                    ON detection.detection_id = event.detection_id
                  LEFT JOIN cover_assets AS asset ON asset.asset_id = detection.asset_id
                 WHERE event.case_id = ?
                 ORDER BY event.created_at, event.case_event_id
                """,
                (case_id,),
            ).fetchall()
            reviews = conn.execute(
                """
                SELECT case_review_id, detection_id, action, reason,
                       reviewed_by_user_id, reviewed_at
                  FROM cover_case_reviews
                 WHERE case_id = ?
                 ORDER BY reviewed_at, case_review_id
                """,
                (case_id,),
            ).fetchall()
        event_items: list[dict[str, Any]] = []
        for row in events:
            item = dict(row)
            raw_tags = str(item.pop("risk_tags_json", "") or "")
            try:
                item["risk_tags"] = list(json.loads(raw_tags)) if raw_tags else []
            except (TypeError, ValueError):
                item["risk_tags"] = []
            event_items.append(item)
        return {
            "case": dict(risk_case),
            "events": event_items,
            "reviews": [dict(row) for row in reviews],
        }

    def get_risk_case_asset(
        self,
        scope: CoverAccessScope,
        case_id: str,
        asset_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT asset.*
                  FROM cover_risk_cases AS risk_case
                  JOIN cover_case_events AS event ON event.case_id = risk_case.case_id
                  JOIN cover_detections AS detection ON detection.detection_id = event.detection_id
                  JOIN cover_assets AS asset ON asset.asset_id = detection.asset_id
                 WHERE risk_case.case_id = ? AND risk_case.workspace_key = ?
                   AND asset.asset_id = ? AND asset.workspace_key = risk_case.workspace_key
                 LIMIT 1
                """,
                (
                    _required_text(case_id, "case_id"),
                    _required_text(scope.workspace_key, "workspace_key"),
                    _required_text(asset_id, "asset_id"),
                ),
            ).fetchone()
        return self._row(row)

    def create_import_preview(
        self,
        scope: CoverAccessScope,
        *,
        import_kind: str,
        source_file_name: str,
        source_file_sha256: str,
        source_file_path: str,
        sheet_name: str = "",
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        source_file_sha256 = _required_text(source_file_sha256, "source_file_sha256")
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM cover_import_batches
                 WHERE workspace_key = ? AND import_kind = ? AND source_file_sha256 = ?
                """,
                (workspace_key, import_kind, source_file_sha256),
            ).fetchone()
        if existing is not None:
            return self.get_import(scope, str(existing["import_id"])) or dict(existing)

        import_id = str(uuid4())
        now = _now()
        with self._connect() as conn:
            existing_channels = {
                str(row["channel_id"]): {
                    "channel_id": str(row["channel_id"]),
                    "operator_name": str(row["operator_name"] or ""),
                }
                for row in conn.execute(
                    """
                    SELECT channel.channel_id, operator.name AS operator_name
                      FROM cover_channels AS channel
                      LEFT JOIN cover_operators AS operator ON operator.operator_pk = channel.operator_pk
                     WHERE channel.workspace_key = ? AND channel.platform = 'youtube'
                    """,
                    (workspace_key,),
                ).fetchall()
            }
            existing_videos = {
                str(row["video_id"]): {
                    "video_id": str(row["video_id"]),
                    "channel_id": str(row["channel_id"]),
                }
                for row in conn.execute(
                    """
                    SELECT video.video_id, channel.channel_id
                      FROM cover_videos AS video
                      JOIN cover_channels AS channel ON channel.channel_pk = video.channel_pk
                     WHERE video.workspace_key = ? AND video.platform = 'youtube'
                    """,
                    (workspace_key,),
                ).fetchall()
            }
            parser = WorkbookImportParser(
                source_file_path,
                import_kind=import_kind,
                sheet_name=sheet_name,
                existing_channels=existing_channels,
                existing_videos=existing_videos,
            )
            conn.execute(
                """
                INSERT INTO cover_import_batches (
                    import_id, workspace_key, import_kind, status, source_file_name,
                    source_file_sha256, source_file_path, sheet_name, mapping_json,
                    stats_json, created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'uploaded', ?, ?, ?, NULL, '{}', '{}', ?, ?, ?)
                """,
                (
                    import_id,
                    workspace_key,
                    import_kind,
                    Path(source_file_name).name,
                    source_file_sha256,
                    str(source_file_path),
                    int(scope.user_id),
                    now,
                    now,
                ),
            )
            try:
                for item in parser.rows():
                    conn.execute(
                        """
                        INSERT INTO cover_import_rows (
                            import_id, source_sheet, source_row, status, normalized_key,
                            raw_json, normalized_json, error_message, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            import_id,
                            item.source_sheet,
                            item.source_row,
                            item.status,
                            item.normalized_key or None,
                            json.dumps(item.raw, ensure_ascii=False, default=str),
                            json.dumps(item.normalized, ensure_ascii=False),
                            item.error_message or None,
                            now,
                        ),
                    )
                    for conflict in item.conflicts:
                        conn.execute(
                            """
                            INSERT INTO cover_import_conflicts (
                                import_id, source_sheet, source_row, conflict_type,
                                natural_key, existing_json, incoming_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                import_id,
                                item.source_sheet,
                                item.source_row,
                                conflict["conflict_type"],
                                conflict["natural_key"],
                                json.dumps(conflict["existing"], ensure_ascii=False),
                                json.dumps(conflict["incoming"], ensure_ascii=False),
                                now,
                            ),
                        )
                conn.execute(
                    """
                    UPDATE cover_import_batches
                       SET status = 'previewed', sheet_name = ?, mapping_json = ?,
                           stats_json = ?, updated_at = ?
                     WHERE import_id = ?
                    """,
                    (
                        parser.sheet_name,
                        json.dumps(parser.mapping, ensure_ascii=False),
                        json.dumps(parser.stats, ensure_ascii=False),
                        _now(),
                        import_id,
                    ),
                )
            except Exception as exc:
                conn.execute(
                    """
                    UPDATE cover_import_batches
                       SET status = 'failed', error_message = ?, updated_at = ?
                     WHERE import_id = ?
                    """,
                    (str(exc), _now(), import_id),
                )
                raise
        result = self.get_import(scope, import_id)
        if result is None:
            raise RuntimeError("import preview did not return a row")
        return result

    def get_import(self, scope: CoverAccessScope, import_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cover_import_batches WHERE import_id = ? AND workspace_key = ?",
                (import_id, scope.workspace_key.strip()),
            ).fetchone()
            if row is None:
                return None
            conflict_samples = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT source_sheet, source_row, conflict_type, natural_key,
                           existing_json, incoming_json, resolution_status
                      FROM cover_import_conflicts
                     WHERE import_id = ?
                     ORDER BY conflict_id
                     LIMIT 20
                    """,
                    (import_id,),
                ).fetchall()
            ]
        result = dict(row)
        result["mapping"] = json.loads(result.pop("mapping_json") or "{}")
        result["stats"] = json.loads(result.pop("stats_json") or "{}")
        result["conflict_samples"] = conflict_samples
        return result

    def confirm_import(self, scope: CoverAccessScope, import_id: str) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        now = _now()
        with self._connect() as conn:
            batch = conn.execute(
                "SELECT * FROM cover_import_batches WHERE import_id = ? AND workspace_key = ?",
                (import_id, workspace_key),
            ).fetchone()
            if batch is None:
                raise LookupError("import not found")
            if batch["status"] == "completed":
                return self.get_import(scope, import_id) or dict(batch)
            if batch["status"] != "previewed":
                raise ValueError("only previewed imports can be confirmed")
            conn.execute(
                "UPDATE cover_import_batches SET status = 'running', confirmed_at = ?, updated_at = ? WHERE import_id = ?",
                (now, now, import_id),
            )
            applied_channels: set[int] = set()
            applied_videos: set[int] = set()
            historical_observations = 0
            skipped_conflicts = 0
            rows = conn.execute(
                """
                SELECT import_row_id, source_sheet, source_row, normalized_json
                  FROM cover_import_rows
                 WHERE import_id = ? AND status = 'valid'
                 ORDER BY import_row_id
                """,
                (import_id,),
            )
            for row in rows:
                item = json.loads(row["normalized_json"])
                operator_pk = self._upsert_import_operator(conn, scope, item.get("operator_name", ""), now)
                channel_pk = self._upsert_import_channel(
                    conn,
                    scope,
                    item,
                    operator_pk,
                    now,
                    import_id=import_id,
                    source_sheet=row["source_sheet"],
                    source_row=int(row["source_row"]),
                )
                if channel_pk is None:
                    skipped_conflicts += 1
                    continue
                applied_channels.add(channel_pk)
                if batch["import_kind"] == "channels":
                    conn.execute(
                        "UPDATE cover_import_rows SET status = 'imported' WHERE import_row_id = ?",
                        (row["import_row_id"],),
                    )
                    continue
                video_pk = self._upsert_import_video(
                    conn,
                    scope,
                    channel_pk,
                    item,
                    now,
                    import_id=import_id,
                    source_sheet=row["source_sheet"],
                    source_row=int(row["source_row"]),
                )
                if video_pk is None:
                    skipped_conflicts += 1
                    continue
                applied_videos.add(video_pk)
                if item.get("overall_risk"):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO cover_historical_observations (
                            workspace_key, video_pk, import_row_id, overall_risk,
                            risk_tags_json, summary, evidence, confidence,
                            evidence_status, model_version, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'evidence_missing', 'legacy_unknown', ?)
                        """,
                        (
                            workspace_key,
                            video_pk,
                            row["import_row_id"],
                            item["overall_risk"],
                            json.dumps(self._split_tags(item.get("risk_tags", "")), ensure_ascii=False),
                            item.get("summary") or None,
                            item.get("evidence") or None,
                            item.get("confidence"),
                            now,
                        ),
                    )
                    historical_observations += 1
                conn.execute(
                    "UPDATE cover_import_rows SET status = 'imported' WHERE import_row_id = ?",
                    (row["import_row_id"],),
                )
            stats = json.loads(batch["stats_json"] or "{}")
            stats.update({
                "applied_channels": len(applied_channels),
                "applied_videos": len(applied_videos),
                "historical_observations": historical_observations,
                "skipped_conflicts": skipped_conflicts,
            })
            conn.execute(
                """
                UPDATE cover_import_batches
                   SET status = 'completed', stats_json = ?, updated_at = ?, finished_at = ?
                 WHERE import_id = ?
                """,
                (json.dumps(stats, ensure_ascii=False), _now(), _now(), import_id),
            )
        result = self.get_import(scope, import_id)
        if result is None:
            raise RuntimeError("confirmed import did not return a row")
        return result

    @staticmethod
    def _split_tags(value: str) -> list[str]:
        return [item.strip() for item in re.split(r"[,，;；|]", value or "") if item.strip()]

    @staticmethod
    def _upsert_import_operator(
        conn: sqlite3.Connection,
        scope: CoverAccessScope,
        name: str,
        now: str,
    ) -> int | None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return None
        conn.execute(
            """
            INSERT INTO cover_operators (
                workspace_key, name, active, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT (workspace_key, name) DO UPDATE SET
                active = 1, updated_at = excluded.updated_at
            """,
            (scope.workspace_key, normalized_name, int(scope.user_id), now, now),
        )
        row = conn.execute(
            "SELECT operator_pk FROM cover_operators WHERE workspace_key = ? AND name = ?",
            (scope.workspace_key, normalized_name),
        ).fetchone()
        return int(row["operator_pk"])

    @staticmethod
    def _record_import_conflict(
        conn: sqlite3.Connection,
        *,
        import_id: str,
        source_sheet: str,
        source_row: int,
        conflict_type: str,
        natural_key: str,
        existing: dict[str, Any],
        incoming: dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO cover_import_conflicts (
                import_id, source_sheet, source_row, conflict_type, natural_key,
                existing_json, incoming_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_id,
                source_sheet,
                source_row,
                conflict_type,
                natural_key,
                json.dumps(existing, ensure_ascii=False),
                json.dumps(incoming, ensure_ascii=False),
                now,
            ),
        )

    def _upsert_import_channel(
        self,
        conn: sqlite3.Connection,
        scope: CoverAccessScope,
        item: dict[str, Any],
        operator_pk: int | None,
        now: str,
        *,
        import_id: str,
        source_sheet: str,
        source_row: int,
    ) -> int | None:
        existing = conn.execute(
            """
            SELECT channel_pk, channel_id, name, source_url, operator_pk
              FROM cover_channels
             WHERE workspace_key = ? AND platform = ? AND channel_id = ?
            """,
            (scope.workspace_key, item["platform"], item["channel_id"]),
        ).fetchone()
        if (
            existing is not None
            and existing["operator_pk"] is not None
            and operator_pk is not None
            and int(existing["operator_pk"]) != operator_pk
        ):
            self._record_import_conflict(
                conn,
                import_id=import_id,
                source_sheet=source_sheet,
                source_row=source_row,
                conflict_type="channel_operator_mismatch_existing",
                natural_key=item["channel_id"],
                existing=dict(existing),
                incoming=item,
                now=now,
            )
            return None
        conn.execute(
            """
            INSERT INTO cover_channels (
                workspace_key, platform, channel_id, name, source_url, operator_pk,
                active, first_seen_at, last_seen_at, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT (workspace_key, platform, channel_id) DO UPDATE SET
                name = excluded.name,
                source_url = excluded.source_url,
                operator_pk = COALESCE(excluded.operator_pk, cover_channels.operator_pk),
                active = 1,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                scope.workspace_key,
                item["platform"],
                item["channel_id"],
                item["channel_name"],
                item["channel_url"],
                operator_pk,
                now,
                now,
                int(scope.user_id),
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT channel_pk FROM cover_channels
             WHERE workspace_key = ? AND platform = ? AND channel_id = ?
            """,
            (scope.workspace_key, item["platform"], item["channel_id"]),
        ).fetchone()
        return int(row["channel_pk"])

    def _upsert_import_video(
        self,
        conn: sqlite3.Connection,
        scope: CoverAccessScope,
        channel_pk: int,
        item: dict[str, Any],
        now: str,
        *,
        import_id: str,
        source_sheet: str,
        source_row: int,
    ) -> int | None:
        existing = conn.execute(
            """
            SELECT video_pk, video_id, channel_pk, title, video_url
              FROM cover_videos
             WHERE workspace_key = ? AND platform = ? AND video_id = ?
            """,
            (scope.workspace_key, item["platform"], item["video_id"]),
        ).fetchone()
        if existing is not None and int(existing["channel_pk"]) != channel_pk:
            self._record_import_conflict(
                conn,
                import_id=import_id,
                source_sheet=source_sheet,
                source_row=source_row,
                conflict_type="video_channel_mismatch_existing",
                natural_key=item["video_id"],
                existing=dict(existing),
                incoming=item,
                now=now,
            )
            return None
        conn.execute(
            """
            INSERT INTO cover_videos (
                workspace_key, platform, video_id, channel_pk, title, video_url,
                thumbnail_url, upload_date, first_seen_at, last_seen_at,
                created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (workspace_key, platform, video_id) DO UPDATE SET
                title = excluded.title,
                video_url = excluded.video_url,
                thumbnail_url = excluded.thumbnail_url,
                upload_date = COALESCE(excluded.upload_date, cover_videos.upload_date),
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                scope.workspace_key,
                item["platform"],
                item["video_id"],
                channel_pk,
                item["video_title"],
                item["video_url"],
                item["thumbnail_url"],
                item.get("upload_date") or None,
                now,
                now,
                int(scope.user_id),
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT video_pk FROM cover_videos
             WHERE workspace_key = ? AND platform = ? AND video_id = ?
            """,
            (scope.workspace_key, item["platform"], item["video_id"]),
        ).fetchone()
        return int(row["video_pk"])

    def upsert_channel(
        self,
        scope: CoverAccessScope,
        *,
        platform: str,
        channel_id: str,
        name: str,
        source_url: str,
        operator_pk: int | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        platform = _required_text(platform, "platform").lower()
        channel_id = _required_text(channel_id, "channel_id")
        name = _required_text(name, "name")
        source_url = _required_text(source_url, "source_url")
        now = _now()
        with self._connect() as conn:
            if operator_pk is not None:
                operator = conn.execute(
                    "SELECT operator_pk FROM cover_operators WHERE operator_pk = ? AND workspace_key = ?",
                    (operator_pk, workspace_key),
                ).fetchone()
                if operator is None:
                    raise ValueError("operator is not visible in this workspace")
            conn.execute(
                """
                INSERT INTO cover_channels (
                    workspace_key, platform, channel_id, name, source_url, operator_pk,
                    active, first_seen_at, last_seen_at, created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace_key, platform, channel_id) DO UPDATE SET
                    name = excluded.name,
                    source_url = excluded.source_url,
                    operator_pk = COALESCE(excluded.operator_pk, cover_channels.operator_pk),
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_key,
                    platform,
                    channel_id,
                    name,
                    source_url,
                    operator_pk,
                    now,
                    now,
                    int(scope.user_id),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM cover_channels
                 WHERE workspace_key = ? AND platform = ? AND channel_id = ?
                """,
                (workspace_key, platform, channel_id),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("channel upsert did not return a row")
        return result

    def get_channel(
        self, scope: CoverAccessScope, channel_pk: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cover_channels WHERE channel_pk = ? AND workspace_key = ?",
                (int(channel_pk), scope.workspace_key.strip()),
            ).fetchone()
        return self._row(row)

    def list_channels(
        self,
        scope: CoverAccessScope,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 500)
        safe_offset = max(int(offset), 0)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cover_channels
                 WHERE workspace_key = ?
                 ORDER BY updated_at DESC, channel_pk DESC
                 LIMIT ? OFFSET ?
                """,
                (scope.workspace_key.strip(), safe_limit, safe_offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_channels(
        self,
        scope: CoverAccessScope,
        *,
        keyword: str = "",
        active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        safe_limit = min(max(int(limit), 1), 100)
        safe_offset = max(int(offset), 0)
        clauses = ["channel.workspace_key = ?"]
        params: list[Any] = [workspace_key]
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            escaped = normalized_keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(channel.name LIKE ? ESCAPE '\\' OR channel.channel_id LIKE ? ESCAPE '\\' "
                "OR operator.name LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern))
        if active is not None:
            clauses.append("channel.active = ?")
            params.append(1 if active else 0)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                      FROM cover_channels AS channel
                      LEFT JOIN cover_operators AS operator
                        ON operator.operator_pk = channel.operator_pk
                     WHERE {where}
                    """,
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT channel.channel_pk, channel.platform, channel.channel_id,
                       channel.name, channel.source_url, channel.active,
                       channel.operator_pk, operator.name AS operator_name,
                       channel.last_scan_at, channel.updated_at,
                       (SELECT COUNT(*) FROM cover_videos AS video
                         WHERE video.workspace_key = channel.workspace_key
                           AND video.channel_pk = channel.channel_pk) AS video_count,
                       (SELECT COUNT(*)
                          FROM cover_risk_cases AS risk_case
                          JOIN cover_videos AS risk_video
                            ON risk_video.video_pk = risk_case.video_pk
                         WHERE risk_case.workspace_key = channel.workspace_key
                           AND risk_video.channel_pk = channel.channel_pk
                           AND risk_case.current_status IN ('open', 'needs_review')) AS open_case_count,
                       (SELECT run_channel.scan_status
                          FROM cover_run_channels AS run_channel
                          JOIN cover_runs AS run ON run.run_id = run_channel.run_id
                         WHERE run_channel.channel_pk = channel.channel_pk
                           AND run.workspace_key = channel.workspace_key
                         ORDER BY run.created_at DESC, run_channel.run_channel_id DESC
                         LIMIT 1) AS latest_scan_status,
                       (SELECT run_channel.completeness
                          FROM cover_run_channels AS run_channel
                          JOIN cover_runs AS run ON run.run_id = run_channel.run_id
                         WHERE run_channel.channel_pk = channel.channel_pk
                           AND run.workspace_key = channel.workspace_key
                         ORDER BY run.created_at DESC, run_channel.run_channel_id DESC
                         LIMIT 1) AS latest_scan_completeness
                  FROM cover_channels AS channel
                  LEFT JOIN cover_operators AS operator
                    ON operator.operator_pk = channel.operator_pk
                 WHERE {where}
                 ORDER BY channel.updated_at DESC, channel.channel_pk DESC
                 LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["active"] = bool(item["active"])
        return {"items": items, "total": total, "limit": safe_limit, "offset": safe_offset}

    def upsert_video(
        self,
        scope: CoverAccessScope,
        *,
        channel_pk: int,
        platform: str,
        video_id: str,
        title: str,
        video_url: str,
        thumbnail_url: str,
        upload_date: str | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        platform = _required_text(platform, "platform").lower()
        video_id = _required_text(video_id, "video_id")
        title = _required_text(title, "title")
        video_url = _required_text(video_url, "video_url")
        thumbnail_url = _required_text(thumbnail_url, "thumbnail_url")
        now = _now()
        with self._connect() as conn:
            channel = conn.execute(
                "SELECT channel_pk FROM cover_channels WHERE channel_pk = ? AND workspace_key = ?",
                (int(channel_pk), workspace_key),
            ).fetchone()
            if channel is None:
                raise ValueError("channel is not visible in this workspace")
            conn.execute(
                """
                INSERT INTO cover_videos (
                    workspace_key, platform, video_id, channel_pk, title, video_url,
                    thumbnail_url, upload_date, first_seen_at, last_seen_at,
                    created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace_key, platform, video_id) DO UPDATE SET
                    channel_pk = excluded.channel_pk,
                    title = excluded.title,
                    video_url = excluded.video_url,
                    thumbnail_url = excluded.thumbnail_url,
                    upload_date = COALESCE(excluded.upload_date, cover_videos.upload_date),
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_key,
                    platform,
                    video_id,
                    int(channel_pk),
                    title,
                    video_url,
                    thumbnail_url,
                    upload_date,
                    now,
                    now,
                    int(scope.user_id),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM cover_videos
                 WHERE workspace_key = ? AND platform = ? AND video_id = ?
                """,
                (workspace_key, platform, video_id),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("video upsert did not return a row")
        return result

    def create_run(
        self,
        scope: CoverAccessScope,
        *,
        trigger_type: str,
        intensity: str,
        channel_pks: list[int] | None,
        params: dict[str, Any],
        model_snapshot: dict[str, Any],
        prompt_version: str,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        trigger_type = _required_text(trigger_type, "trigger_type")
        intensity = _required_text(intensity, "intensity")
        prompt_version = _required_text(prompt_version, "prompt_version")
        if trigger_type not in {"manual", "schedule"}:
            raise ValueError("unsupported trigger_type")
        if intensity not in {"conservative", "standard", "strict"}:
            raise ValueError("unsupported intensity")
        requested_channel_pks = sorted({int(value) for value in channel_pks or [] if int(value) > 0})
        now = _now()
        run_id = str(uuid4())
        with self._connect() as conn:
            if requested_channel_pks:
                placeholders = ",".join("?" for _ in requested_channel_pks)
                channels = conn.execute(
                    f"""
                    SELECT channel.channel_pk, channel.channel_id, channel.name, channel.source_url,
                           operator.operator_pk, operator.name AS operator_name
                      FROM cover_channels AS channel
                      LEFT JOIN cover_operators AS operator ON operator.operator_pk = channel.operator_pk
                     WHERE channel.workspace_key = ? AND channel.active = 1
                       AND channel.channel_pk IN ({placeholders})
                     ORDER BY channel.channel_pk
                    """,
                    (workspace_key, *requested_channel_pks),
                ).fetchall()
                if len(channels) != len(requested_channel_pks):
                    raise ValueError("one or more channels are not visible in this workspace")
            else:
                channels = conn.execute(
                    """
                    SELECT channel.channel_pk, channel.channel_id, channel.name, channel.source_url,
                           operator.operator_pk, operator.name AS operator_name
                      FROM cover_channels AS channel
                      LEFT JOIN cover_operators AS operator ON operator.operator_pk = channel.operator_pk
                     WHERE channel.workspace_key = ? AND channel.active = 1
                     ORDER BY channel.channel_pk
                    """,
                    (workspace_key,),
                ).fetchall()
            if not channels:
                raise ValueError("no active channels selected")
            channel_ids = [int(row["channel_pk"]) for row in channels]
            safe_model_snapshot = {
                str(key): value
                for key, value in dict(model_snapshot or {}).items()
                if str(key).casefold() not in {"api_key", "token", "secret", "password"}
            }
            conn.execute(
                """
                INSERT INTO cover_runs (
                    run_id, workspace_key, status, trigger_type, intensity,
                    params_json, scope_snapshot_json, model_snapshot_json, prompt_version,
                    total_item_count, completed_item_count, failed_item_count,
                    status_message, created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    workspace_key,
                    trigger_type,
                    intensity,
                    json.dumps(dict(params or {}), ensure_ascii=False, sort_keys=True),
                    json.dumps({"channel_pks": channel_ids}, ensure_ascii=False, sort_keys=True),
                    json.dumps(safe_model_snapshot, ensure_ascii=False, sort_keys=True),
                    prompt_version,
                    f"已排队，待扫描 {len(channels)} 个频道",
                    int(scope.user_id),
                    now,
                    now,
                ),
            )
            for row in channels:
                operator_snapshot = {
                    "operator_pk": row["operator_pk"],
                    "operator_name": str(row["operator_name"] or ""),
                    "channel_id": str(row["channel_id"]),
                    "channel_name": str(row["name"]),
                    "source_url": str(row["source_url"]),
                }
                conn.execute(
                    """
                    INSERT INTO cover_run_channels (
                        run_id, channel_pk, operator_snapshot_json, scan_status, completeness
                    ) VALUES (?, ?, ?, 'pending', 'pending')
                    """,
                    (run_id, int(row["channel_pk"]), json.dumps(operator_snapshot, ensure_ascii=False, sort_keys=True)),
                )
        result = self.get_run(scope, run_id)
        if result is None:
            raise RuntimeError("run creation did not return a row")
        return result

    def get_run(self, scope: CoverAccessScope, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run.*,
                       (SELECT COUNT(*) FROM cover_run_channels WHERE run_id = run.run_id)
                           AS total_channel_count
                  FROM cover_runs AS run
                 WHERE run.run_id = ? AND run.workspace_key = ?
                """,
                (_required_text(run_id, "run_id"), scope.workspace_key.strip()),
            ).fetchone()
        return self._row(row)

    def list_runs(
        self,
        scope: CoverAccessScope,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = min(max(int(limit), 1), 100)
        safe_offset = max(int(offset), 0)
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        with self._connect() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_runs WHERE workspace_key = ?",
                    (workspace_key,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT run.*,
                       (SELECT COUNT(*) FROM cover_run_channels WHERE run_id = run.run_id)
                           AS total_channel_count
                  FROM cover_runs AS run
                 WHERE run.workspace_key = ?
                 ORDER BY run.created_at DESC, run.run_id DESC
                 LIMIT ? OFFSET ?
                """,
                (workspace_key, safe_limit, safe_offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_run_detail(
        self,
        scope: CoverAccessScope,
        run_id: str,
        *,
        item_limit: int = 50,
        item_offset: int = 0,
    ) -> dict[str, Any] | None:
        run = self.get_run(scope, run_id)
        if run is None:
            return None
        safe_limit = min(max(int(item_limit), 1), 100)
        safe_offset = max(int(item_offset), 0)
        with self._connect() as conn:
            channels = conn.execute(
                """
                SELECT run_channel.run_channel_id, run_channel.channel_pk,
                       channel.name AS channel_name, channel.source_url,
                       run_channel.scan_status, run_channel.completeness,
                       run_channel.discovered_count, run_channel.error_message
                  FROM cover_run_channels AS run_channel
                  JOIN cover_channels AS channel ON channel.channel_pk = run_channel.channel_pk
                 WHERE run_channel.run_id = ? AND channel.workspace_key = ?
                 ORDER BY run_channel.run_channel_id
                """,
                (run_id, scope.workspace_key.strip()),
            ).fetchall()
            item_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_task_items WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            items = conn.execute(
                """
                SELECT item.task_item_id, item.video_pk, item.reason, item.stage,
                       item.status, item.attempts, item.error_type, item.error_message,
                       item.created_at, item.started_at, item.finished_at,
                       video.video_id, video.title AS video_title, video.video_url,
                       video.thumbnail_url, detection.overall_risk,
                       detection.confidence, detection.summary
                  FROM cover_task_items AS item
                  JOIN cover_videos AS video ON video.video_pk = item.video_pk
                  LEFT JOIN cover_detections AS detection
                    ON detection.task_item_id = item.task_item_id
                 WHERE item.run_id = ? AND video.workspace_key = ?
                 ORDER BY item.task_item_id
                 LIMIT ? OFFSET ?
                """,
                (run_id, scope.workspace_key.strip(), safe_limit, safe_offset),
            ).fetchall()
        return {
            "run": run,
            "channels": [dict(row) for row in channels],
            "items": [dict(row) for row in items],
            "item_total": item_total,
            "item_limit": safe_limit,
            "item_offset": safe_offset,
        }

    def get_run_report_data(
        self,
        scope: CoverAccessScope,
        run_id: str,
    ) -> dict[str, Any] | None:
        run = self.get_run(scope, run_id)
        if run is None:
            return None
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        with self._connect() as conn:
            channel_rows = conn.execute(
                """
                SELECT channel_pk, operator_snapshot_json, scan_status, completeness,
                       discovered_count, error_message
                  FROM cover_run_channels
                 WHERE run_id = ?
                 ORDER BY run_channel_id
                """,
                (run_id,),
            ).fetchall()
            rows = conn.execute(
                """
                SELECT item.task_item_id, item.reason, item.stage, item.status,
                       item.attempts, item.error_type, item.error_message,
                       item.created_at AS item_created_at, item.finished_at AS item_finished_at,
                       video.channel_pk, video.video_pk, video.video_id,
                       video.title AS video_title, video.video_url, video.thumbnail_url,
                       video.upload_date,
                       detection.detection_id, detection.overall_risk,
                       detection.risk_tags_json, detection.summary, detection.evidence,
                       detection.confidence, detection.provider, detection.model,
                       detection.prompt_version, detection.duration_seconds,
                       detection.finished_at AS detected_at,
                       asset.asset_id, asset.content_sha256, asset.original_url,
                       asset.fetched_url, asset.width, asset.height, asset.fetched_at,
                       risk_case.case_id, risk_case.current_status AS case_status,
                       risk_case.opened_at AS case_opened_at,
                       (SELECT event.event_type
                          FROM cover_case_events AS event
                         WHERE event.case_id = risk_case.case_id
                         ORDER BY event.created_at DESC, event.case_event_id DESC
                         LIMIT 1) AS latest_case_event
                  FROM cover_task_items AS item
                  JOIN cover_videos AS video ON video.video_pk = item.video_pk
                  LEFT JOIN cover_detections AS detection
                    ON detection.task_item_id = item.task_item_id
                  LEFT JOIN cover_assets AS asset ON asset.asset_id = detection.asset_id
                  LEFT JOIN cover_risk_cases AS risk_case
                    ON risk_case.case_id = (
                        SELECT candidate.case_id
                          FROM cover_risk_cases AS candidate
                         WHERE candidate.workspace_key = ?
                           AND candidate.video_pk = item.video_pk
                         ORDER BY candidate.updated_at DESC, candidate.case_id DESC
                         LIMIT 1
                    )
                 WHERE item.run_id = ? AND video.workspace_key = ?
                 ORDER BY item.task_item_id
                """,
                (workspace_key, run_id, workspace_key),
            ).fetchall()
        channels: list[dict[str, Any]] = []
        snapshots: dict[int, dict[str, Any]] = {}
        for channel_row in channel_rows:
            channel = dict(channel_row)
            try:
                snapshot = json.loads(str(channel.pop("operator_snapshot_json") or "{}"))
            except json.JSONDecodeError:
                snapshot = {}
            channel["snapshot"] = snapshot if isinstance(snapshot, dict) else {}
            channels.append(channel)
            snapshots[int(channel["channel_pk"])] = channel["snapshot"]
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                risk_tags = json.loads(str(item.pop("risk_tags_json") or "[]"))
            except json.JSONDecodeError:
                risk_tags = []
            item["risk_tags"] = risk_tags if isinstance(risk_tags, list) else []
            item["channel_snapshot"] = dict(snapshots.get(int(item["channel_pk"]), {}))
            items.append(item)
        return {"run": run, "channels": channels, "items": items}

    def claim_next_run(self, *, worker_name: str) -> dict[str, Any] | None:
        worker_name = _required_text(worker_name, "worker_name")
        lease_token = uuid4().hex
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT run_id FROM cover_runs
                 WHERE status = 'queued'
                 ORDER BY created_at, run_id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            run_id = str(row["run_id"])
            updated = conn.execute(
                """
                UPDATE cover_runs
                   SET status = 'running', worker_name = ?, worker_lease_token = ?,
                       last_heartbeat_at = ?, started_at = COALESCE(started_at, ?),
                       status_message = '正在执行', updated_at = ?
                 WHERE run_id = ? AND status = 'queued'
                """,
                (worker_name, lease_token, now, now, now, run_id),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            claimed = conn.execute("SELECT * FROM cover_runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.commit()
        return self._row(claimed)

    def heartbeat_run(self, run_id: str, worker_lease_token: str) -> bool:
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_runs
                   SET last_heartbeat_at = ?, updated_at = ?
                 WHERE run_id = ? AND worker_lease_token = ?
                   AND status IN ('running', 'pause_requested', 'cancel_requested')
                """,
                (now, now, _required_text(run_id, "run_id"), _required_text(worker_lease_token, "worker_lease_token")),
            ).rowcount
        return updated == 1

    def enqueue_task_items(
        self,
        run_id: str,
        worker_lease_token: str,
        items: list[dict[str, Any]],
    ) -> int:
        run_id = _required_text(run_id, "run_id")
        worker_lease_token = _required_text(worker_lease_token, "worker_lease_token")
        normalized: dict[tuple[int, str], None] = {}
        for item in items:
            video_pk = int(item.get("video_pk") or 0)
            reason = _required_text(str(item.get("reason") or ""), "reason")
            if video_pk <= 0:
                raise ValueError("video_pk must be positive")
            if reason not in {"new_video", "historical_risk", "retry_unknown", "manual"}:
                raise ValueError("unsupported task item reason")
            normalized[(video_pk, reason)] = None
        if not normalized:
            return 0

        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                """
                SELECT workspace_key FROM cover_runs
                 WHERE run_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (run_id, worker_lease_token),
            ).fetchone()
            if run is None:
                conn.rollback()
                return 0
            workspace_key = str(run["workspace_key"])
            inserted = 0
            for video_pk, reason in normalized:
                video = conn.execute(
                    "SELECT 1 FROM cover_videos WHERE video_pk = ? AND workspace_key = ?",
                    (video_pk, workspace_key),
                ).fetchone()
                if video is None:
                    conn.rollback()
                    raise ValueError("video is not visible in this run workspace")
                inserted += conn.execute(
                    """
                    INSERT OR IGNORE INTO cover_task_items (
                        run_id, video_pk, reason, stage, status, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, 'download', 'queued', 0, ?, ?)
                    """,
                    (run_id, video_pk, reason, now, now),
                ).rowcount
            conn.execute(
                """
                UPDATE cover_runs
                   SET total_item_count = (
                           SELECT COUNT(*) FROM cover_task_items WHERE run_id = ?
                       ), updated_at = ?
                 WHERE run_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (run_id, now, run_id, worker_lease_token),
            )
            conn.commit()
        return inserted

    def list_run_channels(self, run_id: str, worker_lease_token: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            run = conn.execute(
                """
                SELECT workspace_key FROM cover_runs
                 WHERE run_id = ? AND worker_lease_token = ?
                   AND status IN ('running', 'pause_requested', 'cancel_requested')
                """,
                (_required_text(run_id, "run_id"), _required_text(worker_lease_token, "worker_lease_token")),
            ).fetchone()
            if run is None:
                return []
            rows = conn.execute(
                """
                SELECT run_channel.*, channel.platform, channel.channel_id,
                       channel.name AS channel_name, channel.source_url
                  FROM cover_run_channels AS run_channel
                  JOIN cover_channels AS channel ON channel.channel_pk = run_channel.channel_pk
                 WHERE run_channel.run_id = ? AND channel.workspace_key = ?
                 ORDER BY run_channel.run_channel_id
                """,
                (run_id, str(run["workspace_key"])),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_run_channel(
        self,
        run_id: str,
        worker_lease_token: str,
        run_channel_id: int,
        *,
        completeness: str,
        discovered_count: int,
        checkpoint: dict[str, Any],
        error_message: str = "",
    ) -> bool:
        if completeness not in {"complete", "partial", "failed"}:
            raise ValueError("unsupported channel completeness")
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_run_channels
                   SET scan_status = ?, completeness = ?, discovered_count = ?,
                       checkpoint_json = ?, error_message = ?
                 WHERE run_channel_id = ? AND run_id = ?
                   AND EXISTS (
                       SELECT 1 FROM cover_runs
                        WHERE run_id = ? AND worker_lease_token = ?
                          AND status IN ('running', 'pause_requested', 'cancel_requested')
                   )
                """,
                (
                    "failed" if completeness == "failed" else "completed",
                    completeness,
                    max(int(discovered_count), 0),
                    json.dumps(dict(checkpoint or {}), ensure_ascii=False, sort_keys=True),
                    str(error_message or "")[:1000] or None,
                    int(run_channel_id),
                    _required_text(run_id, "run_id"),
                    run_id,
                    _required_text(worker_lease_token, "worker_lease_token"),
                ),
            ).rowcount
            if updated == 1:
                conn.execute(
                    """
                    UPDATE cover_channels SET last_scan_at = ?, updated_at = ?
                     WHERE channel_pk = (
                         SELECT channel_pk FROM cover_run_channels WHERE run_channel_id = ?
                     )
                    """,
                    (now, now, int(run_channel_id)),
                )
        return updated == 1

    def get_video_by_identity(
        self,
        scope: CoverAccessScope,
        *,
        platform: str,
        video_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM cover_videos
                 WHERE workspace_key = ? AND platform = ? AND video_id = ?
                """,
                (
                    scope.workspace_key.strip(),
                    _required_text(platform, "platform").lower(),
                    _required_text(video_id, "video_id"),
                ),
            ).fetchone()
        return self._row(row)

    def get_video_scan_reason(self, scope: CoverAccessScope, video_pk: int) -> str | None:
        reasons = self.get_video_scan_reasons(scope, [video_pk])
        if int(video_pk) not in reasons:
            raise ValueError("video is not visible in this workspace")
        return reasons[int(video_pk)]

    def get_video_scan_reasons(
        self,
        scope: CoverAccessScope,
        video_pks: list[int],
    ) -> dict[int, str | None]:
        normalized = sorted({int(value) for value in video_pks if int(value) > 0})
        if not normalized:
            return {}
        result: dict[int, str | None] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), 500):
                batch = normalized[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT video.video_pk,
                           (
                               SELECT detection.overall_risk
                                 FROM cover_detections AS detection
                                WHERE detection.workspace_key = video.workspace_key
                                  AND detection.video_pk = video.video_pk
                                ORDER BY detection.created_at DESC, detection.detection_id DESC
                                LIMIT 1
                           ) AS current_risk,
                           (
                               SELECT observation.overall_risk
                                 FROM cover_historical_observations AS observation
                                WHERE observation.workspace_key = video.workspace_key
                                  AND observation.video_pk = video.video_pk
                                ORDER BY observation.imported_at DESC, observation.observation_id DESC
                                LIMIT 1
                           ) AS historical_risk
                      FROM cover_videos AS video
                     WHERE video.workspace_key = ? AND video.video_pk IN ({placeholders})
                    """,
                    (scope.workspace_key.strip(), *batch),
                ).fetchall()
                for row in rows:
                    current_risk = str(row["current_risk"] or "")
                    historical_risk = str(row["historical_risk"] or "")
                    if current_risk:
                        reason = "retry_unknown" if current_risk == "unknown" else None
                    elif historical_risk in {"risk", "review"}:
                        reason = "historical_risk"
                    elif historical_risk == "unknown":
                        reason = "retry_unknown"
                    elif historical_risk == "safe":
                        reason = None
                    else:
                        # Imported videos without a current-model result are new to this detector.
                        reason = "new_video"
                    result[int(row["video_pk"])] = reason
        return result

    def claim_next_task_item(
        self,
        run_id: str,
        worker_lease_token: str,
    ) -> dict[str, Any] | None:
        run_id = _required_text(run_id, "run_id")
        worker_lease_token = _required_text(worker_lease_token, "worker_lease_token")
        item_lease_token = uuid4().hex
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                """
                SELECT 1 FROM cover_runs
                 WHERE run_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (run_id, worker_lease_token),
            ).fetchone()
            if run is None:
                conn.rollback()
                return None
            row = conn.execute(
                """
                SELECT task_item_id FROM cover_task_items
                 WHERE run_id = ?
                   AND (status = 'queued' OR (status = 'retry_wait' AND next_retry_at <= ?))
                 ORDER BY task_item_id
                 LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            task_item_id = int(row["task_item_id"])
            updated = conn.execute(
                """
                UPDATE cover_task_items
                   SET status = 'running', worker_lease_token = ?, attempts = attempts + 1,
                       last_heartbeat_at = ?, started_at = COALESCE(started_at, ?),
                       next_retry_at = NULL, error_type = NULL, error_message = NULL,
                       updated_at = ?
                 WHERE task_item_id = ?
                   AND (status = 'queued' OR (status = 'retry_wait' AND next_retry_at <= ?))
                """,
                (item_lease_token, now, now, now, task_item_id, now),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            claimed = conn.execute(
                "SELECT * FROM cover_task_items WHERE task_item_id = ?",
                (task_item_id,),
            ).fetchone()
            conn.commit()
        return self._row(claimed)

    def heartbeat_task_item(self, task_item_id: int, worker_lease_token: str) -> bool:
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_task_items
                   SET last_heartbeat_at = ?, updated_at = ?
                 WHERE task_item_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (now, now, int(task_item_id), _required_text(worker_lease_token, "worker_lease_token")),
            ).rowcount
        return updated == 1

    def get_task_item(self, task_item_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cover_task_items WHERE task_item_id = ?",
                (int(task_item_id),),
            ).fetchone()
        return self._row(row)

    def get_task_item_context(self, task_item_id: int, worker_lease_token: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT item.*, run.workspace_key, run.intensity, run.prompt_version,
                       video.video_id, video.title, video.thumbnail_url, video.video_url
                  FROM cover_task_items AS item
                  JOIN cover_runs AS run ON run.run_id = item.run_id
                  JOIN cover_videos AS video ON video.video_pk = item.video_pk
                 WHERE item.task_item_id = ? AND item.worker_lease_token = ?
                   AND item.status = 'running'
                """,
                (int(task_item_id), _required_text(worker_lease_token, "worker_lease_token")),
            ).fetchone()
        return self._row(row)

    def get_run_item_state(self, run_id: str, worker_lease_token: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run = conn.execute(
                """
                SELECT 1 FROM cover_runs
                 WHERE run_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (_required_text(run_id, "run_id"), _required_text(worker_lease_token, "worker_lease_token")),
            ).fetchone()
            if run is None:
                return None
            row = conn.execute(
                """
                SELECT COUNT(*) AS unfinished_count,
                       MIN(CASE WHEN status = 'retry_wait' THEN next_retry_at END) AS next_retry_at
                  FROM cover_task_items
                 WHERE run_id = ? AND status IN ('queued', 'running', 'retry_wait')
                """,
                (run_id,),
            ).fetchone()
        return self._row(row)

    def save_asset(
        self,
        scope: CoverAccessScope,
        *,
        video_pk: int,
        asset: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        asset_id = str(uuid4())
        content_sha256 = _required_text(str(asset.get("content_sha256") or ""), "content_sha256")
        storage_backend = str(asset.get("storage_backend") or "local").strip().lower()
        if storage_backend not in {"local", "oss"}:
            raise ValueError("unsupported storage_backend")
        with self._connect() as conn:
            video = conn.execute(
                "SELECT 1 FROM cover_videos WHERE video_pk = ? AND workspace_key = ?",
                (int(video_pk), scope.workspace_key.strip()),
            ).fetchone()
            if video is None:
                raise ValueError("video is not visible in this workspace")
            conn.execute(
                """
                INSERT OR IGNORE INTO cover_assets (
                    asset_id, workspace_key, video_pk, content_sha256, storage_key,
                    storage_backend, original_url, fetched_url, mime_type, byte_size,
                    width, height, fetched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    scope.workspace_key.strip(),
                    int(video_pk),
                    content_sha256,
                    _required_text(str(asset.get("storage_key") or ""), "storage_key"),
                    storage_backend,
                    _required_text(str(asset.get("original_url") or ""), "original_url"),
                    _required_text(str(asset.get("fetched_url") or ""), "fetched_url"),
                    _required_text(str(asset.get("mime_type") or ""), "mime_type"),
                    int(asset.get("byte_size") or 0),
                    int(asset.get("width") or 0),
                    int(asset.get("height") or 0),
                    now,
                    now,
                ),
            )
            if storage_backend == "oss":
                conn.execute(
                    """
                    UPDATE cover_assets
                       SET storage_backend = 'oss'
                     WHERE workspace_key = ? AND video_pk = ? AND content_sha256 = ?
                       AND storage_backend = 'local'
                    """,
                    (scope.workspace_key.strip(), int(video_pk), content_sha256),
                )
            row = conn.execute(
                """
                SELECT * FROM cover_assets
                 WHERE workspace_key = ? AND video_pk = ? AND content_sha256 = ?
                """,
                (scope.workspace_key.strip(), int(video_pk), content_sha256),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("asset save did not return a row")
        return result

    def get_latest_asset(self, scope: CoverAccessScope, video_pk: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM cover_assets
                 WHERE workspace_key = ? AND video_pk = ?
                 ORDER BY fetched_at DESC, asset_id DESC LIMIT 1
                """,
                (scope.workspace_key.strip(), int(video_pk)),
            ).fetchone()
        return self._row(row)

    def list_asset_lifecycle_records(
        self,
        scope: CoverAccessScope,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        safe_limit = min(max(int(limit), 1), 5000)
        safe_offset = max(int(offset), 0)
        with self._connect() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_assets WHERE workspace_key = ?",
                    (workspace_key,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT asset.asset_id, asset.video_pk, asset.content_sha256,
                       asset.storage_backend, asset.storage_key, asset.byte_size,
                       asset.fetched_at,
                       EXISTS (
                           SELECT 1
                             FROM cover_case_events AS event
                             JOIN cover_detections AS detection
                               ON detection.detection_id = event.detection_id
                            WHERE detection.asset_id = asset.asset_id
                       ) AS is_case_evidence,
                       EXISTS (
                           SELECT 1 FROM cover_detections AS detection
                            WHERE detection.asset_id = asset.asset_id
                              AND detection.overall_risk IN ('review', 'risk', 'unknown')
                       ) AS has_sensitive_detection,
                       EXISTS (
                           SELECT 1 FROM cover_detections AS detection
                            WHERE detection.asset_id = asset.asset_id
                              AND detection.overall_risk = 'safe'
                       ) AS has_safe_detection,
                       asset.asset_id = (
                           SELECT candidate.asset_id FROM cover_assets AS candidate
                            WHERE candidate.workspace_key = asset.workspace_key
                              AND candidate.video_pk = asset.video_pk
                            ORDER BY candidate.fetched_at DESC, candidate.asset_id DESC
                            LIMIT 1
                       ) AS is_latest_for_video
                  FROM cover_assets AS asset
                 WHERE asset.workspace_key = ?
                 ORDER BY asset.fetched_at, asset.asset_id
                 LIMIT ? OFFSET ?
                """,
                (workspace_key, safe_limit, safe_offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_asset_lifecycle_record(
        self,
        scope: CoverAccessScope,
        asset_id: str,
    ) -> dict[str, Any] | None:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_asset_id = _required_text(asset_id, "asset_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT asset.asset_id, asset.video_pk, asset.content_sha256,
                       asset.storage_backend, asset.storage_key, asset.byte_size,
                       asset.fetched_at,
                       EXISTS (
                           SELECT 1
                             FROM cover_case_events AS event
                             JOIN cover_detections AS detection
                               ON detection.detection_id = event.detection_id
                            WHERE detection.asset_id = asset.asset_id
                       ) AS is_case_evidence,
                       EXISTS (
                           SELECT 1 FROM cover_detections AS detection
                            WHERE detection.asset_id = asset.asset_id
                              AND detection.overall_risk IN ('review', 'risk', 'unknown')
                       ) AS has_sensitive_detection,
                       EXISTS (
                           SELECT 1 FROM cover_detections AS detection
                            WHERE detection.asset_id = asset.asset_id
                              AND detection.overall_risk = 'safe'
                       ) AS has_safe_detection,
                       asset.asset_id = (
                           SELECT candidate.asset_id FROM cover_assets AS candidate
                            WHERE candidate.workspace_key = asset.workspace_key
                              AND candidate.video_pk = asset.video_pk
                            ORDER BY candidate.fetched_at DESC, candidate.asset_id DESC
                            LIMIT 1
                       ) AS is_latest_for_video
                  FROM cover_assets AS asset
                 WHERE asset.asset_id = ? AND asset.workspace_key = ?
                """,
                (normalized_asset_id, workspace_key),
            ).fetchone()
        return self._row(row)

    def register_cleanup_plan(
        self,
        scope: CoverAccessScope,
        *,
        cleanup_kind: str,
        manifest_path: str,
        manifest_sha256: str,
        policy: dict[str, Any],
        total_count: int,
        candidate_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_kind = _required_text(cleanup_kind, "cleanup_kind")
        if normalized_kind not in {"asset", "staging"}:
            raise ValueError("unsupported cleanup_kind")
        normalized_manifest_path = _required_text(manifest_path, "manifest_path")
        if len(normalized_manifest_path) > 4096:
            raise ValueError("manifest_path is too long")
        normalized_digest = str(manifest_sha256 or "").strip().lower()
        if re.fullmatch(r"[a-f0-9]{64}", normalized_digest) is None:
            raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(policy, dict):
            raise ValueError("policy must be an object")
        try:
            policy_json = json.dumps(
                policy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("policy must be JSON serializable") from exc

        normalized_total = int(total_count)
        if normalized_total < 0:
            raise ValueError("total_count must not be negative")
        normalized_items: list[tuple[str, str, int]] = []
        seen_keys: set[str] = set()
        for item in candidate_items:
            if not isinstance(item, dict):
                raise ValueError("candidate item must be an object")
            item_key = _required_text(str(item.get("item_key") or ""), "item_key")
            if len(item_key) > 2048:
                raise ValueError("item_key is too long")
            if item_key in seen_keys:
                raise ValueError("duplicate cleanup item_key")
            seen_keys.add(item_key)
            reason = _required_text(str(item.get("reason") or ""), "reason")[:200]
            byte_size = int(item.get("byte_size", 0))
            if byte_size < 0:
                raise ValueError("byte_size must not be negative")
            normalized_items.append((item_key, reason, byte_size))
        if len(normalized_items) > normalized_total:
            raise ValueError("candidate_count must not exceed total_count")

        cleanup_run_id = str(uuid4())
        created_at = _now()
        candidate_bytes = sum(item[2] for item in normalized_items)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM cover_cleanup_runs
                 WHERE workspace_key = ? AND cleanup_kind = ? AND manifest_sha256 = ?
                """,
                (workspace_key, normalized_kind, normalized_digest),
            ).fetchone()
            if existing is not None:
                existing_items = conn.execute(
                    """
                    SELECT item_key, reason, byte_size, execution_status,
                           result_message, executed_at
                      FROM cover_cleanup_items
                     WHERE cleanup_run_id = ?
                     ORDER BY cleanup_item_id
                    """,
                    (str(existing["cleanup_run_id"]),),
                ).fetchall()
                existing_candidates = [
                    (str(item["item_key"]), str(item["reason"]), int(item["byte_size"]))
                    for item in existing_items
                ]
                same_plan = (
                    str(existing["policy_json"]) == policy_json
                    and int(existing["total_count"]) == normalized_total
                    and int(existing["candidate_count"]) == len(normalized_items)
                    and int(existing["candidate_bytes"]) == candidate_bytes
                    and existing_candidates == normalized_items
                )
                if not same_plan:
                    raise ValueError("manifest digest is already registered with different plan data")
                return _public_cleanup_plan(existing, existing_items)
            conn.execute(
                """
                INSERT INTO cover_cleanup_runs (
                    cleanup_run_id, workspace_key, cleanup_kind, manifest_path,
                    manifest_sha256, policy_json, status, total_count,
                    candidate_count, candidate_bytes, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?)
                """,
                (
                    cleanup_run_id,
                    workspace_key,
                    normalized_kind,
                    normalized_manifest_path,
                    normalized_digest,
                    policy_json,
                    normalized_total,
                    len(normalized_items),
                    candidate_bytes,
                    int(scope.user_id),
                    created_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO cover_cleanup_items (
                    cleanup_run_id, item_key, reason, byte_size
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (cleanup_run_id, item_key, reason, byte_size)
                    for item_key, reason, byte_size in normalized_items
                ],
            )
            row = conn.execute(
                "SELECT * FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
                (cleanup_run_id,),
            ).fetchone()
            item_rows = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ?
                 ORDER BY cleanup_item_id
                """,
                (cleanup_run_id,),
            ).fetchall()
        if row is None:
            raise RuntimeError("cleanup plan registration did not return a row")
        return _public_cleanup_plan(row, item_rows)

    def get_cleanup_plan(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
    ) -> dict[str, Any] | None:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if row is None:
                return None
            item_rows = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ?
                 ORDER BY cleanup_item_id
                """,
                (normalized_run_id,),
            ).fetchall()
        return _public_cleanup_plan(row, item_rows)

    def approve_cleanup_plan(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        expected_manifest_sha256: str,
        execution_token_hash: str,
        approved_at: str,
        execution_token_expires_at: str,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        expected_digest = str(expected_manifest_sha256 or "").strip().lower()
        token_hash = str(execution_token_hash or "").strip().lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
            raise ValueError("expected manifest digest is invalid")
        if re.fullmatch(r"[a-f0-9]{64}", token_hash) is None:
            raise ValueError("execution token hash is invalid")
        approved_time = _parse_aware_datetime(approved_at, "approved_at")
        expires_time = _parse_aware_datetime(
            execution_token_expires_at,
            "execution_token_expires_at",
        )
        if expires_time <= approved_time:
            raise ValueError("execution token expiry must be after approval")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if row is None:
                raise ValueError("cleanup plan not found")
            if str(row["status"]) != "planned":
                raise ValueError("cleanup plan is not planned")
            if str(row["manifest_sha256"]) != expected_digest:
                raise ValueError("manifest digest does not match cleanup plan")
            conn.execute(
                """
                UPDATE cover_cleanup_runs
                   SET status = 'approved', approved_by_user_id = ?, approved_at = ?,
                       execution_token_hash = ?, execution_token_expires_at = ?
                 WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'planned'
                """,
                (
                    int(scope.user_id),
                    approved_time.isoformat(timespec="seconds"),
                    token_hash,
                    expires_time.isoformat(timespec="seconds"),
                    normalized_run_id,
                    workspace_key,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            item_rows = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ? ORDER BY cleanup_item_id
                """,
                (normalized_run_id,),
            ).fetchall()
        if updated is None:
            raise RuntimeError("cleanup approval did not return a row")
        return _public_cleanup_plan(updated, item_rows)

    def claim_cleanup_execution(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        execution_token: str,
        worker_name: str = "cover-cleanup-worker",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        raw_token = _required_text(execution_token, "execution_token")
        if len(raw_token) > 512:
            raise ValueError("execution token is invalid")
        normalized_worker_name = _required_text(worker_name, "worker_name")[:200]
        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            raise ValueError("now must include timezone")
        effective_now = effective_now.astimezone(timezone.utc)
        expired = False
        execution_lease_token = secrets.token_urlsafe(32)
        worker_lease_hash = sha256(execution_lease_token.encode("utf-8")).hexdigest()
        updated = None
        item_rows: list[Any] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if row is None:
                raise ValueError("cleanup plan not found")
            if str(row["status"]) != "approved":
                raise ValueError("cleanup plan is not approved")
            stored_hash = str(row["execution_token_hash"] or "")
            expires_text = str(row["execution_token_expires_at"] or "")
            if not stored_hash or not expires_text:
                raise ValueError("cleanup approval token is missing")
            expires_at = _parse_aware_datetime(expires_text, "execution_token_expires_at")
            if effective_now >= expires_at:
                conn.execute(
                    """
                    UPDATE cover_cleanup_runs
                       SET status = 'cancelled', execution_token_hash = NULL,
                           execution_token_expires_at = NULL, finished_at = ?
                     WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'approved'
                    """,
                    (
                        effective_now.isoformat(timespec="seconds"),
                        normalized_run_id,
                        workspace_key,
                    ),
                )
                conn.commit()
                expired = True
            else:
                supplied_hash = sha256(raw_token.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(stored_hash, supplied_hash):
                    raise ValueError("execution token is invalid")
                changed = conn.execute(
                    """
                    UPDATE cover_cleanup_runs
                       SET status = 'running', started_at = ?,
                           execution_token_hash = NULL,
                           execution_token_expires_at = NULL,
                           worker_lease_hash = ?, worker_name = ?, last_heartbeat_at = ?
                     WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'approved'
                    """,
                    (
                        effective_now.isoformat(timespec="seconds"),
                        worker_lease_hash,
                        normalized_worker_name,
                        effective_now.isoformat(timespec="seconds"),
                        normalized_run_id,
                        workspace_key,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("cleanup plan is not approved")
                updated = conn.execute(
                    "SELECT * FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
                    (normalized_run_id,),
                ).fetchone()
                item_rows = conn.execute(
                    """
                    SELECT item_key, reason, byte_size, execution_status,
                           result_message, executed_at
                      FROM cover_cleanup_items
                     WHERE cleanup_run_id = ? ORDER BY cleanup_item_id
                    """,
                    (normalized_run_id,),
                ).fetchall()
        if expired:
            raise ValueError("execution token has expired")
        if updated is None:
            raise RuntimeError("cleanup execution claim did not return a row")
        result = _public_cleanup_plan(updated, item_rows)
        result["execution_lease_token"] = execution_lease_token
        return result

    def heartbeat_cleanup_execution(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        worker_lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        lease_hash = _cleanup_lease_hash(worker_lease_token)
        effective_time = now or datetime.now(timezone.utc)
        if effective_time.tzinfo is None:
            raise ValueError("now must include timezone")
        heartbeat_at = effective_time.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT worker_lease_hash FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'running'
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                str(row["worker_lease_hash"] or ""), lease_hash
            ):
                return False
            changed = conn.execute(
                """
                UPDATE cover_cleanup_runs SET last_heartbeat_at = ?
                 WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'running'
                """,
                (heartbeat_at, normalized_run_id, workspace_key),
            ).rowcount
        return changed == 1

    def record_cleanup_item_result(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        item_key: str,
        worker_lease_token: str,
        execution_status: str,
        result_message: str,
        executed_at: datetime | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        normalized_item_key = _required_text(item_key, "item_key")
        lease_hash = _cleanup_lease_hash(worker_lease_token)
        normalized_status = _required_text(execution_status, "execution_status")
        if normalized_status not in {"deleted", "skipped", "failed"}:
            raise ValueError("unsupported cleanup item result")
        message = str(result_message or "")[:1000] or None
        effective_time = executed_at or datetime.now(timezone.utc)
        if effective_time.tzinfo is None:
            raise ValueError("executed_at must include timezone")
        executed_text = effective_time.astimezone(timezone.utc).isoformat(timespec="seconds")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                """
                SELECT status, worker_lease_hash FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if run is None:
                raise ValueError("cleanup plan not found")
            if str(run["status"]) != "running":
                raise ValueError("cleanup plan is not running")
            if not hmac.compare_digest(str(run["worker_lease_hash"] or ""), lease_hash):
                raise ValueError("cleanup worker lease is invalid")
            item = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ? AND item_key = ?
                """,
                (normalized_run_id, normalized_item_key),
            ).fetchone()
            if item is None:
                raise ValueError("cleanup item not found")
            current_status = str(item["execution_status"])
            if current_status != "pending":
                if current_status == normalized_status and item["result_message"] == message:
                    return dict(item)
                raise ValueError("cleanup item is already finalized")
            changed = conn.execute(
                """
                UPDATE cover_cleanup_items
                   SET execution_status = ?, result_message = ?, executed_at = ?
                 WHERE cleanup_run_id = ? AND item_key = ? AND execution_status = 'pending'
                """,
                (
                    normalized_status,
                    message,
                    executed_text,
                    normalized_run_id,
                    normalized_item_key,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("cleanup item is already finalized")
            updated = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ? AND item_key = ?
                """,
                (normalized_run_id, normalized_item_key),
            ).fetchone()
        if updated is None:
            raise RuntimeError("cleanup item result did not return a row")
        return dict(updated)

    def finish_cleanup_execution(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        worker_lease_token: str,
        finished_at: datetime | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        lease_hash = _cleanup_lease_hash(worker_lease_token)
        effective_time = finished_at or datetime.now(timezone.utc)
        if effective_time.tzinfo is None:
            raise ValueError("finished_at must include timezone")
        finished_text = effective_time.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                """
                SELECT status, worker_lease_hash FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if run is None:
                raise ValueError("cleanup plan not found")
            if str(run["status"]) != "running":
                raise ValueError("cleanup plan is not running")
            if not hmac.compare_digest(str(run["worker_lease_hash"] or ""), lease_hash):
                raise ValueError("cleanup worker lease is invalid")
            counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN execution_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN execution_status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                  FROM cover_cleanup_items WHERE cleanup_run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            if int(counts["pending_count"] or 0) > 0:
                raise ValueError("cleanup execution still has pending items")
            status = "partial_failed" if int(counts["failed_count"] or 0) > 0 else "completed"
            conn.execute(
                """
                UPDATE cover_cleanup_runs
                   SET status = ?, finished_at = ?, worker_lease_hash = NULL,
                       worker_name = NULL, last_heartbeat_at = NULL
                 WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'running'
                """,
                (status, finished_text, normalized_run_id, workspace_key),
            )
            updated = conn.execute(
                "SELECT * FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            item_rows = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ? ORDER BY cleanup_item_id
                """,
                (normalized_run_id,),
            ).fetchall()
        if updated is None:
            raise RuntimeError("cleanup execution finish did not return a row")
        return _public_cleanup_plan(updated, item_rows)

    def recover_stale_cleanup_execution(
        self,
        scope: CoverAccessScope,
        cleanup_run_id: str,
        *,
        stale_before: datetime,
        recovered_at: datetime | None = None,
    ) -> dict[str, Any]:
        workspace_key = _required_text(scope.workspace_key, "workspace_key")
        normalized_run_id = _required_text(cleanup_run_id, "cleanup_run_id")
        if stale_before.tzinfo is None:
            raise ValueError("stale_before must include timezone")
        effective_recovered_at = recovered_at or datetime.now(timezone.utc)
        if effective_recovered_at.tzinfo is None:
            raise ValueError("recovered_at must include timezone")
        stale_time = stale_before.astimezone(timezone.utc)
        recovered_text = effective_recovered_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                """
                SELECT status, last_heartbeat_at FROM cover_cleanup_runs
                 WHERE cleanup_run_id = ? AND workspace_key = ?
                """,
                (normalized_run_id, workspace_key),
            ).fetchone()
            if run is None:
                raise ValueError("cleanup plan not found")
            if str(run["status"]) != "running":
                raise ValueError("cleanup plan is not running")
            heartbeat = str(run["last_heartbeat_at"] or "")
            if not heartbeat or _parse_aware_datetime(
                heartbeat, "last_heartbeat_at"
            ) >= stale_time:
                raise ValueError("cleanup execution is not stale")
            conn.execute(
                """
                UPDATE cover_cleanup_items
                   SET execution_status = 'failed', result_message = 'execution_interrupted',
                       executed_at = ?
                 WHERE cleanup_run_id = ? AND execution_status = 'pending'
                """,
                (recovered_text, normalized_run_id),
            )
            conn.execute(
                """
                UPDATE cover_cleanup_runs
                   SET status = 'partial_failed', finished_at = ?,
                       worker_lease_hash = NULL, worker_name = NULL,
                       last_heartbeat_at = NULL
                 WHERE cleanup_run_id = ? AND workspace_key = ? AND status = 'running'
                """,
                (recovered_text, normalized_run_id, workspace_key),
            )
            updated = conn.execute(
                "SELECT * FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            item_rows = conn.execute(
                """
                SELECT item_key, reason, byte_size, execution_status,
                       result_message, executed_at
                  FROM cover_cleanup_items
                 WHERE cleanup_run_id = ? ORDER BY cleanup_item_id
                """,
                (normalized_run_id,),
            ).fetchall()
        if updated is None:
            raise RuntimeError("cleanup recovery did not return a row")
        return _public_cleanup_plan(updated, item_rows)

    def start_task_attempt(
        self,
        task_item_id: int,
        worker_lease_token: str,
        *,
        stage: str,
        provider: str = "",
    ) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as conn:
            item = conn.execute(
                """
                SELECT attempts FROM cover_task_items
                 WHERE task_item_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (int(task_item_id), _required_text(worker_lease_token, "worker_lease_token")),
            ).fetchone()
            if item is None:
                return None
            attempt_no = int(item["attempts"])
            conn.execute(
                """
                INSERT OR IGNORE INTO cover_attempts (
                    task_item_id, attempt_no, stage, status, provider, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (
                    int(task_item_id),
                    attempt_no,
                    _required_text(stage, "stage"),
                    str(provider or "")[:200] or None,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM cover_attempts
                 WHERE task_item_id = ? AND attempt_no = ? AND stage = ?
                """,
                (int(task_item_id), attempt_no, stage),
            ).fetchone()
        return self._row(row)

    def finish_task_attempt(
        self,
        attempt_id: int,
        *,
        succeeded: bool,
        duration_seconds: float,
        error_type: str = "",
        error_message: str = "",
        usage: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_attempts
                   SET status = ?, error_type = ?, error_message = ?, usage_json = ?,
                       duration_seconds = ?, finished_at = ?
                 WHERE attempt_id = ? AND status = 'running'
                """,
                (
                    "succeeded" if succeeded else "failed",
                    None if succeeded else str(error_type or "unexpected")[:100],
                    None if succeeded else str(error_message or "")[:1000],
                    json.dumps(dict(usage or {}), ensure_ascii=False, sort_keys=True),
                    max(float(duration_seconds), 0),
                    _now(),
                    int(attempt_id),
                ),
            ).rowcount
        return updated == 1

    def save_detection_and_case(
        self,
        scope: CoverAccessScope,
        *,
        task_item_id: int,
        worker_lease_token: str,
        asset_id: str,
        detection: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        detection_id = str(uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute(
                """
                SELECT item.run_id, item.video_pk
                  FROM cover_task_items AS item
                  JOIN cover_runs AS run ON run.run_id = item.run_id
                 WHERE item.task_item_id = ? AND item.worker_lease_token = ?
                   AND item.status = 'running' AND run.workspace_key = ?
                """,
                (
                    int(task_item_id),
                    _required_text(worker_lease_token, "worker_lease_token"),
                    scope.workspace_key.strip(),
                ),
            ).fetchone()
            if item is None:
                conn.rollback()
                raise ValueError("task item lease is no longer valid")
            overall_risk = _required_text(str(detection.get("overall_risk") or ""), "overall_risk")
            conn.execute(
                """
                INSERT OR IGNORE INTO cover_detections (
                    detection_id, workspace_key, run_id, task_item_id, video_pk, asset_id,
                    overall_risk, risk_tags_json, summary, evidence, confidence,
                    provider, model, prompt_version, prompt_hash, intensity,
                    input_snapshot_json, raw_response, started_at, finished_at,
                    duration_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    detection_id,
                    scope.workspace_key.strip(),
                    str(item["run_id"]),
                    int(task_item_id),
                    int(item["video_pk"]),
                    _required_text(asset_id, "asset_id"),
                    overall_risk,
                    json.dumps(list(detection.get("risk_tags") or []), ensure_ascii=False),
                    _required_text(str(detection.get("summary") or ""), "summary"),
                    _required_text(str(detection.get("evidence") or ""), "evidence"),
                    float(detection.get("confidence") or 0),
                    _required_text(str(detection.get("provider") or ""), "provider"),
                    _required_text(str(detection.get("model") or ""), "model"),
                    _required_text(str(detection.get("prompt_version") or ""), "prompt_version"),
                    _required_text(str(detection.get("prompt_hash") or ""), "prompt_hash"),
                    _required_text(str(detection.get("intensity") or ""), "intensity"),
                    json.dumps(dict(detection.get("input_snapshot") or {}), ensure_ascii=False, sort_keys=True),
                    str(detection.get("raw_response") or "")[:20_000],
                    str(detection.get("started_at") or now),
                    str(detection.get("finished_at") or now),
                    max(float(detection.get("duration_seconds") or 0), 0),
                    now,
                ),
            )
            saved_detection = conn.execute(
                "SELECT detection_id, overall_risk FROM cover_detections WHERE task_item_id = ?",
                (int(task_item_id),),
            ).fetchone()
            if saved_detection is None:
                conn.rollback()
                raise RuntimeError("detection save did not return a row")
            detection_id = str(saved_detection["detection_id"])
            overall_risk = str(saved_detection["overall_risk"])
            active_case = conn.execute(
                """
                SELECT risk_case.case_id, opened_detection.asset_id AS opened_asset_id
                  FROM cover_risk_cases AS risk_case
                  JOIN cover_detections AS opened_detection
                    ON opened_detection.detection_id = risk_case.opened_detection_id
                 WHERE risk_case.workspace_key = ? AND risk_case.video_pk = ?
                   AND risk_case.current_status IN ('open', 'needs_review')
                 ORDER BY risk_case.opened_at, risk_case.case_id LIMIT 1
                """,
                (scope.workspace_key.strip(), int(item["video_pk"])),
            ).fetchone()
            case_id = str(active_case["case_id"]) if active_case is not None else ""
            opened_asset_id = str(active_case["opened_asset_id"]) if active_case is not None else ""
            if overall_risk in {"risk", "review", "unknown"} and not case_id:
                case_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO cover_risk_cases (
                        case_id, workspace_key, video_pk, opened_detection_id,
                        current_status, opened_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'needs_review', ?, ?)
                    """,
                    (
                        case_id,
                        scope.workspace_key.strip(),
                        int(item["video_pk"]),
                        detection_id,
                        now,
                        now,
                    ),
                )
            event_type = {
                "risk": "risk_detected",
                "review": "review_detected",
                "unknown": "unknown_detected",
            }.get(overall_risk)
            if overall_risk == "safe" and case_id:
                event_type = (
                    "safe_redetection"
                    if opened_asset_id == _required_text(asset_id, "asset_id")
                    else "rectification_candidate"
                )
            if case_id and event_type:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cover_case_events (
                        case_id, detection_id, event_type, actor_type,
                        actor_user_id, reason, created_at
                    ) VALUES (?, ?, ?, 'system', NULL, ?, ?)
                    """,
                    (
                        case_id,
                        detection_id,
                        event_type,
                        str(detection.get("summary") or "模型检测结果")[:1000],
                        now,
                    ),
                )
            conn.commit()
        return {
            "detection_id": detection_id,
            "overall_risk": overall_risk,
            "case_id": case_id or None,
            "case_event_type": event_type,
        }

    def finish_run_without_items(self, run_id: str, worker_lease_token: str) -> bool:
        now = _now()
        with self._connect() as conn:
            run_id = _required_text(run_id, "run_id")
            channel_counts = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN completeness = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN completeness = 'partial' THEN 1 ELSE 0 END) AS partial
                  FROM cover_run_channels WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            total_channels = int(channel_counts["total"] or 0)
            failed_channels = int(channel_counts["failed"] or 0)
            partial_channels = int(channel_counts["partial"] or 0)
            if total_channels > 0 and failed_channels == total_channels:
                status = "failed"
                status_message = "频道扫描全部失败"
            elif failed_channels > 0 or partial_channels > 0:
                status = "partial_failed"
                status_message = "频道扫描不完整，未发现需要检测的新视频"
            else:
                status = "completed"
                status_message = "未发现需要检测的新视频"
            updated = conn.execute(
                """
                UPDATE cover_runs
                   SET status = ?, status_message = ?, worker_name = NULL,
                       worker_lease_token = NULL, last_heartbeat_at = NULL,
                       finished_at = ?, updated_at = ?
                 WHERE run_id = ? AND worker_lease_token = ? AND status = 'running'
                   AND NOT EXISTS (SELECT 1 FROM cover_task_items WHERE run_id = ?)
                """,
                (
                    status,
                    status_message,
                    now,
                    now,
                    run_id,
                    _required_text(worker_lease_token, "worker_lease_token"),
                    run_id,
                ),
            ).rowcount
        return updated == 1

    def retry_task_item(
        self,
        task_item_id: int,
        worker_lease_token: str,
        *,
        next_retry_at: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_task_items
                   SET status = 'retry_wait', next_retry_at = ?,
                       worker_lease_token = NULL, last_heartbeat_at = NULL,
                       error_type = ?, error_message = ?, updated_at = ?
                 WHERE task_item_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (
                    _required_text(next_retry_at, "next_retry_at"),
                    _required_text(error_type, "error_type")[:100],
                    str(error_message or "")[:1000],
                    now,
                    int(task_item_id),
                    _required_text(worker_lease_token, "worker_lease_token"),
                ),
            ).rowcount
        return updated == 1

    def advance_task_item(
        self,
        task_item_id: int,
        worker_lease_token: str,
        *,
        expected_stage: str,
        next_stage: str,
    ) -> bool:
        expected_stage = _required_text(expected_stage, "expected_stage")
        next_stage = _required_text(next_stage, "next_stage")
        if (expected_stage, next_stage) not in {("download", "review"), ("review", "persist")}:
            raise ValueError("unsupported task item stage transition")
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE cover_task_items
                   SET stage = ?, last_heartbeat_at = ?, updated_at = ?
                 WHERE task_item_id = ? AND worker_lease_token = ?
                   AND status = 'running' AND stage = ?
                """,
                (
                    next_stage,
                    now,
                    now,
                    int(task_item_id),
                    _required_text(worker_lease_token, "worker_lease_token"),
                    expected_stage,
                ),
            ).rowcount
        return updated == 1

    def finish_task_item(
        self,
        task_item_id: int,
        worker_lease_token: str,
        *,
        succeeded: bool,
        error_type: str = "",
        error_message: str = "",
    ) -> bool:
        now = _now()
        target_status = "succeeded" if succeeded else "failed"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT run_id FROM cover_task_items
                 WHERE task_item_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (int(task_item_id), _required_text(worker_lease_token, "worker_lease_token")),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            run_id = str(row["run_id"])
            conn.execute(
                """
                UPDATE cover_task_items
                   SET status = ?, worker_lease_token = NULL, last_heartbeat_at = NULL,
                       error_type = ?, error_message = ?, finished_at = ?, updated_at = ?
                 WHERE task_item_id = ? AND worker_lease_token = ? AND status = 'running'
                """,
                (
                    target_status,
                    None if succeeded else str(error_type or "unexpected")[:100],
                    None if succeeded else str(error_message or "task item failed")[:1000],
                    now,
                    now,
                    int(task_item_id),
                    worker_lease_token,
                ),
            )
            counts = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                  FROM cover_task_items WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            completed = int(counts["completed"] or 0)
            failed = int(counts["failed"] or 0)
            final_status: str | None = None
            final_message: str | None = None
            if total > 0 and completed + failed == total:
                incomplete_channels = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM cover_run_channels
                         WHERE run_id = ? AND completeness IN ('partial', 'failed')
                        """,
                        (run_id,),
                    ).fetchone()[0]
                )
                if completed == 0:
                    final_status = "failed"
                    final_message = "检测失败"
                elif failed > 0 or incomplete_channels > 0:
                    final_status = "partial_failed"
                    if failed > 0 and incomplete_channels > 0:
                        final_message = "部分频道扫描不完整，且部分条目检测失败"
                    elif incomplete_channels > 0:
                        final_message = "部分频道扫描不完整"
                    else:
                        final_message = "部分条目检测失败"
                else:
                    final_status = "completed"
                    final_message = "检测完成"
            conn.execute(
                """
                UPDATE cover_runs
                   SET total_item_count = ?, completed_item_count = ?, failed_item_count = ?,
                       status = CASE WHEN status = 'running' THEN COALESCE(?, status) ELSE status END,
                       status_message = CASE WHEN status = 'running' AND ? IS NOT NULL
                                             THEN ? ELSE status_message END,
                       worker_name = CASE
                           WHEN status = 'running' AND ? IS NOT NULL THEN NULL ELSE worker_name END,
                       worker_lease_token = CASE
                           WHEN status = 'running' AND ? IS NOT NULL THEN NULL ELSE worker_lease_token END,
                       last_heartbeat_at = CASE
                           WHEN status = 'running' AND ? IS NOT NULL THEN NULL ELSE last_heartbeat_at END,
                       finished_at = CASE
                           WHEN status = 'running' AND ? IS NOT NULL THEN ? ELSE finished_at END,
                       updated_at = ?
                 WHERE run_id = ?
                """,
                (
                    total,
                    completed,
                    failed,
                    final_status,
                    final_status,
                    final_message,
                    final_status,
                    final_status,
                    final_status,
                    final_status,
                    now,
                    now,
                    run_id,
                ),
            )
            conn.commit()
        return True

    def request_pause(self, scope: CoverAccessScope, run_id: str) -> dict[str, Any]:
        return self._request_control(scope, run_id, action="pause")

    def request_cancel(self, scope: CoverAccessScope, run_id: str) -> dict[str, Any]:
        return self._request_control(scope, run_id, action="cancel")

    def _request_control(self, scope: CoverAccessScope, run_id: str, *, action: str) -> dict[str, Any]:
        run_id = _required_text(run_id, "run_id")
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM cover_runs WHERE run_id = ? AND workspace_key = ?",
                (run_id, scope.workspace_key.strip()),
            ).fetchone()
            if row is None:
                raise LookupError("cover run not found")
            status = str(row["status"])
            if action == "pause":
                if status in {"paused", "pause_requested"}:
                    pass
                elif status == "queued":
                    conn.execute(
                        """
                        UPDATE cover_runs SET status = 'paused', status_message = '已暂停',
                               worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                               updated_at = ? WHERE run_id = ?
                        """,
                        (now, run_id),
                    )
                elif status == "running":
                    conn.execute(
                        "UPDATE cover_runs SET status = 'pause_requested', status_message = '正在暂停', updated_at = ? WHERE run_id = ?",
                        (now, run_id),
                    )
                else:
                    raise ValueError(f"run cannot be paused from status {status}")
            elif action == "cancel":
                if status in {"cancelled", "cancel_requested"}:
                    pass
                elif status in {"queued", "paused"}:
                    conn.execute(
                        """
                        UPDATE cover_runs SET status = 'cancelled', status_message = '已取消',
                               worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                               finished_at = ?, updated_at = ? WHERE run_id = ?
                        """,
                        (now, now, run_id),
                    )
                elif status in {"running", "pause_requested"}:
                    conn.execute(
                        "UPDATE cover_runs SET status = 'cancel_requested', status_message = '正在取消', updated_at = ? WHERE run_id = ?",
                        (now, run_id),
                    )
                else:
                    raise ValueError(f"run cannot be cancelled from status {status}")
            else:
                raise ValueError("unsupported control action")
        result = self.get_run(scope, run_id)
        if result is None:
            raise LookupError("cover run not found")
        return result

    def resume_run(self, scope: CoverAccessScope, run_id: str) -> dict[str, Any]:
        run_id = _required_text(run_id, "run_id")
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM cover_runs WHERE run_id = ? AND workspace_key = ?",
                (run_id, scope.workspace_key.strip()),
            ).fetchone()
            if row is None:
                raise LookupError("cover run not found")
            status = str(row["status"])
            if status == "paused":
                conn.execute(
                    """
                    UPDATE cover_runs SET status = 'queued', status_message = '已恢复，等待执行',
                           worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                           finished_at = NULL, updated_at = ? WHERE run_id = ?
                    """,
                    (now, run_id),
                )
            elif status != "queued":
                raise ValueError(f"run cannot be resumed from status {status}")
        result = self.get_run(scope, run_id)
        if result is None:
            raise LookupError("cover run not found")
        return result

    def settle_requested_control(self, run_id: str, worker_lease_token: str) -> str | None:
        run_id = _required_text(run_id, "run_id")
        worker_lease_token = _required_text(worker_lease_token, "worker_lease_token")
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM cover_runs WHERE run_id = ? AND worker_lease_token = ?",
                (run_id, worker_lease_token),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            target = {"pause_requested": "paused", "cancel_requested": "cancelled"}.get(status)
            if target is None:
                return status
            conn.execute(
                """
                UPDATE cover_runs
                   SET status = ?, status_message = ?, worker_name = NULL,
                       worker_lease_token = NULL, last_heartbeat_at = NULL,
                       finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END,
                       updated_at = ?
                 WHERE run_id = ? AND worker_lease_token = ? AND status = ?
                """,
                (
                    target,
                    "已暂停" if target == "paused" else "已取消",
                    target,
                    now,
                    now,
                    run_id,
                    worker_lease_token,
                    status,
                ),
            )
            if target == "paused":
                conn.execute(
                    """
                    UPDATE cover_task_items
                       SET status = 'queued', worker_lease_token = NULL,
                           last_heartbeat_at = NULL, updated_at = ?
                     WHERE run_id = ? AND status = 'running'
                    """,
                    (now, run_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE cover_task_items
                       SET status = 'cancelled', worker_lease_token = NULL,
                           last_heartbeat_at = NULL, finished_at = ?, updated_at = ?
                     WHERE run_id = ? AND status IN ('queued', 'running', 'retry_wait')
                    """,
                    (now, now, run_id),
                )
        return target

    def release_run_for_worker_shutdown(self, run_id: str, worker_lease_token: str) -> str | None:
        run_id = _required_text(run_id, "run_id")
        worker_lease_token = _required_text(worker_lease_token, "worker_lease_token")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM cover_runs WHERE run_id = ? AND worker_lease_token = ?",
                (run_id, worker_lease_token),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            current_status = str(row["status"])
            target_status = {
                "running": "queued",
                "pause_requested": "paused",
                "cancel_requested": "cancelled",
            }.get(current_status)
            if target_status is None:
                conn.rollback()
                return current_status
            status_message = {
                "queued": "worker 已停止，任务重新排队",
                "paused": "已暂停",
                "cancelled": "已取消",
            }[target_status]
            conn.execute(
                """
                UPDATE cover_runs
                   SET status = ?, status_message = ?, worker_name = NULL,
                       worker_lease_token = NULL, last_heartbeat_at = NULL,
                       finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END,
                       updated_at = ?
                 WHERE run_id = ? AND worker_lease_token = ? AND status = ?
                """,
                (
                    target_status,
                    status_message,
                    target_status,
                    now,
                    now,
                    run_id,
                    worker_lease_token,
                    current_status,
                ),
            )
            if target_status == "cancelled":
                conn.execute(
                    """
                    UPDATE cover_task_items
                       SET status = 'cancelled', worker_lease_token = NULL,
                           last_heartbeat_at = NULL, finished_at = COALESCE(finished_at, ?),
                           updated_at = ?
                     WHERE run_id = ? AND status IN ('queued', 'running', 'retry_wait')
                    """,
                    (now, now, run_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE cover_task_items
                       SET status = 'queued', worker_lease_token = NULL,
                           last_heartbeat_at = NULL, updated_at = ?
                     WHERE run_id = ? AND status = 'running'
                    """,
                    (now, run_id),
                )
            conn.commit()
        return target_status

    def recover_stale_runs(self, *, stale_before: str) -> dict[str, int]:
        stale_before = _required_text(stale_before, "stale_before")
        now = _now()
        counts = {"requeued": 0, "paused": 0, "cancelled": 0}
        with self._connect() as conn:
            counts["requeued"] = conn.execute(
                """
                UPDATE cover_runs SET status = 'queued', status_message = '执行中断，已重新排队',
                       worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                       updated_at = ?
                 WHERE status = 'running' AND last_heartbeat_at < ?
                """,
                (now, stale_before),
            ).rowcount
            counts["paused"] = conn.execute(
                """
                UPDATE cover_runs SET status = 'paused', status_message = '已暂停',
                       worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                       updated_at = ?
                 WHERE status = 'pause_requested' AND last_heartbeat_at < ?
                """,
                (now, stale_before),
            ).rowcount
            counts["cancelled"] = conn.execute(
                """
                UPDATE cover_runs SET status = 'cancelled', status_message = '已取消',
                       worker_name = NULL, worker_lease_token = NULL, last_heartbeat_at = NULL,
                       finished_at = ?, updated_at = ?
                 WHERE status = 'cancel_requested' AND last_heartbeat_at < ?
                """,
                (now, now, stale_before),
            ).rowcount
            conn.execute(
                """
                UPDATE cover_task_items
                   SET status = 'queued', worker_lease_token = NULL,
                       last_heartbeat_at = NULL, updated_at = ?
                 WHERE status = 'running'
                   AND run_id IN (
                       SELECT run_id FROM cover_runs WHERE status IN ('queued', 'paused')
                   )
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE cover_task_items
                   SET status = 'cancelled', worker_lease_token = NULL,
                       last_heartbeat_at = NULL, finished_at = COALESCE(finished_at, ?),
                       updated_at = ?
                 WHERE status IN ('queued', 'running', 'retry_wait')
                   AND run_id IN (
                       SELECT run_id FROM cover_runs WHERE status = 'cancelled'
                   )
                """,
                (now, now),
            )
        return counts
