from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal


CleanupAction = Literal["keep", "delete_candidate"]


@dataclass(frozen=True)
class CoverRetentionPolicy:
    safe_days: int = 60
    unprocessed_days: int = 14

    def __post_init__(self) -> None:
        if self.safe_days <= 0 or self.unprocessed_days <= 0:
            raise ValueError("封面保留天数必须大于 0")


@dataclass(frozen=True)
class CoverAssetCleanupDecision:
    asset_id: str
    storage_backend: str
    storage_key: str
    action: CleanupAction
    reason: str
    fetched_at: str
    byte_size: int


def build_asset_cleanup_plan(
    records: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    policy: CoverRetentionPolicy | None = None,
) -> list[CoverAssetCleanupDecision]:
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    effective_now = effective_now.astimezone(timezone.utc)
    retention = policy or CoverRetentionPolicy()
    return [
        _classify_asset(record, now=effective_now, policy=retention)
        for record in records
    ]


def _classify_asset(
    record: dict[str, Any],
    *,
    now: datetime,
    policy: CoverRetentionPolicy,
) -> CoverAssetCleanupDecision:
    asset_id = str(record.get("asset_id") or "").strip()
    backend = str(record.get("storage_backend") or "").strip().lower()
    storage_key = str(record.get("storage_key") or "").strip()
    fetched_at_text = str(record.get("fetched_at") or "").strip()
    flags = _read_flags(record)
    fetched_at = _parse_timestamp(fetched_at_text)
    byte_size = _parse_nonnegative_int(record.get("byte_size"))
    if (
        not asset_id
        or backend not in {"local", "oss"}
        or not storage_key
        or flags is None
        or fetched_at is None
        or byte_size is None
    ):
        return _decision(record, action="keep", reason="invalid_metadata")

    is_case_evidence, has_sensitive, has_safe, is_latest = flags
    if is_case_evidence:
        return _decision(record, action="keep", reason="case_evidence")
    if has_sensitive:
        return _decision(record, action="keep", reason="sensitive_detection")
    if is_latest:
        return _decision(record, action="keep", reason="latest_video_asset")
    if has_safe:
        expired = fetched_at < now - timedelta(days=policy.safe_days)
        return _decision(
            record,
            action="delete_candidate" if expired else "keep",
            reason="safe_retention_expired" if expired else "safe_within_retention",
        )
    expired = fetched_at < now - timedelta(days=policy.unprocessed_days)
    return _decision(
        record,
        action="delete_candidate" if expired else "keep",
        reason="unprocessed_retention_expired" if expired else "unprocessed_within_retention",
    )


def _read_flags(record: dict[str, Any]) -> tuple[bool, bool, bool, bool] | None:
    values = [
        record.get("is_case_evidence"),
        record.get("has_sensitive_detection"),
        record.get("has_safe_detection"),
        record.get("is_latest_for_video"),
    ]
    if any(not isinstance(value, (bool, int)) or int(value) not in {0, 1} for value in values):
        return None
    return bool(values[0]), bool(values[1]), bool(values[2]), bool(values[3])


def _parse_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decision(
    record: dict[str, Any],
    *,
    action: CleanupAction,
    reason: str,
) -> CoverAssetCleanupDecision:
    return CoverAssetCleanupDecision(
        asset_id=str(record.get("asset_id") or "").strip(),
        storage_backend=str(record.get("storage_backend") or "").strip().lower(),
        storage_key=str(record.get("storage_key") or "").strip(),
        action=action,
        reason=reason,
        fetched_at=str(record.get("fetched_at") or "").strip(),
        byte_size=_parse_nonnegative_int(record.get("byte_size")) or 0,
    )
