from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath

from service.cover_monitor.cleanup_preflight import (
    preflight_cleanup_plan,
    preflight_staging_item,
)
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


@dataclass(frozen=True)
class StagingCleanupExecutionResult:
    cleanup_run_id: str
    status: str
    deleted_count: int
    skipped_count: int
    failed_count: int


def execute_staging_cleanup(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    cleanup_run_id: str,
    *,
    execution_token: str,
    staging_root: str | Path,
    confirm_delete: bool = False,
    worker_name: str = "cover-staging-cleanup",
    now: datetime | None = None,
) -> StagingCleanupExecutionResult:
    if confirm_delete is not True:
        raise ValueError("confirm_delete=True is required")
    effective_now = _effective_time(now)
    plan = store.get_cleanup_plan(scope, cleanup_run_id)
    if plan is None:
        raise ValueError("cleanup plan not found")
    if str(plan["cleanup_kind"]) != "staging":
        raise ValueError("only staging cleanup execution is supported")

    initial_preflight = preflight_cleanup_plan(
        store,
        scope,
        cleanup_run_id,
        now=effective_now,
        staging_root=staging_root,
    )
    if not initial_preflight.plan_ready:
        raise ValueError(f"cleanup preflight blocked: {initial_preflight.block_reason}")

    claimed = store.claim_cleanup_execution(
        scope,
        cleanup_run_id,
        execution_token=execution_token,
        worker_name=worker_name,
        now=effective_now,
    )
    lease_token = str(claimed["execution_lease_token"])
    running_preflight = preflight_cleanup_plan(
        store,
        scope,
        cleanup_run_id,
        now=effective_now,
        staging_root=staging_root,
    )
    if not running_preflight.plan_ready:
        _record_all_pending_as_failed(
            store,
            scope,
            claimed,
            worker_lease_token=lease_token,
            reason=f"preflight_blocked:{running_preflight.block_reason}",
            now=effective_now,
        )
        finished = store.finish_cleanup_execution(
            scope,
            cleanup_run_id,
            worker_lease_token=lease_token,
            finished_at=effective_now,
        )
        return _execution_result(finished)

    allowed_root = Path(staging_root).resolve()
    policy = claimed.get("policy")
    if not isinstance(policy, dict):
        raise RuntimeError("cleanup policy is unavailable after claim")
    stale_hours = int(policy["stale_hours"])
    raw_items = {
        str(item["item_key"]): item
        for item in claimed["items"]
        if isinstance(item, dict)
    }
    for decision in running_preflight.items:
        if not store.heartbeat_cleanup_execution(
            scope,
            cleanup_run_id,
            worker_lease_token=lease_token,
            now=_effective_time(now),
        ):
            raise RuntimeError("cleanup worker lease was lost")
        if decision.status != "ready":
            _record_result(
                store,
                scope,
                cleanup_run_id,
                decision.item_key,
                lease_token,
                "skipped",
                decision.reason,
                _effective_time(now),
            )
            continue
        item = raw_items.get(decision.item_key)
        if item is None:
            raise RuntimeError("cleanup item disappeared after claim")
        final_check = preflight_staging_item(
            allowed_root,
            item,
            now=_effective_time(now),
            stale_hours=stale_hours,
        )
        if final_check.status != "ready":
            _record_result(
                store,
                scope,
                cleanup_run_id,
                decision.item_key,
                lease_token,
                "skipped",
                final_check.reason,
                _effective_time(now),
            )
            continue
        try:
            _unlink_staging_candidate(allowed_root, decision.item_key)
        except FileNotFoundError:
            execution_status = "skipped"
            message = "file_missing_before_delete"
        except OSError as exc:
            execution_status = "failed"
            message = f"delete_failed:{type(exc).__name__}"
        else:
            execution_status = "deleted"
            message = "staging_file_deleted"
        _record_result(
            store,
            scope,
            cleanup_run_id,
            decision.item_key,
            lease_token,
            execution_status,
            message,
            _effective_time(now),
        )

    finished = store.finish_cleanup_execution(
        scope,
        cleanup_run_id,
        worker_lease_token=lease_token,
        finished_at=_effective_time(now),
    )
    return _execution_result(finished)


def _record_all_pending_as_failed(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    plan: dict[str, object],
    *,
    worker_lease_token: str,
    reason: str,
    now: datetime,
) -> None:
    for item in plan.get("items", []):
        if isinstance(item, dict) and item.get("execution_status") == "pending":
            _record_result(
                store,
                scope,
                str(plan["cleanup_run_id"]),
                str(item["item_key"]),
                worker_lease_token,
                "failed",
                reason,
                now,
            )


def _unlink_staging_candidate(root: Path, item_key: str) -> None:
    parts = PurePosixPath(item_key).parts
    if len(parts) != 2:
        raise ValueError("invalid staging cleanup path")
    supports_anchored_unlink = os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd
    if supports_anchored_unlink and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parent_fd = os.open(
                parts[0],
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                os.unlink(parts[1], dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)
        return

    target = root.joinpath(*parts)
    if target.is_symlink() or target.parent.is_symlink():
        raise OSError("symlink changed before delete")
    resolved = target.resolve(strict=True)
    resolved.relative_to(root)
    resolved.unlink()


def _record_result(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    cleanup_run_id: str,
    item_key: str,
    worker_lease_token: str,
    execution_status: str,
    message: str,
    now: datetime,
) -> None:
    store.record_cleanup_item_result(
        scope,
        cleanup_run_id,
        item_key=item_key,
        worker_lease_token=worker_lease_token,
        execution_status=execution_status,
        result_message=message,
        executed_at=now,
    )


def _execution_result(plan: dict[str, object]) -> StagingCleanupExecutionResult:
    items = [item for item in plan.get("items", []) if isinstance(item, dict)]
    return StagingCleanupExecutionResult(
        cleanup_run_id=str(plan["cleanup_run_id"]),
        status=str(plan["status"]),
        deleted_count=sum(item.get("execution_status") == "deleted" for item in items),
        skipped_count=sum(item.get("execution_status") == "skipped" for item in items),
        failed_count=sum(item.get("execution_status") == "failed" for item in items),
    )


def _effective_time(value: datetime | None) -> datetime:
    effective = value or datetime.now(timezone.utc)
    if effective.tzinfo is None:
        raise ValueError("now must include timezone")
    return effective.astimezone(timezone.utc)
