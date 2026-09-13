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


def _registered_asset_plan(
    tmp_path: Path,
) -> tuple[CoverMonitorStore, CoverAccessScope, str, str]:
    store = CoverMonitorStore(tmp_path / "asset-cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-preflight",
        name="Preflight Channel",
        source_url="https://www.youtube.com/channel/UC-preflight",
    )
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-preflight",
        title="Preflight Video",
        video_url="https://www.youtube.com/watch?v=video-preflight",
        thumbnail_url="https://i.ytimg.com/vi/video-preflight/hqdefault.jpg",
    )
    assets = []
    for marker in ("e", "f"):
        assets.append(
            store.save_asset(
                scope,
                video_pk=video["video_pk"],
                asset={
                    "content_sha256": marker * 64,
                    "storage_key": f"video-preflight/{marker * 64}.jpg",
                    "original_url": video["thumbnail_url"],
                    "fetched_url": video["thumbnail_url"],
                    "mime_type": "image/jpeg",
                    "byte_size": 1024,
                    "width": 1280,
                    "height": 720,
                },
            )
        )
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_assets SET fetched_at = ? WHERE asset_id = ?",
            ("2026-06-01T00:00:00+00:00", assets[0]["asset_id"]),
        )
        conn.execute(
            "UPDATE cover_assets SET fetched_at = ? WHERE asset_id = ?",
            ("2026-09-01T00:00:00+00:00", assets[1]["asset_id"]),
        )

    manifest = tmp_path / "asset-manifest.csv"
    manifest.write_text("registered asset cleanup manifest", encoding="utf-8")
    cleanup = store.register_cleanup_plan(
        scope,
        cleanup_kind="asset",
        manifest_path=str(manifest),
        manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
        policy={"safe_days": 60, "unprocessed_days": 14},
        total_count=2,
        candidate_items=[
            {
                "item_key": assets[0]["asset_id"],
                "reason": "unprocessed_retention_expired",
                "byte_size": 1024,
            }
        ],
    )
    return store, scope, cleanup["cleanup_run_id"], assets[0]["asset_id"]


def test_asset_preflight_rechecks_current_lifecycle_state(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, asset_id = _registered_asset_plan(tmp_path)

    result = preflight_cleanup_plan(store, scope, cleanup_run_id, now=NOW)

    assert result.plan_ready is True
    assert result.ready_count == 1
    assert [(item.item_key, item.status, item.reason) for item in result.items] == [
        (asset_id, "ready", "unprocessed_retention_expired")
    ]


def test_asset_preflight_skips_an_asset_that_became_latest(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, asset_id = _registered_asset_plan(tmp_path)
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_assets SET fetched_at = ? WHERE asset_id = ?",
            ("2026-09-02T00:00:00+00:00", asset_id),
        )

    result = preflight_cleanup_plan(store, scope, cleanup_run_id, now=NOW)

    assert result.plan_ready is True
    assert result.ready_count == 0
    assert result.items[0].status == "skipped"
    assert result.items[0].reason == "latest_video_asset"


def test_asset_preflight_skips_an_asset_that_became_case_evidence(tmp_path: Path) -> None:
    store, scope, cleanup_run_id, asset_id = _registered_asset_plan(tmp_path)
    channel = store.list_channels(scope)[0]
    asset = store.get_asset_lifecycle_record(scope, asset_id)
    assert asset is not None
    run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={},
        model_snapshot={"provider": "test", "model": "vision-test"},
        prompt_version="preflight-test-v1",
    )
    claimed_run = store.claim_next_run(worker_name="preflight-test-worker")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": asset["video_pk"], "reason": "manual"}],
    )
    task = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert task is not None
    store.save_detection_and_case(
        scope,
        task_item_id=task["task_item_id"],
        worker_lease_token=task["worker_lease_token"],
        asset_id=asset_id,
        detection={
            "overall_risk": "review",
            "risk_tags": ["manual-review"],
            "summary": "需要复核",
            "evidence": "测试证据",
            "confidence": 0.5,
            "provider": "test",
            "model": "vision-test",
            "prompt_version": "preflight-test-v1",
            "prompt_hash": "a" * 64,
            "intensity": "standard",
            "input_snapshot": {},
            "raw_response": "{}",
            "duration_seconds": 0.1,
        },
    )

    result = preflight_cleanup_plan(store, scope, cleanup_run_id, now=NOW)

    assert result.ready_count == 0
    assert result.items[0].status == "skipped"
    assert result.items[0].reason == "case_evidence"
