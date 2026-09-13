from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from service.cover_monitor.lifecycle import (
    CoverRetentionPolicy,
    build_asset_cleanup_plan,
    build_staging_cleanup_plan,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _asset(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "asset_id": "asset-1",
        "storage_backend": "oss",
        "storage_key": "video-1/hash.jpg",
        "fetched_at": "2026-06-01T00:00:00+00:00",
        "is_case_evidence": 0,
        "has_sensitive_detection": 0,
        "has_safe_detection": 1,
        "is_latest_for_video": 0,
    }
    record.update(overrides)
    return record


def test_cleanup_plan_never_deletes_case_or_sensitive_evidence() -> None:
    decisions = build_asset_cleanup_plan(
        [
            _asset(asset_id="case", is_case_evidence=1),
            _asset(asset_id="risk", has_sensitive_detection=1),
        ],
        now=NOW,
    )

    assert [(item.asset_id, item.action, item.reason) for item in decisions] == [
        ("case", "keep", "case_evidence"),
        ("risk", "keep", "sensitive_detection"),
    ]


def test_cleanup_plan_keeps_latest_video_asset_even_when_old() -> None:
    decision = build_asset_cleanup_plan(
        [_asset(is_latest_for_video=1)],
        now=NOW,
    )[0]

    assert decision.action == "keep"
    assert decision.reason == "latest_video_asset"


def test_cleanup_plan_marks_only_expired_safe_and_unprocessed_assets() -> None:
    decisions = build_asset_cleanup_plan(
        [
            _asset(asset_id="old-safe"),
            _asset(asset_id="recent-safe", fetched_at="2026-08-20T00:00:00+00:00"),
            _asset(
                asset_id="old-unprocessed",
                fetched_at="2026-08-01T00:00:00+00:00",
                has_safe_detection=0,
            ),
            _asset(
                asset_id="recent-unprocessed",
                fetched_at="2026-09-05T00:00:00+00:00",
                has_safe_detection=0,
            ),
        ],
        now=NOW,
        policy=CoverRetentionPolicy(safe_days=60, unprocessed_days=14),
    )

    assert [(item.asset_id, item.action, item.reason) for item in decisions] == [
        ("old-safe", "delete_candidate", "safe_retention_expired"),
        ("recent-safe", "keep", "safe_within_retention"),
        ("old-unprocessed", "delete_candidate", "unprocessed_retention_expired"),
        ("recent-unprocessed", "keep", "unprocessed_within_retention"),
    ]


def test_cleanup_plan_fails_closed_for_invalid_timestamp_or_unknown_backend() -> None:
    decisions = build_asset_cleanup_plan(
        [
            _asset(asset_id="bad-time", fetched_at="not-a-time"),
            _asset(asset_id="bad-backend", storage_backend="unknown"),
            _asset(asset_id="bad-size", byte_size="not-a-number"),
            _asset(asset_id="bad-flag", is_case_evidence=[]),
        ],
        now=NOW,
    )

    assert [(item.asset_id, item.action, item.reason) for item in decisions] == [
        ("bad-time", "keep", "invalid_metadata"),
        ("bad-backend", "keep", "invalid_metadata"),
        ("bad-size", "keep", "invalid_metadata"),
        ("bad-flag", "keep", "invalid_metadata"),
    ]


def _write_staging_file(root: Path, relative: str, *, modified_at: datetime) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"staging-cover")
    timestamp = modified_at.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def test_staging_plan_marks_only_recognized_files_older_than_24_hours(tmp_path: Path) -> None:
    digest = "a" * 64
    _write_staging_file(
        tmp_path,
        f"video001/{digest}.jpg",
        modified_at=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
    )
    _write_staging_file(
        tmp_path,
        f"video001/{digest}.png",
        modified_at=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc),
    )
    _write_staging_file(
        tmp_path,
        f"video001/{digest}.jpg.part-{'b' * 32}",
        modified_at=datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc),
    )
    _write_staging_file(
        tmp_path,
        "unexpected/manual-note.txt",
        modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    decisions = build_staging_cleanup_plan(tmp_path, now=NOW, stale_hours=24)

    assert [(item.relative_path, item.action, item.reason) for item in decisions] == [
        ("unexpected/manual-note.txt", "keep", "unrecognized_staging_file"),
        (f"video001/{digest}.jpg", "delete_candidate", "staging_retention_expired"),
        (
            f"video001/{digest}.jpg.part-{'b' * 32}",
            "delete_candidate",
            "staging_retention_expired",
        ),
        (f"video001/{digest}.png", "keep", "staging_within_retention"),
    ]


def test_staging_plan_rejects_invalid_retention_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stale_hours"):
        build_staging_cleanup_plan(tmp_path, now=NOW, stale_hours=0)


def test_staging_cli_can_bind_generated_manifest_to_audit_record(tmp_path: Path) -> None:
    digest = "d" * 64
    _write_staging_file(
        tmp_path / "staging",
        f"video001/{digest}.jpg",
        modified_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    output = tmp_path / "manifest.csv"
    db_path = tmp_path / "cover.sqlite3"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/plan_cover_staging_cleanup_v1.py",
            "--staging-root",
            str(tmp_path / "staging"),
            "--output",
            str(output),
            "--db",
            str(db_path),
            "--register-audit",
            "--workspace",
            "audit-test",
            "--user-id",
            "9",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["cleanup_run_id"]
    assert result["delete_candidate_count"] == 1
    with sqlite3.connect(db_path) as conn:
        audit = conn.execute(
            """
            SELECT workspace_key, cleanup_kind, status, candidate_count
              FROM cover_cleanup_runs
            """
        ).fetchone()
    assert audit == ("audit-test", "staging", "planned", 1)
