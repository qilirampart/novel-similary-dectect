from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import SETTINGS
from service.business_store import (
    backfill_compare_task_item_payload_store_batch,
    init_business_db,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill compare_task_item_payloads from legacy compare_task_items columns in controllable batches."
    )
    parser.add_argument(
        "--business-db",
        default=SETTINGS.business_db_path,
        help="Path to the business SQLite DB.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows to process per batch.",
    )
    parser.add_argument(
        "--mode",
        choices=("missing", "all"),
        default="missing",
        help="`missing` only fills rows absent from payload table; `all` replays every hot-table row.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Optional hard stop after N batches. `0` means run until complete.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    business_db = Path(args.business_db)
    batch_size = max(int(args.batch_size), 1)
    max_batches = max(int(args.max_batches), 0)

    init_business_db(business_db)

    batch_index = 0
    total_processed = 0
    cursor = 0
    last_result: dict[str, object] = {
        "remaining_rows": 0,
        "next_result_id": 0,
        "candidate_rows_before": 0,
        "candidate_rows_after": 0,
        "completed": True,
    }

    while True:
        if max_batches and batch_index >= max_batches:
            break
        batch_index += 1
        batch = backfill_compare_task_item_payload_store_batch(
            business_db,
            batch_size=batch_size,
            last_result_id=cursor,
            mode=args.mode,
        )
        total_processed += int(batch["processed_rows"] or 0)
        cursor = int(batch["next_result_id"] or cursor)
        last_result = batch
        print(
            "OK "
            f"batch={batch_index} "
            f"processed_rows={int(batch['processed_rows'])} "
            f"candidate_rows_before={int(batch['candidate_rows_before'])} "
            f"candidate_rows_after={int(batch['candidate_rows_after'])} "
            f"remaining_rows={int(batch['remaining_rows'])} "
            f"next_result_id={int(batch['next_result_id'])} "
            f"completed={1 if batch['completed'] else 0}"
        )
        if bool(batch["completed"]):
            break

    print(
        "OK "
        f"mode={args.mode} "
        f"batches={batch_index} "
        f"batch_size={batch_size} "
        f"total_processed={total_processed} "
        f"remaining_rows={int(last_result['remaining_rows'])} "
        f"next_result_id={int(last_result['next_result_id'])} "
        f"completed={1 if last_result['completed'] else 0}"
    )


if __name__ == "__main__":
    main()
