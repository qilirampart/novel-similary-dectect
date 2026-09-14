from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from service.cover_monitor.cleanup_executor import (
    StagingCleanupExecutionResult,
    execute_staging_cleanup,
)
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


EXECUTION_TOKEN_ENV = "COVER_CLEANUP_EXECUTION_TOKEN"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute one approved staging-only cover cleanup plan.",
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--cleanup-run-id", required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument(
        "--confirm-cleanup-run-id",
        required=True,
        help="Must exactly match --cleanup-run-id.",
    )
    parser.add_argument("--max-items", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required destructive-action confirmation.",
    )
    return parser.parse_args(argv)


def run(
    args: argparse.Namespace,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    if args.execute is not True:
        raise ValueError("--execute is required")
    cleanup_run_id = str(args.cleanup_run_id).strip()
    if str(args.confirm_cleanup_run_id).strip() != cleanup_run_id:
        raise ValueError("cleanup run id confirmation does not match")
    execution_token = str(environ.get(EXECUTION_TOKEN_ENV) or "").strip()
    if not execution_token:
        raise ValueError(f"{EXECUTION_TOKEN_ENV} is required")
    max_items = _positive_limit(args.max_items, "max_items")
    max_bytes = _positive_limit(args.max_bytes, "max_bytes")

    scope = CoverAccessScope(
        workspace_key=str(args.workspace_key).strip(),
        user_id=int(args.user_id),
    )
    store = CoverMonitorStore(args.db_path)
    plan = store.get_cleanup_plan(scope, cleanup_run_id)
    if plan is None:
        raise ValueError("cleanup plan not found")
    if str(plan.get("cleanup_kind")) != "staging":
        raise ValueError("only staging cleanup execution is supported")
    if str(plan.get("status")) != "approved":
        raise ValueError("cleanup plan is not approved")

    candidate_count, candidate_bytes = _candidate_totals(plan)
    if candidate_count > max_items:
        raise ValueError(
            f"cleanup item safety limit exceeded: {candidate_count} > {max_items}"
        )
    if candidate_bytes > max_bytes:
        raise ValueError(
            f"cleanup byte safety limit exceeded: {candidate_bytes} > {max_bytes}"
        )

    result = execute_staging_cleanup(
        store,
        scope,
        cleanup_run_id,
        execution_token=execution_token,
        staging_root=args.staging_root,
        confirm_delete=True,
        worker_name="cover-staging-cleanup-cli",
    )
    return {
        "cleanup_run_id": result.cleanup_run_id,
        "status": result.status,
        "candidate_count": candidate_count,
        "candidate_bytes": candidate_bytes,
        "deleted_count": result.deleted_count,
        "skipped_count": result.skipped_count,
        "failed_count": result.failed_count,
    }


def _positive_limit(raw_value: object, name: str) -> int:
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _candidate_totals(plan: dict[str, object]) -> tuple[int, int]:
    raw_items = plan.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("cleanup plan items are invalid")
    total_bytes = 0
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("cleanup plan item is invalid")
        try:
            byte_size = int(item.get("byte_size", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("cleanup plan item byte size is invalid") from exc
        if byte_size < 0:
            raise ValueError("cleanup plan item byte size is invalid")
        total_bytes += byte_size
    return len(raw_items), total_bytes


def main(argv: Sequence[str] | None = None) -> int:
    report = run(parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
