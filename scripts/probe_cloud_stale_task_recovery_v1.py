from __future__ import annotations

import argparse
import os
import shlex

import paramiko


DEFAULT_ENV_FILE = "/opt/novel-similarity-service/shared/novel-similarity.env"
DEFAULT_RELEASE_DIR = "/opt/novel-similarity-service/current"
DEFAULT_VENV_PYTHON = "/opt/novel-similarity-service/shared/venv/bin/python"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--release-dir", default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--python-bin", default=DEFAULT_VENV_PYTHON)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def build_remote_python(timeout_seconds: int, poll_seconds: float) -> str:
    return f'''import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from api.config import SETTINGS
from service.business_store import (
    create_compare_task,
    delete_compare_task,
    get_compare_task,
    list_compare_task_items,
    replace_task_items,
)

TIMEOUT_SECONDS = {int(timeout_seconds)}
POLL_SECONDS = {float(poll_seconds)}


def emit(event, **payload):
    print(json.dumps({{"event": event, **payload}}, ensure_ascii=False), flush=True)


task_id = f"probe-stale-recovery-{{uuid4().hex}}"
probe_file = Path(SETTINGS.task_upload_root) / "probe_stale_recovery.xlsx"
probe_file.parent.mkdir(parents=True, exist_ok=True)
if not probe_file.exists():
    probe_file.write_text("probe", encoding="utf-8")

task = create_compare_task(
    db_path=SETTINGS.business_db_path,
    task_id=task_id,
    detection_mode="rewrite",
    source_file_name=probe_file.name,
    source_file_ext=".xlsx",
    source_file_path=str(probe_file),
    source_file_sha256="probe-stale-recovery",
    source_file_size=probe_file.stat().st_size,
    params={{"candidate_display_score_threshold": SETTINGS.candidate_display_score_threshold}},
    created_by="codex-cloud-probe",
)
replace_task_items(
    db_path=SETTINGS.business_db_path,
    task_id=task_id,
    items=[
        {{
            "item_order": 1,
            "source_ref": "probe_row_1",
            "source_short_drama": "probe",
            "query_text": "stale task recovery probe text",
        }}
    ],
)

items = list_compare_task_items(SETTINGS.business_db_path, task_id, limit=10, offset=0)
if not items:
    raise SystemExit("probe task items were not created")

stale_margin_seconds = max(float(SETTINGS.task_recovery_stale_seconds) + 30.0, 180.0)
stale_ts = time.strftime(
    "%Y-%m-%d %H:%M:%S",
    time.localtime(time.time() - stale_margin_seconds),
)

conn = sqlite3.connect(SETTINGS.business_db_path, timeout=60)
try:
    conn.execute(
        """
        UPDATE compare_task_items
           SET status = 'running',
               started_at = ?,
               finished_at = NULL,
               duration_seconds = NULL,
               updated_at = ?
         WHERE task_id = ?
        """,
        (stale_ts, stale_ts, task_id),
    )
    conn.execute(
        """
        UPDATE compare_tasks
           SET status = 'pause_requested',
               accepted_input_count = 1,
               completed_input_count = 0,
               failed_input_count = 0,
               worker_name = 'probe-stale-worker',
               status_message = 'probe seeded stale pause_requested task',
               started_at = ?,
               finished_at = NULL,
               paused_at = NULL,
               updated_at = ?,
               last_heartbeat_at = ?
         WHERE task_id = ?
        """,
        (stale_ts, stale_ts, stale_ts, task_id),
    )
    conn.commit()
finally:
    conn.close()

emit(
    "seeded",
    task_id=task_id,
    stale_ts=stale_ts,
    recovery_stale_seconds=float(SETTINGS.task_recovery_stale_seconds),
    recovery_sweep_seconds=float(SETTINGS.task_recovery_sweep_seconds),
)

deadline = time.time() + max(TIMEOUT_SECONDS, 1)
recovered = False
last_snapshot = {{}}
while time.time() < deadline:
    current_task = get_compare_task(SETTINGS.business_db_path, task_id, include_deleted=True)
    current_items = list_compare_task_items(SETTINGS.business_db_path, task_id, limit=10, offset=0)
    item_statuses = [str(item.get("status") or "") for item in current_items]
    last_snapshot = {{
        "task_status": "" if current_task is None else str(current_task.get("status") or ""),
        "worker_name": "" if current_task is None else str(current_task.get("worker_name") or ""),
        "paused_at": None if current_task is None else current_task.get("paused_at"),
        "status_message": "" if current_task is None else str(current_task.get("status_message") or ""),
        "item_statuses": item_statuses,
    }}
    emit("poll", **last_snapshot)
    if (
        last_snapshot["task_status"] == "paused"
        and last_snapshot["worker_name"] == ""
        and last_snapshot["paused_at"]
        and item_statuses
        and all(status == "queued" for status in item_statuses)
    ):
        recovered = True
        break
    time.sleep(max(POLL_SECONDS, 0.2))

cleanup = None
if recovered:
    cleanup = delete_compare_task(
        SETTINGS.business_db_path,
        task_id,
        reason="codex stale task recovery probe cleanup",
    )

emit(
    "result",
    ok=recovered,
    task_id=task_id,
    final_snapshot=last_snapshot,
    cleanup_status=None if cleanup is None else cleanup.get("status"),
    cleanup_deleted=None if cleanup is None else cleanup.get("is_deleted"),
)

if not recovered:
    raise SystemExit(2)
'''


def build_remote_command(args: argparse.Namespace) -> str:
    python_source = build_remote_python(
        timeout_seconds=max(int(args.timeout_seconds), 1),
        poll_seconds=max(float(args.poll_seconds), 0.2),
    )
    quoted_env_file = shlex.quote(args.env_file)
    quoted_release_dir = shlex.quote(args.release_dir)
    quoted_python_bin = shlex.quote(args.python_bin)
    return (
        "bash -lc "
        + shlex.quote(
            "set -a; "
            f"source {quoted_env_file}; "
            "set +a; "
            f"cd {quoted_release_dir}; "
            f"PYTHONPATH={quoted_release_dir} {quoted_python_bin} - <<'PY'\n"
            f"{python_source}\n"
            "PY"
        )
    )


def main() -> int:
    args = parse_args()
    password = require_password(args.password_env)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        username=args.user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    command = build_remote_command(args)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=max(args.timeout_seconds + 30, 60))
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()

    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip())
    if exit_code != 0:
        raise SystemExit(exit_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
