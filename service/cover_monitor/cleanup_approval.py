from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from pathlib import Path
import re
import secrets

from service.cover_monitor.cleanup_preflight import preflight_cleanup_plan
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


@dataclass(frozen=True)
class CleanupApproval:
    cleanup_run_id: str
    status: str
    execution_token: str
    expires_at: str
    ready_count: int
    skipped_count: int


def approve_cleanup_plan(
    store: CoverMonitorStore,
    scope: CoverAccessScope,
    cleanup_run_id: str,
    *,
    expected_manifest_sha256: str,
    staging_root: str | Path | None = None,
    now: datetime | None = None,
    token_ttl_minutes: int = 15,
) -> CleanupApproval:
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    effective_now = effective_now.astimezone(timezone.utc)
    ttl_minutes = int(token_ttl_minutes)
    if ttl_minutes < 1 or ttl_minutes > 60:
        raise ValueError("token_ttl_minutes must be between 1 and 60")
    expected_digest = str(expected_manifest_sha256 or "").strip().lower()
    if re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
        raise ValueError("expected manifest digest is invalid")

    plan = store.get_cleanup_plan(scope, cleanup_run_id)
    if plan is None:
        raise ValueError("cleanup plan not found")
    if str(plan["status"]) != "planned":
        raise ValueError("cleanup plan is not planned")
    if not hmac.compare_digest(str(plan["manifest_sha256"]), expected_digest):
        raise ValueError("manifest digest does not match cleanup plan")
    preflight = preflight_cleanup_plan(
        store,
        scope,
        cleanup_run_id,
        now=effective_now,
        staging_root=staging_root,
    )
    if not preflight.plan_ready:
        raise ValueError(f"cleanup preflight blocked: {preflight.block_reason}")
    if preflight.ready_count <= 0:
        raise ValueError("cleanup preflight has no ready candidates")

    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = effective_now + timedelta(minutes=ttl_minutes)
    approved = store.approve_cleanup_plan(
        scope,
        cleanup_run_id,
        expected_manifest_sha256=expected_digest,
        execution_token_hash=token_hash,
        approved_at=effective_now.isoformat(timespec="seconds"),
        execution_token_expires_at=expires_at.isoformat(timespec="seconds"),
    )
    return CleanupApproval(
        cleanup_run_id=cleanup_run_id,
        status=str(approved["status"]),
        execution_token=raw_token,
        expires_at=expires_at.isoformat(timespec="seconds"),
        ready_count=preflight.ready_count,
        skipped_count=preflight.skipped_count,
    )
