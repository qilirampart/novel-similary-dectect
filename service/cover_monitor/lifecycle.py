from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import re
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


@dataclass(frozen=True)
class CoverStagingCleanupDecision:
    relative_path: str
    action: CleanupAction
    reason: str
    modified_at: str
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


def build_staging_cleanup_plan(
    staging_root: str | Path,
    *,
    now: datetime | None = None,
    stale_hours: int = 24,
) -> list[CoverStagingCleanupDecision]:
    if int(stale_hours) <= 0:
        raise ValueError("stale_hours 必须大于 0")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    effective_now = effective_now.astimezone(timezone.utc)
    root = Path(staging_root).resolve()
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("staging_root 必须是目录")
    decisions: list[CoverStagingCleanupDecision] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            decisions.append(_staging_decision(relative, reason="unrecognized_staging_file"))
            continue
        try:
            stat = path.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        except OSError:
            decisions.append(_staging_decision(relative, reason="invalid_metadata"))
            continue
        recognized = _is_recognized_staging_path(relative)
        expired = modified_at < effective_now - timedelta(hours=int(stale_hours))
        if not recognized:
            action: CleanupAction = "keep"
            reason = "unrecognized_staging_file"
        else:
            action = "delete_candidate" if expired else "keep"
            reason = "staging_retention_expired" if expired else "staging_within_retention"
        decisions.append(
            CoverStagingCleanupDecision(
                relative_path=relative,
                action=action,
                reason=reason,
                modified_at=modified_at.isoformat(timespec="seconds"),
                byte_size=max(int(stat.st_size), 0),
            )
        )
    return decisions


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
_STAGING_FILE = re.compile(
    r"^[a-f0-9]{64}\.(?:jpg|png|webp)(?:\.part-[a-f0-9]{32})?$"
)


def _is_recognized_staging_path(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return len(parts) == 2 and bool(_VIDEO_ID.fullmatch(parts[0])) and bool(
        _STAGING_FILE.fullmatch(parts[1])
    )


def _staging_decision(relative_path: str, *, reason: str) -> CoverStagingCleanupDecision:
    return CoverStagingCleanupDecision(
        relative_path=relative_path,
        action="keep",
        reason=reason,
        modified_at="",
        byte_size=0,
    )


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
