from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.cover_monitor.lifecycle import CoverRetentionPolicy, build_asset_cleanup_plan
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a dry-run cover asset cleanup manifest")
    parser.add_argument("--db", required=True, help="Cover monitor SQLite database path")
    parser.add_argument("--output", required=True, help="Output CSV manifest path")
    parser.add_argument("--workspace", default="internal")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--safe-days", type=int, default=60)
    parser.add_argument("--unprocessed-days", type=int, default=14)
    parser.add_argument(
        "--register-audit",
        action="store_true",
        help="Register the generated manifest as a planned cleanup run",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = CoverMonitorStore(args.db)
    scope = CoverAccessScope(workspace_key=args.workspace, user_id=args.user_id)
    policy = CoverRetentionPolicy(
        safe_days=args.safe_days,
        unprocessed_days=args.unprocessed_days,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    candidate_count = 0
    candidate_bytes = 0
    candidate_items: list[dict[str, object]] = []
    offset = 0
    fieldnames = [
        "asset_id",
        "storage_backend",
        "storage_key",
        "action",
        "reason",
        "fetched_at",
        "byte_size",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        while True:
            page = store.list_asset_lifecycle_records(scope, limit=1000, offset=offset)
            records = page["items"]
            if not records:
                break
            for decision in build_asset_cleanup_plan(records, policy=policy):
                writer.writerow(asdict(decision))
                total += 1
                if decision.action == "delete_candidate":
                    candidate_count += 1
                    candidate_bytes += decision.byte_size
                    candidate_items.append(
                        {
                            "item_key": decision.asset_id,
                            "reason": decision.reason,
                            "byte_size": decision.byte_size,
                        }
                    )
            offset += len(records)

    cleanup_run_id = None
    if args.register_audit:
        manifest_sha256 = sha256(output.read_bytes()).hexdigest()
        cleanup = store.register_cleanup_plan(
            scope,
            cleanup_kind="asset",
            manifest_path=str(output.resolve()),
            manifest_sha256=manifest_sha256,
            policy={
                "safe_days": policy.safe_days,
                "unprocessed_days": policy.unprocessed_days,
            },
            total_count=total,
            candidate_items=candidate_items,
        )
        cleanup_run_id = cleanup["cleanup_run_id"]

    print(
        json.dumps(
            {
                "mode": "dry_run",
                "workspace": args.workspace,
                "total_assets": total,
                "delete_candidate_count": candidate_count,
                "delete_candidate_bytes": candidate_bytes,
                "manifest": str(output.resolve()),
                "cleanup_run_id": cleanup_run_id,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
