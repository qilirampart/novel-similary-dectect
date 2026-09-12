from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from service.cover_monitor.importer import WorkbookImportParser

try:
    from pysqlite3 import dbapi2 as sqlite3  # type: ignore
except Exception:
    import sqlite3


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 5
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _required_text(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


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
                    original_url, fetched_url, mime_type, byte_size, width, height,
                    fetched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    scope.workspace_key.strip(),
                    int(video_pk),
                    content_sha256,
                    _required_text(str(asset.get("storage_key") or ""), "storage_key"),
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
            if overall_risk in {"risk", "review", "unknown"}:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cover_risk_cases (
                        case_id, workspace_key, video_pk, opened_detection_id,
                        current_status, opened_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'needs_review', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        scope.workspace_key.strip(),
                        int(item["video_pk"]),
                        detection_id,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return {"detection_id": detection_id, "overall_risk": overall_risk}

    def finish_run_without_items(self, run_id: str, worker_lease_token: str) -> bool:
        now = _now()
        with self._connect() as conn:
            failed_channels = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_run_channels WHERE run_id = ? AND completeness = 'failed'",
                    (_required_text(run_id, "run_id"),),
                ).fetchone()[0]
            )
            total_channels = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cover_run_channels WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            status = "failed" if total_channels > 0 and failed_channels == total_channels else "completed"
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
                    "未发现需要检测的新视频" if status == "completed" else "频道扫描失败",
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
            if total > 0 and completed + failed == total:
                final_status = "completed" if failed == 0 else ("failed" if completed == 0 else "partial_failed")
            conn.execute(
                """
                UPDATE cover_runs
                   SET total_item_count = ?, completed_item_count = ?, failed_item_count = ?,
                       status = CASE WHEN status = 'running' THEN COALESCE(?, status) ELSE status END,
                       status_message = CASE
                           WHEN status = 'running' AND ? = 'completed' THEN '检测完成'
                           WHEN status = 'running' AND ? = 'partial_failed' THEN '部分条目检测失败'
                           WHEN status = 'running' AND ? = 'failed' THEN '检测失败'
                           ELSE status_message END,
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
                    final_status,
                    final_status,
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
