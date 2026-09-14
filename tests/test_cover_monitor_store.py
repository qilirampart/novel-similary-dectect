from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from service.cover_monitor.store import (
    SCHEMA_PATH,
    CoverAccessScope,
    CoverMonitorStore,
    init_cover_db,
)


def test_init_cover_db_is_idempotent_and_enables_required_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "cover-monitor.sqlite3"

    init_cover_db(db_path)
    init_cover_db(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = conn.execute(
            "SELECT MAX(version) FROM cover_schema_versions"
        ).fetchone()[0]
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert version == 10
    assert "idx_cover_runs_claim" in indexes
    assert "idx_cover_task_items_run_claim" in indexes
    assert "uq_cover_detections_task_item" in indexes
    assert "uq_cover_case_events_system_detection" in indexes
    assert "idx_cover_assets_lifecycle" in indexes
    assert "idx_cover_detections_asset_risk" in indexes
    assert "idx_cover_case_events_detection" in indexes
    assert {
        "cover_channels",
        "cover_videos",
        "cover_runs",
        "cover_task_items",
        "cover_assets",
        "cover_detections",
        "cover_risk_cases",
        "cover_case_events",
        "cover_import_conflicts",
        "cover_historical_observations",
        "cover_cleanup_runs",
        "cover_cleanup_items",
    }.issubset(tables)


def test_init_cover_db_migrates_existing_assets_to_local_storage_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "cover-monitor-v6.sqlite3"
    legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "    storage_backend TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local', 'oss')),\n",
        "",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO cover_schema_versions (version, applied_at) VALUES (6, '2026-09-12T00:00:00+00:00')"
        )
        conn.execute(
            """
            INSERT INTO cover_assets (
                asset_id, workspace_key, video_pk, content_sha256, storage_key,
                original_url, fetched_url, mime_type, byte_size, width, height,
                fetched_at, created_at
            ) VALUES ('legacy', 'internal', 999, ?, ?, ?, ?, 'image/jpeg', 1024, 1280, 720, ?, ?)
            """,
            (
                "a" * 64,
                f"video-legacy/{'a' * 64}.jpg",
                "https://i.ytimg.com/vi/video-legacy/hqdefault.jpg",
                "https://i.ytimg.com/vi/video-legacy/hqdefault.jpg",
                "2026-09-12T00:00:00+00:00",
                "2026-09-12T00:00:00+00:00",
            ),
        )

    init_cover_db(db_path)

    with sqlite3.connect(db_path) as conn:
        backend = conn.execute(
            "SELECT storage_backend FROM cover_assets WHERE asset_id = 'legacy'"
        ).fetchone()[0]
        version = conn.execute("SELECT MAX(version) FROM cover_schema_versions").fetchone()[0]
    assert backend == "local"
    assert version == 10


def test_init_cover_db_adds_cleanup_token_columns_to_v9_database(tmp_path: Path) -> None:
    db_path = tmp_path / "cover-monitor-v9.sqlite3"
    legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "    execution_token_hash TEXT,\n    execution_token_expires_at TEXT,\n",
        "",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO cover_schema_versions (version, applied_at) VALUES (9, ?)",
            ("2026-09-13T00:00:00+00:00",),
        )

    init_cover_db(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cover_cleanup_runs)").fetchall()
        }
        version = conn.execute("SELECT MAX(version) FROM cover_schema_versions").fetchone()[0]
    assert {"execution_token_hash", "execution_token_expires_at"}.issubset(columns)
    assert version == 10


def test_cleanup_plan_registration_is_workspace_scoped_and_auditable(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    cleanup = store.register_cleanup_plan(
        scope,
        cleanup_kind="staging",
        manifest_path="manifests/staging-20260913.csv",
        manifest_sha256="a" * 64,
        policy={"stale_hours": 24},
        total_count=9,
        candidate_items=[
            {
                "item_key": f"video001/{'b' * 64}.jpg",
                "reason": "staging_retention_expired",
                "byte_size": 2048,
            }
        ],
    )

    assert cleanup["workspace_key"] == "internal"
    assert cleanup["cleanup_kind"] == "staging"
    assert cleanup["status"] == "planned"
    assert cleanup["total_count"] == 9
    assert cleanup["candidate_count"] == 1
    assert cleanup["candidate_bytes"] == 2048
    assert cleanup["created_by_user_id"] == 7
    assert cleanup["policy"] == {"stale_hours": 24}
    assert cleanup["items"] == [
        {
            "item_key": f"video001/{'b' * 64}.jpg",
            "reason": "staging_retention_expired",
            "byte_size": 2048,
            "execution_status": "pending",
            "result_message": None,
            "executed_at": None,
        }
    ]

    repeated = store.register_cleanup_plan(
        scope,
        cleanup_kind="staging",
        manifest_path="manifests/staging-20260913.csv",
        manifest_sha256="a" * 64,
        policy={"stale_hours": 24},
        total_count=9,
        candidate_items=[
            {
                "item_key": f"video001/{'b' * 64}.jpg",
                "reason": "staging_retention_expired",
                "byte_size": 2048,
            }
        ],
    )
    assert repeated["cleanup_run_id"] == cleanup["cleanup_run_id"]


def test_cleanup_plan_registration_rejects_invalid_digest_and_duplicate_items(
    tmp_path: Path,
) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    with pytest.raises(ValueError, match="manifest_sha256"):
        store.register_cleanup_plan(
            scope,
            cleanup_kind="asset",
            manifest_path="manifest.csv",
            manifest_sha256="not-a-digest",
            policy={},
            total_count=0,
            candidate_items=[],
        )

    duplicate = {"item_key": "asset-1", "reason": "expired", "byte_size": 1}
    with pytest.raises(ValueError, match="duplicate"):
        store.register_cleanup_plan(
            scope,
            cleanup_kind="asset",
            manifest_path="manifest.csv",
            manifest_sha256="c" * 64,
            policy={},
            total_count=2,
            candidate_items=[duplicate, duplicate],
        )


def test_channel_and_video_identity_are_unique_inside_workspace(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    first_channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-001",
        name="频道初始名",
        source_url="https://www.youtube.com/channel/UC-001",
    )
    updated_channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-001",
        name="频道新名称",
        source_url="https://www.youtube.com/@example",
    )
    first_video = store.upsert_video(
        scope,
        channel_pk=first_channel["channel_pk"],
        platform="youtube",
        video_id="video-001",
        title="第一版标题",
        video_url="https://www.youtube.com/watch?v=video-001",
        thumbnail_url="https://i.ytimg.com/vi/video-001/hqdefault.jpg",
    )
    updated_video = store.upsert_video(
        scope,
        channel_pk=first_channel["channel_pk"],
        platform="youtube",
        video_id="video-001",
        title="更新后的标题",
        video_url="https://www.youtube.com/watch?v=video-001",
        thumbnail_url="https://i.ytimg.com/vi/video-001/maxresdefault.jpg",
    )

    assert first_channel["channel_pk"] == updated_channel["channel_pk"]
    assert updated_channel["name"] == "频道新名称"
    assert first_video["video_pk"] == updated_video["video_pk"]
    assert updated_video["title"] == "更新后的标题"
    assert len(store.list_channels(scope)) == 1


def test_save_asset_persists_and_validates_storage_backend(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-storage",
        name="Storage Channel",
        source_url="https://www.youtube.com/channel/UC-storage",
    )
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-storage",
        title="Storage Video",
        video_url="https://www.youtube.com/watch?v=video-storage",
        thumbnail_url="https://i.ytimg.com/vi/video-storage/hqdefault.jpg",
    )
    asset = {
        "storage_backend": "oss",
        "content_sha256": "b" * 64,
        "storage_key": f"video-storage/{'b' * 64}.jpg",
        "original_url": video["thumbnail_url"],
        "fetched_url": video["thumbnail_url"],
        "mime_type": "image/jpeg",
        "byte_size": 1024,
        "width": 1280,
        "height": 720,
    }

    saved = store.save_asset(scope, video_pk=video["video_pk"], asset=asset)

    assert saved["storage_backend"] == "oss"
    with pytest.raises(ValueError, match="storage_backend"):
        store.save_asset(
            scope,
            video_pk=video["video_pk"],
            asset={**asset, "content_sha256": "c" * 64, "storage_backend": "unknown"},
        )


def test_save_asset_promotes_duplicate_local_content_to_oss_without_downgrade(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-promote",
        name="Promote Channel",
        source_url="https://www.youtube.com/channel/UC-promote",
    )
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-promote",
        title="Promote Video",
        video_url="https://www.youtube.com/watch?v=video-promote",
        thumbnail_url="https://i.ytimg.com/vi/video-promote/hqdefault.jpg",
    )
    base_asset = {
        "content_sha256": "d" * 64,
        "storage_key": f"video-promote/{'d' * 64}.jpg",
        "original_url": video["thumbnail_url"],
        "fetched_url": video["thumbnail_url"],
        "mime_type": "image/jpeg",
        "byte_size": 1024,
        "width": 1280,
        "height": 720,
    }

    local = store.save_asset(
        scope,
        video_pk=video["video_pk"],
        asset={**base_asset, "storage_backend": "local"},
    )
    promoted = store.save_asset(
        scope,
        video_pk=video["video_pk"],
        asset={**base_asset, "storage_backend": "oss"},
    )
    not_downgraded = store.save_asset(
        scope,
        video_pk=video["video_pk"],
        asset={**base_asset, "storage_backend": "local"},
    )

    assert promoted["asset_id"] == local["asset_id"]
    assert promoted["storage_backend"] == "oss"
    assert not_downgraded["storage_backend"] == "oss"


def test_asset_lifecycle_records_mark_only_the_latest_version(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-lifecycle",
        name="Lifecycle Channel",
        source_url="https://www.youtube.com/channel/UC-lifecycle",
    )
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-lifecycle",
        title="Lifecycle Video",
        video_url="https://www.youtube.com/watch?v=video-lifecycle",
        thumbnail_url="https://i.ytimg.com/vi/video-lifecycle/hqdefault.jpg",
    )
    for marker in ("e", "f"):
        store.save_asset(
            scope,
            video_pk=video["video_pk"],
            asset={
                "content_sha256": marker * 64,
                "storage_key": f"video-lifecycle/{marker * 64}.jpg",
                "original_url": video["thumbnail_url"],
                "fetched_url": video["thumbnail_url"],
                "mime_type": "image/jpeg",
                "byte_size": 1024,
                "width": 1280,
                "height": 720,
            },
        )
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_assets SET fetched_at = '2026-06-01T00:00:00+00:00' WHERE content_sha256 = ?",
            ("e" * 64,),
        )
        conn.execute(
            "UPDATE cover_assets SET fetched_at = '2026-09-01T00:00:00+00:00' WHERE content_sha256 = ?",
            ("f" * 64,),
        )

    records = store.list_asset_lifecycle_records(scope, limit=100, offset=0)

    assert records["total"] == 2
    assert [item["is_latest_for_video"] for item in records["items"]] == [0, 1]
    assert all(item["has_safe_detection"] == 0 for item in records["items"])
    assert all(item["has_sensitive_detection"] == 0 for item in records["items"])
    assert all(item["is_case_evidence"] == 0 for item in records["items"])


def test_workspace_scope_blocks_cross_workspace_reads(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    internal = CoverAccessScope(workspace_key="internal", user_id=7)
    isolated = CoverAccessScope(workspace_key="isolated", user_id=9)
    channel = store.upsert_channel(
        internal,
        platform="youtube",
        channel_id="UC-private",
        name="内部频道",
        source_url="https://www.youtube.com/channel/UC-private",
    )

    assert store.get_channel(internal, channel["channel_pk"]) is not None
    assert store.get_channel(isolated, channel["channel_pk"]) is None
    assert store.list_channels(isolated) == []


def test_database_rejects_cross_workspace_video_channel_reference(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    internal = CoverAccessScope(workspace_key="internal", user_id=7)
    isolated = CoverAccessScope(workspace_key="isolated", user_id=9)
    channel = store.upsert_channel(
        internal,
        platform="youtube",
        channel_id="UC-private",
        name="内部频道",
        source_url="https://www.youtube.com/channel/UC-private",
    )

    with pytest.raises(ValueError, match="channel is not visible"):
        store.upsert_video(
            isolated,
            channel_pk=channel["channel_pk"],
            platform="youtube",
            video_id="video-private",
            title="不应写入",
            video_url="https://www.youtube.com/watch?v=video-private",
            thumbnail_url="https://i.ytimg.com/vi/video-private/hqdefault.jpg",
        )


def test_overview_only_counts_current_workspace(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    internal = CoverAccessScope(workspace_key="internal", user_id=7)
    isolated = CoverAccessScope(workspace_key="isolated", user_id=9)
    store.upsert_channel(
        internal,
        platform="youtube",
        channel_id="UC-internal",
        name="内部频道",
        source_url="https://www.youtube.com/channel/UC-internal",
    )
    store.upsert_channel(
        isolated,
        platform="youtube",
        channel_id="UC-isolated",
        name="隔离频道",
        source_url="https://www.youtube.com/channel/UC-isolated",
    )

    overview = store.get_overview(internal)

    assert overview == {
        "channel_count": 1,
        "video_count": 0,
        "risk_count": 0,
        "pending_review_count": 0,
        "risk_distribution": {"safe": 0, "review": 0, "risk": 0, "unknown": 0},
        "latest_run": None,
    }


def _create_cover_run_fixture(store: CoverMonitorStore, scope: CoverAccessScope) -> dict:
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-worker",
        name="Worker Channel",
        source_url="https://www.youtube.com/channel/UC-worker",
    )
    return store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": False},
        model_snapshot={"provider": "test", "model": "vision-model"},
        prompt_version="cover-visible-evidence-v1",
    )


def test_cover_run_claim_uses_exclusive_lease_and_rejects_late_heartbeat(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    created = _create_cover_run_fixture(store, scope)

    claimed = store.claim_next_run(worker_name="cover-worker-1")

    assert claimed is not None
    assert claimed["run_id"] == created["run_id"]
    assert claimed["status"] == "running"
    assert len(claimed["worker_lease_token"]) == 32
    assert store.claim_next_run(worker_name="cover-worker-2") is None
    assert store.heartbeat_run(created["run_id"], claimed["worker_lease_token"])
    assert not store.heartbeat_run(created["run_id"], "stale-token")


def test_cover_run_pause_resume_and_cancel_are_lease_safe(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    created = _create_cover_run_fixture(store, scope)
    claimed = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed is not None

    assert store.request_pause(scope, created["run_id"])["status"] == "pause_requested"
    assert store.settle_requested_control(created["run_id"], claimed["worker_lease_token"]) == "paused"
    assert store.resume_run(scope, created["run_id"])["status"] == "queued"

    reclaimed = store.claim_next_run(worker_name="cover-worker-2")
    assert reclaimed is not None
    assert reclaimed["worker_lease_token"] != claimed["worker_lease_token"]
    assert store.request_cancel(scope, created["run_id"])["status"] == "cancel_requested"
    assert store.settle_requested_control(created["run_id"], reclaimed["worker_lease_token"]) == "cancelled"


def test_cover_run_recovery_resolves_stale_control_requests(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    paused_run = _create_cover_run_fixture(store, scope)
    paused_claim = store.claim_next_run(worker_name="cover-worker-1")
    assert paused_claim is not None
    store.request_pause(scope, paused_run["run_id"])
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_runs SET last_heartbeat_at = ? WHERE run_id = ?",
            ("2020-01-01T00:00:00+00:00", paused_run["run_id"]),
        )

    recovered = store.recover_stale_runs(stale_before="2021-01-01T00:00:00+00:00")

    assert recovered == {"requeued": 0, "paused": 1, "cancelled": 0}
    assert store.get_run(scope, paused_run["run_id"])["status"] == "paused"


def test_cover_task_items_are_idempotent_and_use_independent_leases(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    videos = [
        store.upsert_video(
            scope,
            channel_pk=channel["channel_pk"],
            platform="youtube",
            video_id=f"video-{index:03d}",
            title=f"Video {index}",
            video_url=f"https://www.youtube.com/watch?v=video-{index:03d}",
            thumbnail_url=f"https://i.ytimg.com/vi/video-{index:03d}/hqdefault.jpg",
        )
        for index in range(1, 3)
    ]
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None

    inserted = store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "new_video"} for video in videos],
    )
    duplicate_inserted = store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "new_video"} for video in videos],
    )

    assert inserted == 2
    assert duplicate_inserted == 0
    assert store.get_run(scope, run["run_id"])["total_item_count"] == 2

    first = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    second = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert first is not None and second is not None
    assert first["task_item_id"] != second["task_item_id"]
    assert first["status"] == "running"
    assert first["stage"] == "download"
    assert first["attempts"] == 1
    assert len(first["worker_lease_token"]) == 32
    assert store.heartbeat_task_item(first["task_item_id"], first["worker_lease_token"])
    assert not store.heartbeat_task_item(first["task_item_id"], "stale-token")


def test_cover_task_item_progress_and_terminal_counts_are_lease_safe(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-stage-001",
        title="Stage Video",
        video_url="https://www.youtube.com/watch?v=video-stage-001",
        thumbnail_url="https://i.ytimg.com/vi/video-stage-001/hqdefault.jpg",
    )
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "manual"}],
    )
    item = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert item is not None

    assert store.advance_task_item(
        item["task_item_id"], item["worker_lease_token"], expected_stage="download", next_stage="review"
    )
    assert not store.advance_task_item(
        item["task_item_id"], "stale-token", expected_stage="review", next_stage="persist"
    )
    assert store.advance_task_item(
        item["task_item_id"], item["worker_lease_token"], expected_stage="review", next_stage="persist"
    )
    assert store.finish_task_item(item["task_item_id"], item["worker_lease_token"], succeeded=True)
    assert not store.finish_task_item(item["task_item_id"], item["worker_lease_token"], succeeded=True)

    finished = store.get_run(scope, run["run_id"])
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["completed_item_count"] == 1
    assert finished["failed_item_count"] == 0


def test_stale_run_recovery_requeues_claimed_task_item(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-recovery-001",
        title="Recovery Video",
        video_url="https://www.youtube.com/watch?v=video-recovery-001",
        thumbnail_url="https://i.ytimg.com/vi/video-recovery-001/hqdefault.jpg",
    )
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "new_video"}],
    )
    claimed_item = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert claimed_item is not None
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_runs SET last_heartbeat_at = ? WHERE run_id = ?",
            ("2020-01-01T00:00:00+00:00", run["run_id"]),
        )
        conn.execute(
            "UPDATE cover_task_items SET last_heartbeat_at = ? WHERE task_item_id = ?",
            ("2020-01-01T00:00:00+00:00", claimed_item["task_item_id"]),
        )

    recovered = store.recover_stale_runs(stale_before="2021-01-01T00:00:00+00:00")
    recovered_item = store.get_task_item(claimed_item["task_item_id"])

    assert recovered["requeued"] == 1
    assert recovered_item is not None
    assert recovered_item["status"] == "queued"
    assert recovered_item["worker_lease_token"] is None
    reclaimed_run = store.claim_next_run(worker_name="cover-worker-2")
    assert reclaimed_run is not None
    reclaimed_item = store.claim_next_task_item(run["run_id"], reclaimed_run["worker_lease_token"])
    assert reclaimed_item is not None
    assert reclaimed_item["attempts"] == 2


def test_retry_wait_item_is_not_claimed_before_due_time(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-retry-001",
        title="Retry Video",
        video_url="https://www.youtube.com/watch?v=video-retry-001",
        thumbnail_url="https://i.ytimg.com/vi/video-retry-001/hqdefault.jpg",
    )
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "retry_unknown"}],
    )
    item = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert item is not None

    assert store.retry_task_item(
        item["task_item_id"],
        item["worker_lease_token"],
        next_retry_at="2999-01-01T00:00:00+00:00",
        error_type="timeout",
        error_message="provider timed out",
    )
    assert store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"]) is None
    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_task_items SET next_retry_at = ? WHERE task_item_id = ?",
            ("2020-01-01T00:00:00+00:00", item["task_item_id"]),
        )
    retried = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert retried is not None
    assert retried["attempts"] == 2
    assert retried["stage"] == "download"


def test_cancel_request_wins_when_last_task_item_finishes(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-cancel-race-001",
        title="Cancel Race Video",
        video_url="https://www.youtube.com/watch?v=video-cancel-race-001",
        thumbnail_url="https://i.ytimg.com/vi/video-cancel-race-001/hqdefault.jpg",
    )
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "manual"}],
    )
    item = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert item is not None

    assert store.request_cancel(scope, run["run_id"])["status"] == "cancel_requested"
    assert store.finish_task_item(item["task_item_id"], item["worker_lease_token"], succeeded=True)
    after_finish = store.get_run(scope, run["run_id"])
    assert after_finish is not None
    assert after_finish["status"] == "cancel_requested"
    assert after_finish["worker_lease_token"] == claimed_run["worker_lease_token"]
    assert store.settle_requested_control(run["run_id"], claimed_run["worker_lease_token"]) == "cancelled"


def test_worker_shutdown_requeues_run_and_releases_claimed_item_lease(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover-monitor.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    run = _create_cover_run_fixture(store, scope)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-shutdown-001",
        title="Shutdown Video",
        video_url="https://www.youtube.com/watch?v=video-shutdown-001",
        thumbnail_url="https://i.ytimg.com/vi/video-shutdown-001/hqdefault.jpg",
    )
    claimed_run = store.claim_next_run(worker_name="cover-worker-1")
    assert claimed_run is not None
    store.enqueue_task_items(
        run["run_id"],
        claimed_run["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "manual"}],
    )
    claimed_item = store.claim_next_task_item(run["run_id"], claimed_run["worker_lease_token"])
    assert claimed_item is not None

    status = store.release_run_for_worker_shutdown(
        run["run_id"],
        claimed_run["worker_lease_token"],
    )

    assert status == "queued"
    released_run = store.get_run(scope, run["run_id"])
    released_item = store.get_task_item(claimed_item["task_item_id"])
    assert released_run is not None and released_run["status"] == "queued"
    assert released_run["worker_lease_token"] is None
    assert released_item is not None and released_item["status"] == "queued"
    assert released_item["worker_lease_token"] is None
    assert not store.heartbeat_task_item(claimed_item["task_item_id"], claimed_item["worker_lease_token"])
