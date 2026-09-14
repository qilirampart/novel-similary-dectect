from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath

from service.cover_monitor.lifecycle import (
    CoverRetentionPolicy,
    build_asset_cleanup_plan,
    is_recognized_staging_path,
)
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


@dataclass(frozen=True)
class CleanupPreflightItem:
    item_key: str
    status: str
    reason: str


@dataclass(frozen=True)
class CleanupPreflightResult:
    cleanup_run_id: str
    plan_ready: bool
    block_reason: str
    ready_count: int
    skipped_count: int
    items: list[CleanupPreflightItem]


def preflight_cleanup_plan(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    cleanup_run_id: str,
    *,
    now: datetime | None = None,
    staging_root: str | Path | None = None,
) -> CleanupPreflightResult:
    plan = store.get_cleanup_plan(scope, cleanup_run_id)
    if plan is None:
        return _blocked(cleanup_run_id, "cleanup_plan_not_found")
    if str(plan["status"]) not in {"planned", "approved", "running"}:
        return _blocked(cleanup_run_id, "cleanup_plan_not_active")
    manifest_path = Path(str(plan["manifest_path"]))
    try:
        manifest_digest = _hash_file(manifest_path)
    except OSError:
        return _blocked(cleanup_run_id, "manifest_unavailable")
    if manifest_digest != str(plan["manifest_sha256"]):
        return _blocked(cleanup_run_id, "manifest_digest_mismatch")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    effective_now = effective_now.astimezone(timezone.utc)
    cleanup_kind = str(plan["cleanup_kind"])
    if cleanup_kind == "asset":
        return _preflight_asset_plan(
            store,
            scope,
            cleanup_run_id,
            plan,
            now=effective_now,
        )
    if cleanup_kind != "staging":
        return _blocked(cleanup_run_id, "cleanup_kind_not_supported")
    if staging_root is None:
        return _blocked(cleanup_run_id, "staging_root_required")

    policy = plan.get("policy")
    if not isinstance(policy, dict):
        return _blocked(cleanup_run_id, "invalid_policy")
    try:
        stale_hours = int(policy["stale_hours"])
        planned_root = Path(str(policy["staging_root"])).resolve()
        allowed_root = Path(staging_root).resolve()
    except (KeyError, TypeError, ValueError, OSError):
        return _blocked(cleanup_run_id, "invalid_policy")
    if stale_hours <= 0 or planned_root != allowed_root:
        return _blocked(cleanup_run_id, "staging_root_mismatch")
    if not allowed_root.is_dir():
        return _blocked(cleanup_run_id, "staging_root_unavailable")

    raw_items = plan.get("items")
    if not isinstance(raw_items, list):
        return _blocked(cleanup_run_id, "invalid_cleanup_items")
    items: list[CleanupPreflightItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            items.append(_skipped("", "invalid_cleanup_item"))
        elif str(item.get("execution_status") or "") != "pending":
            items.append(_skipped(str(item.get("item_key") or ""), "cleanup_item_not_pending"))
        else:
            items.append(
                preflight_staging_item(
                    allowed_root,
                    item,
                    now=effective_now,
                    stale_hours=stale_hours,
                )
            )
    ready_count = sum(item.status == "ready" for item in items)
    return CleanupPreflightResult(
        cleanup_run_id=cleanup_run_id,
        plan_ready=True,
        block_reason="",
        ready_count=ready_count,
        skipped_count=len(items) - ready_count,
        items=items,
    )


def _preflight_asset_plan(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    cleanup_run_id: str,
    plan: dict[str, object],
    *,
    now: datetime,
) -> CleanupPreflightResult:
    policy_data = plan.get("policy")
    if not isinstance(policy_data, dict):
        return _blocked(cleanup_run_id, "invalid_policy")
    try:
        policy = CoverRetentionPolicy(
            safe_days=int(policy_data["safe_days"]),
            unprocessed_days=int(policy_data["unprocessed_days"]),
        )
    except (KeyError, TypeError, ValueError):
        return _blocked(cleanup_run_id, "invalid_policy")

    results: list[CleanupPreflightItem] = []
    raw_items = plan.get("items")
    if not isinstance(raw_items, list):
        return _blocked(cleanup_run_id, "invalid_cleanup_items")
    for item in raw_items:
        if not isinstance(item, dict):
            results.append(_skipped("", "invalid_cleanup_item"))
            continue
        item_key = str(item.get("item_key") or "")
        if str(item.get("execution_status") or "") != "pending":
            results.append(_skipped(item_key, "cleanup_item_not_pending"))
            continue
        record = store.get_asset_lifecycle_record(scope, item_key)
        if record is None:
            results.append(_skipped(item_key, "asset_missing"))
            continue
        decision = build_asset_cleanup_plan([record], now=now, policy=policy)[0]
        if decision.action != "delete_candidate":
            results.append(_skipped(item_key, decision.reason))
            continue
        try:
            expected_size = int(item.get("byte_size", -1))
        except (TypeError, ValueError):
            results.append(_skipped(item_key, "invalid_cleanup_item"))
            continue
        if decision.byte_size != expected_size:
            results.append(_skipped(item_key, "asset_size_changed"))
            continue
        if decision.reason != str(item.get("reason") or ""):
            results.append(_skipped(item_key, "lifecycle_reason_changed"))
            continue
        results.append(
            CleanupPreflightItem(item_key=item_key, status="ready", reason=decision.reason)
        )
    ready_count = sum(item.status == "ready" for item in results)
    return CleanupPreflightResult(
        cleanup_run_id=cleanup_run_id,
        plan_ready=True,
        block_reason="",
        ready_count=ready_count,
        skipped_count=len(results) - ready_count,
        items=results,
    )


def preflight_staging_item(
    root: Path,
    item: dict[str, object],
    *,
    now: datetime,
    stale_hours: int,
) -> CleanupPreflightItem:
    item_key = str(item.get("item_key") or "")
    if not is_recognized_staging_path(item_key):
        return _skipped(item_key, "invalid_staging_path")
    parts = PurePosixPath(item_key).parts
    target = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return _skipped(item_key, "symlink_not_allowed")
    try:
        resolved = target.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return _skipped(item_key, "path_outside_staging_root")
    if not resolved.is_file():
        return _skipped(item_key, "file_missing")
    try:
        before = resolved.stat()
        expected_size = int(item.get("byte_size", -1))
    except (OSError, TypeError, ValueError):
        return _skipped(item_key, "invalid_file_metadata")
    if before.st_size != expected_size:
        return _skipped(item_key, "file_size_changed")
    modified_at = datetime.fromtimestamp(before.st_mtime, tz=timezone.utc)
    if modified_at >= now - timedelta(hours=stale_hours):
        return _skipped(item_key, "staging_retention_no_longer_expired")
    if ".part-" not in resolved.name:
        expected_hash = resolved.stem
        try:
            actual_hash = _hash_file(resolved)
            after = resolved.stat()
        except OSError:
            return _skipped(item_key, "file_changed_during_preflight")
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            return _skipped(item_key, "file_changed_during_preflight")
        if actual_hash != expected_hash:
            return _skipped(item_key, "content_hash_mismatch")
    return CleanupPreflightItem(
        item_key=item_key,
        status="ready",
        reason=str(item.get("reason") or "staging_retention_expired"),
    )


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _skipped(item_key: str, reason: str) -> CleanupPreflightItem:
    return CleanupPreflightItem(item_key=item_key, status="skipped", reason=reason)


def _blocked(cleanup_run_id: str, reason: str) -> CleanupPreflightResult:
    return CleanupPreflightResult(
        cleanup_run_id=str(cleanup_run_id),
        plan_ready=False,
        block_reason=reason,
        ready_count=0,
        skipped_count=0,
        items=[],
    )
