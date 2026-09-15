from __future__ import annotations

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


REMOTE_CODE = r"""
import json
import sqlite3
import sys

db_path = sys.argv[1]
with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row
    scalar = lambda sql: conn.execute(sql).fetchone()[0]
    imports = [dict(row) for row in conn.execute(
        "SELECT import_id, import_kind, status, source_file_name, sheet_name, stats_json, "
        "created_at, finished_at FROM cover_import_batches ORDER BY created_at"
    )]
    for item in imports:
        item["stats"] = json.loads(item.pop("stats_json") or "{}")
    run_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cover_runs)")
    }
    wanted_run_columns = [
        "run_id", "status", "status_message", "total_item_count",
        "completed_item_count", "failed_item_count", "params_json",
        "created_at", "updated_at",
    ]
    selected_run_columns = [
        name for name in wanted_run_columns if name in run_columns
    ]
    runs = [dict(row) for row in conn.execute(
        f"SELECT {', '.join(selected_run_columns)} FROM cover_runs "
        "ORDER BY created_at DESC LIMIT 10"
    )]
    for item in runs:
        item["params"] = json.loads(item.pop("params_json") or "{}")
    result = {
        "channels": scalar("SELECT COUNT(*) FROM cover_channels"),
        "videos": scalar("SELECT COUNT(*) FROM cover_videos"),
        "historical_observations": scalar("SELECT COUNT(*) FROM cover_historical_observations"),
        "detections": scalar("SELECT COUNT(*) FROM cover_detections"),
        "task_items": scalar("SELECT COUNT(*) FROM cover_task_items"),
        "task_reason_counts": [dict(row) for row in conn.execute(
            "SELECT reason, COUNT(*) AS count FROM cover_task_items GROUP BY reason ORDER BY reason"
        )],
        "imports": imports,
        "recent_runs": runs,
    }
print(json.dumps(result, ensure_ascii=False, indent=2))
"""


def main() -> int:
    remote = RemoteSession(RESOURCE_FILE)
    try:
        encoded = base64.b64encode(REMOTE_CODE.encode("utf-8")).decode("ascii")
        bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
        command = (
            f"{shlex.quote(REMOTE_PYTHON)} -c "
            f"{shlex.quote(bootstrap)} "
            f"{shlex.quote(REMOTE_DB)}"
        )
        print(remote.run(command, timeout=60))
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
