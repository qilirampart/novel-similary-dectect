from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.cover_monitor.lifecycle import build_staging_cleanup_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a dry-run cover staging cleanup manifest")
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--output", required=True, help="Output CSV manifest path")
    parser.add_argument("--stale-hours", type=int, default=24)
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
    print(
        json.dumps(
            {
                "mode": "dry_run",
                "staging_root": str(Path(args.staging_root).resolve()),
                "total_files": len(decisions),
                "delete_candidate_count": len(candidates),
                "delete_candidate_bytes": sum(item.byte_size for item in candidates),
                "manifest": str(output.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
