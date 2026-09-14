from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path

import pytest

import service.cover_monitor.cleanup_executor as cleanup_executor
from service.cover_monitor.cleanup_approval import approve_cleanup_plan
from service.cover_monitor.cleanup_executor import execute_staging_cleanup
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


NOW = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _approved_staging_plan(
    tmp_path: Path,
) -> tuple[CoverMonitorStore, CoverAccessScope, dict, Path, Path]:
    root = tmp_path / "isolated-staging"
    content = b"isolated-cleanup-cover"
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
    approval = approve_cleanup_plan(
        store,
        scope,
        plan["cleanup_run_id"],
        expected_manifest_sha256=plan["manifest_sha256"],
        staging_root=root,
        now=NOW,
    )
    return store, scope, {**plan, "execution_token": approval.execution_token}, root, target


def test_staging_executor_requires_explicit_delete_confirmation(tmp_path: Path) -> None:
    store, scope, plan, root, target = _approved_staging_plan(tmp_path)

    with pytest.raises(ValueError, match="confirm_delete"):
        execute_staging_cleanup(
            store,
            scope,
            plan["cleanup_run_id"],
            execution_token=plan["execution_token"],
            staging_root=root,
            confirm_delete=False,
            now=NOW,
        )

    assert target.is_file()
    assert store.get_cleanup_plan(scope, plan["cleanup_run_id"])["status"] == "approved"


def test_staging_executor_deletes_only_a_revalidated_candidate(tmp_path: Path) -> None:
    store, scope, plan, root, target = _approved_staging_plan(tmp_path)

    result = execute_staging_cleanup(
        store,
        scope,
        plan["cleanup_run_id"],
        execution_token=plan["execution_token"],
        staging_root=root,
        confirm_delete=True,
        now=NOW,
    )

    assert not target.exists()
    assert result.status == "completed"
    assert result.deleted_count == 1
    assert result.skipped_count == 0
    assert result.failed_count == 0
    audited = store.get_cleanup_plan(scope, plan["cleanup_run_id"])
    assert audited["items"][0]["execution_status"] == "deleted"


def test_staging_executor_skips_a_candidate_that_changed_after_approval(tmp_path: Path) -> None:
    store, scope, plan, root, target = _approved_staging_plan(tmp_path)
    recent_timestamp = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc).timestamp()
    os.utime(target, (recent_timestamp, recent_timestamp))

    result = execute_staging_cleanup(
        store,
        scope,
        plan["cleanup_run_id"],
        execution_token=plan["execution_token"],
        staging_root=root,
        confirm_delete=True,
        now=NOW,
    )

    assert target.is_file()
    assert result.status == "completed"
    assert result.deleted_count == 0
    assert result.skipped_count == 1
    audited = store.get_cleanup_plan(scope, plan["cleanup_run_id"])
    assert audited["items"][0]["execution_status"] == "skipped"
    assert audited["items"][0]["result_message"] == "staging_retention_no_longer_expired"


def test_staging_executor_does_not_consume_token_when_manifest_changed(tmp_path: Path) -> None:
    store, scope, plan, root, target = _approved_staging_plan(tmp_path)
    Path(plan["manifest_path"]).write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_digest_mismatch"):
        execute_staging_cleanup(
            store,
            scope,
            plan["cleanup_run_id"],
            execution_token=plan["execution_token"],
            staging_root=root,
            confirm_delete=True,
            now=NOW,
        )

    assert target.is_file()
    assert store.get_cleanup_plan(scope, plan["cleanup_run_id"])["status"] == "approved"


def test_staging_executor_audits_delete_errors_without_losing_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, scope, plan, root, target = _approved_staging_plan(tmp_path)

    def deny_target_unlink(root_path: Path, item_key: str) -> None:
        raise PermissionError("test denial")

    monkeypatch.setattr(cleanup_executor, "_unlink_staging_candidate", deny_target_unlink)

    result = execute_staging_cleanup(
        store,
        scope,
        plan["cleanup_run_id"],
        execution_token=plan["execution_token"],
        staging_root=root,
        confirm_delete=True,
        now=NOW,
    )

    assert target.is_file()
    assert result.status == "partial_failed"
    assert result.failed_count == 1
    audited = store.get_cleanup_plan(scope, plan["cleanup_run_id"])
    assert audited["items"][0]["execution_status"] == "failed"
    assert audited["items"][0]["result_message"] == "delete_failed:PermissionError"
