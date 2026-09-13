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

from service.cover_monitor.lifecycle import build_staging_cleanup_plan
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a dry-run cover staging cleanup manifest")
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--output", required=True, help="Output CSV manifest path")
    parser.add_argument("--stale-hours", type=int, default=24)
    parser.add_argument("--db", help="Cover monitor SQLite database path for audit registration")
    parser.add_argument("--workspace", default="internal")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument(
        "--register-audit",
        action="store_true",
        help="Register the generated manifest as a planned cleanup run; requires --db",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    decisions = build_staging_cleanup_plan(
        args.staging_root,
        stale_hours=args.stale_hours,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["relative_path", "action", "reason", "modified_at", "byte_size"]
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(decision) for decision in decisions)
    candidates = [item for item in decisions if item.action == "delete_candidate"]
    cleanup_run_id = None
    if args.register_audit:
        if not args.db:
            raise ValueError("--register-audit requires --db")
        store = CoverMonitorStore(args.db)
        scope = CoverAccessScope(workspace_key=args.workspace, user_id=args.user_id)
        cleanup = store.register_cleanup_plan(
            scope,
            cleanup_kind="staging",
            manifest_path=str(output.resolve()),
            manifest_sha256=sha256(output.read_bytes()).hexdigest(),
            policy={
                "staging_root": str(Path(args.staging_root).resolve()),
                "stale_hours": args.stale_hours,
            },
            total_count=len(decisions),
            candidate_items=[
                {
                    "item_key": item.relative_path,
                    "reason": item.reason,
                    "byte_size": item.byte_size,
                }
                for item in candidates
            ],
        )
        cleanup_run_id = cleanup["cleanup_run_id"]
    print(
        json.dumps(
            {
                "mode": "dry_run",
                "staging_root": str(Path(args.staging_root).resolve()),
                "total_files": len(decisions),
                "delete_candidate_count": len(candidates),
                "delete_candidate_bytes": sum(item.byte_size for item in candidates),
                "manifest": str(output.resolve()),
                "cleanup_run_id": cleanup_run_id,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
