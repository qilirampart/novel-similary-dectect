from __future__ import annotations

import argparse
import time
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import SETTINGS
from service.business_store import init_business_db
from service.task_executor import run_next_queued_task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one queued task and exit")
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="Sleep interval when queue is empty")
    parser.add_argument("--business-db", default=SETTINGS.business_db_path)
    parser.add_argument("--retrieval-db", default=SETTINGS.db_path)
    parser.add_argument("--export-root", default=SETTINGS.task_export_root)
    parser.add_argument("--worker-name", default=SETTINGS.task_worker_name)
    parser.add_argument("--item-parallelism", type=int, default=SETTINGS.task_item_parallelism)
    args = parser.parse_args()

    SETTINGS.ensure_runtime_dirs()
    init_business_db(args.business_db)
    while True:
        semantic_config = SETTINGS.build_semantic_config()
        task = run_next_queued_task(
            business_db_path=args.business_db,
            retrieval_db_path=args.retrieval_db,
            semantic_config=semantic_config,
            export_root=args.export_root,
            worker_name=args.worker_name,
            item_parallelism=args.item_parallelism,
        )
        if task is not None:
            print(f"OK processed_task={task['task_id']} status={task['status']}")
            if args.once:
                return
            continue
        print("OK no_queued_task")
        if args.once:
            return
        time.sleep(max(args.poll_seconds, 0.5))


if __name__ == "__main__":
    main()
