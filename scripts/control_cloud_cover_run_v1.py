from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shlex

from install_cloud_cover_worker_v1 import RemoteSession


ROOT_DIR = Path(__file__).resolve().parents[1]
RESOURCE_FILE = ROOT_DIR / ".codex" / "测试环境资源清单.md"
REMOTE_ROOT = "/opt/novel-similarity-service"
REMOTE_DB = f"{REMOTE_ROOT}/shared/runtime/cover_monitor/cover_monitor_v1.sqlite3"
REMOTE_PYTHON = f"{REMOTE_ROOT}/shared/venv/bin/python"
REMOTE_CURRENT = f"{REMOTE_ROOT}/current"


REMOTE_CODE = r"""
import json
import sqlite3
import sys

current_root, db_path, run_id, action = sys.argv[1:]
sys.path.insert(0, current_root)
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore

with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT workspace_key, created_by_user_id FROM cover_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
if row is None:
    raise LookupError("cover run not found")

store = CoverMonitorStore(db_path)
scope = CoverAccessScope(
    workspace_key=str(row["workspace_key"]),
    user_id=int(row["created_by_user_id"]),
)
if action == "pause":
    result = store.request_pause(scope, run_id)
elif action == "resume":
    result = store.resume_run(scope, run_id)
elif action == "cancel":
    result = store.request_cancel(scope, run_id)
else:
    raise ValueError("unsupported action")
print(json.dumps({
    "run_id": result["run_id"],
    "status": result["status"],
    "status_message": result.get("status_message"),
    "completed_item_count": result["completed_item_count"],
    "failed_item_count": result["failed_item_count"],
    "total_item_count": result["total_item_count"],
}, ensure_ascii=False, indent=2))
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control one cloud cover-monitor run.")
    parser.add_argument("run_id")
    parser.add_argument("action", choices=("pause", "resume", "cancel"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remote = RemoteSession(RESOURCE_FILE)
    try:
        encoded = base64.b64encode(REMOTE_CODE.encode("utf-8")).decode("ascii")
        bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
        command = " ".join(
            shlex.quote(value)
            for value in (
                REMOTE_PYTHON,
                "-c",
                bootstrap,
                REMOTE_CURRENT,
                REMOTE_DB,
                args.run_id,
                args.action,
            )
        )
        print(remote.run(command, timeout=60))
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
