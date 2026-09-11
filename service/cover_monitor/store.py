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
SCHEMA_VERSION = 3
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
                "SELECT * FROM cover_runs WHERE run_id = ? AND workspace_key = ?",
                (_required_text(run_id, "run_id"), scope.workspace_key.strip()),
            ).fetchone()
        return self._row(row)

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
        return target

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
        return counts
