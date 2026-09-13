from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path

from service.cover_monitor.cleanup_preflight import preflight_cleanup_plan
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _registered_staging_plan(tmp_path: Path) -> tuple[CoverMonitorStore, CoverAccessScope, str, Path, Path]:
    root = tmp_path / "staging"
    digest = sha256(b"cover-evidence").hexdigest()
    relative_path = f"video001/{digest}.jpg"
    target = root / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cover-evidence")
    old_timestamp = datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()
    os.utime(target, (old_timestamp, old_timestamp))

    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["relative_path", "action", "reason", "modified_at", "byte_size"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "relative_path": relative_path,
                "action": "delete_candidate",
                "reason": "staging_retention_expired",
                "modified_at": "2026-09-10T00:00:00+00:00",
                "byte_size": len(b"cover-evidence"),
            }
        )

    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    cleanup = store.register_cleanup_plan(
        scope,
        cleanup_kind="staging",
        manifest_path=str(manifest),
        manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
        policy={"staging_root": str(root.resolve()), "stale_hours": 24},
        total_count=1,
        candidate_items=[
            {
                "item_key": relative_path,
                "reason": "staging_retention_expired",
                "byte_size": len(b"cover-evidence"),
            }
        ],
    )
    return store, scope, cleanup["cleanup_run_id"], manifest, target


def test_staging_preflight_accepts_unchanged_expired_content(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, _, target = _registered_staging_plan(tmp_path)

    result = preflight_cleanup_plan(
        store,
        scope,
        cleanup_run_id,
        now=NOW,
        staging_root=target.parent.parent,
    )

    assert result.plan_ready is True
    assert result.block_reason == ""
    assert result.ready_count == 1
    assert result.skipped_count == 0
    assert [(item.item_key, item.status, item.reason) for item in result.items] == [
        (result.items[0].item_key, "ready", "staging_retention_expired")
    ]


def test_staging_preflight_blocks_a_modified_manifest(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, manifest, _ = _registered_staging_plan(tmp_path)
    manifest.write_text("tampered", encoding="utf-8")

    result = preflight_cleanup_plan(
        store, scope, cleanup_run_id, now=NOW, staging_root=manifest.parent / "staging"
    )

    assert result.plan_ready is False
    assert result.block_reason == "manifest_digest_mismatch"
    assert result.items == []


def test_staging_preflight_skips_a_file_that_changed_after_planning(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, _, target = _registered_staging_plan(tmp_path)
    target.write_bytes(b"changed-cover-evidence")

    result = preflight_cleanup_plan(
        store, scope, cleanup_run_id, now=NOW, staging_root=target.parent.parent
    )

    assert result.plan_ready is True
    assert result.ready_count == 0
    assert result.skipped_count == 1
    assert result.items[0].status == "skipped"
    assert result.items[0].reason == "file_size_changed"


def test_staging_preflight_rejects_same_size_content_tampering(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, _, target = _registered_staging_plan(tmp_path)
    target.write_bytes(b"other-evidence")
    old_timestamp = datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()
    os.utime(target, (old_timestamp, old_timestamp))

    result = preflight_cleanup_plan(
        store, scope, cleanup_run_id, now=NOW, staging_root=target.parent.parent
    )

    assert result.items[0].status == "skipped"
    assert result.items[0].reason == "content_hash_mismatch"


def test_staging_preflight_blocks_a_different_runtime_root(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, _, _ = _registered_staging_plan(tmp_path)
    different_root = tmp_path / "different-staging"
    different_root.mkdir()

    result = preflight_cleanup_plan(
        store, scope, cleanup_run_id, now=NOW, staging_root=different_root
    )

    assert result.plan_ready is False
    assert result.block_reason == "staging_root_mismatch"


def test_cleanup_plan_cannot_be_read_from_another_workspace(tmp_path: Path) -> None:
    store, _, cleanup_run_id, _, _ = _registered_staging_plan(tmp_path)
    other_scope = CoverAccessScope(workspace_key="other", user_id=9)

    result = preflight_cleanup_plan(
        store,
        other_scope,
        cleanup_run_id,
        now=NOW,
        staging_root=store.path.parent / "staging",
    )

    assert result.plan_ready is False
    assert result.block_reason == "cleanup_plan_not_found"
