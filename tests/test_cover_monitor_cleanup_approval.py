from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path

import pytest

from service.cover_monitor.cleanup_approval import approve_cleanup_plan
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


def _staging_plan(tmp_path: Path) -> tuple[CoverMonitorStore, CoverAccessScope, dict, Path]:
    root = tmp_path / "staging"
    content = b"approval-cover"
    digest = sha256(content).hexdigest()
    relative_path = f"video001/{digest}.jpg"
    target = root / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
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
                "byte_size": len(content),
            }
        )
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    plan = store.register_cleanup_plan(
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
                "byte_size": len(content),
            }
        ],
    )
    return store, scope, plan, root


def test_approval_returns_one_time_raw_token_and_stores_only_its_hash(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)

    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
        token_ttl_minutes=15,
    )

    assert approval.status == "approved"
    assert approval.execution_token
    assert approval.ready_count == 1
    assert approval.expires_at == "2026-09-14T08:15:00+00:00"
    visible = store.get_cleanup_plan(scope, plan["cleanup_run_id"])
    assert visible is not None
    assert visible["status"] == "approved"
    assert "execution_token_hash" not in visible
    with store._connect() as conn:
        stored_hash = conn.execute(
            "SELECT execution_token_hash FROM cover_cleanup_runs WHERE cleanup_run_id = ?",
            (plan["cleanup_run_id"],),
        ).fetchone()[0]
    assert stored_hash == sha256(approval.execution_token.encode("utf-8")).hexdigest()
    assert stored_hash != approval.execution_token


def test_approval_rejects_wrong_or_changed_manifest(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)

    with pytest.raises(ValueError, match="manifest digest"):
        approve_cleanup_plan(
            store,
            scope,
            plan["cleanup_run_id"],
            expected_manifest_sha256="f" * 64,
            staging_root=root,
            now=NOW,
        )

    Path(plan["manifest_path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_digest_mismatch"):
        approve_cleanup_plan(
            store,
            scope,
            plan["cleanup_run_id"],
            expected_manifest_sha256=plan["manifest_sha256"],
            staging_root=root,
            now=NOW,
        )


def test_approval_is_single_transition_and_workspace_scoped(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )

    with pytest.raises(ValueError, match="not planned"):
        approve_cleanup_plan(
            store,
            scope,
            plan["cleanup_run_id"],
            expected_manifest_sha256=plan["manifest_sha256"],
            staging_root=root,
            now=NOW,
        )

    other_scope = CoverAccessScope(workspace_key="other", user_id=9)
    with pytest.raises(ValueError, match="not found"):
        approve_cleanup_plan(
            store,
            other_scope,
            plan["cleanup_run_id"],
            expected_manifest_sha256=plan["manifest_sha256"],
            staging_root=root,
            now=NOW,
        )


def test_execution_token_is_consumed_once_when_run_is_claimed(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )

    claimed = store.claim_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        execution_token=approval.execution_token,
        now=NOW + timedelta(minutes=1),
    )

    assert claimed["status"] == "running"
    assert claimed["started_at"] == "2026-09-14T08:01:00+00:00"
    assert claimed["execution_lease_token"]
    assert "worker_lease_hash" not in claimed
    with store._connect() as conn:
        token_fields = conn.execute(
            """
            SELECT execution_token_hash, execution_token_expires_at,
                   worker_lease_hash, last_heartbeat_at
              FROM cover_cleanup_runs WHERE cleanup_run_id = ?
            """,
            (plan["cleanup_run_id"],),
        ).fetchone()
    assert tuple(token_fields) == (
        None,
        None,
        sha256(claimed["execution_lease_token"].encode("utf-8")).hexdigest(),
        "2026-09-14T08:01:00+00:00",
    )
    with pytest.raises(ValueError, match="not approved"):
        store.claim_cleanup_execution(
            scope,
            plan["cleanup_run_id"],
            execution_token=approval.execution_token,
            now=NOW + timedelta(minutes=2),
        )


def test_wrong_execution_token_does_not_change_approval(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )

    with pytest.raises(ValueError, match="token is invalid"):
        store.claim_cleanup_execution(
            scope,
            plan["cleanup_run_id"],
            execution_token="wrong-token-value",
            now=NOW + timedelta(minutes=1),
        )
    assert store.get_cleanup_plan(scope, plan["cleanup_run_id"])["status"] == "approved"


def test_expired_execution_token_cancels_the_plan(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )

    with pytest.raises(ValueError, match="token has expired"):
        store.claim_cleanup_execution(
            scope,
            plan["cleanup_run_id"],
            execution_token=approval.execution_token,
            now=NOW + timedelta(minutes=16),
        )

    expired = store.get_cleanup_plan(scope, plan["cleanup_run_id"])
    assert expired["status"] == "cancelled"
    assert expired["finished_at"] == "2026-09-14T08:16:00+00:00"


def test_cleanup_item_results_are_immutable_and_finish_the_run(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )
    claimed = store.claim_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        execution_token=approval.execution_token,
        now=NOW + timedelta(minutes=1),
    )
    item_key = claimed["items"][0]["item_key"]
    lease_token = claimed["execution_lease_token"]

    recorded = store.record_cleanup_item_result(
        scope,
        plan["cleanup_run_id"],
        item_key=item_key,
        worker_lease_token=lease_token,
        execution_status="deleted",
        result_message="staging file deleted",
        executed_at=NOW + timedelta(minutes=2),
    )
    repeated = store.record_cleanup_item_result(
        scope,
        plan["cleanup_run_id"],
        item_key=item_key,
        worker_lease_token=lease_token,
        execution_status="deleted",
        result_message="staging file deleted",
        executed_at=NOW + timedelta(minutes=3),
    )
    assert recorded["execution_status"] == "deleted"
    assert repeated == recorded
    with pytest.raises(ValueError, match="already finalized"):
        store.record_cleanup_item_result(
            scope,
            plan["cleanup_run_id"],
            item_key=item_key,
            worker_lease_token=lease_token,
            execution_status="failed",
            result_message="rewrite attempt",
            executed_at=NOW + timedelta(minutes=3),
        )

    finished = store.finish_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        worker_lease_token=lease_token,
        finished_at=NOW + timedelta(minutes=4),
    )
    assert finished["status"] == "completed"
    assert finished["finished_at"] == "2026-09-14T08:04:00+00:00"


def test_cleanup_run_cannot_finish_with_pending_items(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )
    claimed = store.claim_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        execution_token=approval.execution_token,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="pending items"):
        store.finish_cleanup_execution(
            scope,
            plan["cleanup_run_id"],
            worker_lease_token=claimed["execution_lease_token"],
            finished_at=NOW + timedelta(minutes=2),
        )


def test_cleanup_execution_lease_controls_heartbeat_and_result_writes(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )
    claimed = store.claim_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        execution_token=approval.execution_token,
        worker_name="cleanup-test-worker",
        now=NOW + timedelta(minutes=1),
    )
    lease_token = claimed["execution_lease_token"]
    item_key = claimed["items"][0]["item_key"]

    assert store.heartbeat_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        worker_lease_token=lease_token,
        now=NOW + timedelta(minutes=2),
    )
    assert not store.heartbeat_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        worker_lease_token="wrong-lease",
        now=NOW + timedelta(minutes=3),
    )
    with pytest.raises(ValueError, match="worker lease"):
        store.record_cleanup_item_result(
            scope,
            plan["cleanup_run_id"],
            item_key=item_key,
            worker_lease_token="wrong-lease",
            execution_status="deleted",
            result_message="must not write",
            executed_at=NOW + timedelta(minutes=3),
        )


def test_stale_cleanup_execution_is_closed_and_pending_items_are_failed(tmp_path: Path) -> None:
    store, scope, plan, root = _staging_plan(tmp_path)
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )
    store.claim_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        execution_token=approval.execution_token,
        worker_name="crashed-worker",
        now=NOW + timedelta(minutes=1),
    )

    recovered = store.recover_stale_cleanup_execution(
        scope,
        plan["cleanup_run_id"],
        stale_before=NOW + timedelta(minutes=5),
        recovered_at=NOW + timedelta(minutes=10),
    )

    assert recovered["status"] == "partial_failed"
    assert recovered["items"][0]["execution_status"] == "failed"
    assert recovered["items"][0]["result_message"] == "execution_interrupted"
