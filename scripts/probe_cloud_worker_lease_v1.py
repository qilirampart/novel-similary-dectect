from __future__ import annotations

import argparse
import os
import shlex

import paramiko


DEFAULT_ENV_FILE = "/opt/novel-similarity-service/shared/novel-similarity.env"
DEFAULT_RELEASE_DIR = "/opt/novel-similarity-service/current"
DEFAULT_VENV_PYTHON = "/opt/novel-similarity-service/shared/venv/bin/python"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe cloud worker lease fencing with a disposable task.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--release-dir", default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--python-bin", default=DEFAULT_VENV_PYTHON)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def build_remote_source() -> str:
    return r'''
import json
import sqlite3
from uuid import uuid4

from api.config import SETTINGS
from service.business_store import (
    claim_next_queued_task_globally,
    create_compare_task,
    delete_compare_task,
    finish_task,
    get_compare_task,
    list_compare_task_items,
    recover_interrupted_tasks,
    replace_task_items,
    save_task_item_outcomes_batch,
    set_task_input_count,
)


db_path = SETTINGS.business_db_path
task_id = f"probe-worker-lease-{uuid4().hex}"
create_compare_task(
    db_path=db_path,
    task_id=task_id,
    detection_mode="rewrite",
    source_file_name="probe-worker-lease.txt",
    source_file_ext=".txt",
    source_file_path="runtime/probe-worker-lease.txt",
    source_file_sha256="probe-worker-lease",
    source_file_size=1,
    params={},
    created_by="codex-cloud-lease-probe",
)
replace_task_items(
    db_path=db_path,
    task_id=task_id,
    items=[{"item_order": 1, "source_ref": "probe", "query_text": "lease probe"}],
)
set_task_input_count(db_path, task_id, 1, "lease probe seeded")

claim_a = claim_next_queued_task_globally(db_path, worker_name="lease-probe-a")
if claim_a is None:
    raise SystemExit("worker A did not claim probe task")
token_a = str(claim_a["worker_lease_token"])

conn = sqlite3.connect(db_path, timeout=60)
try:
    conn.execute(
        "UPDATE compare_tasks SET last_heartbeat_at = '2000-01-01 00:00:00' WHERE task_id = ?",
        (task_id,),
    )
    conn.commit()
finally:
    conn.close()

recovery = recover_interrupted_tasks(db_path, stale_after_seconds=1)
claim_b = claim_next_queued_task_globally(db_path, worker_name="lease-probe-b")
if claim_b is None:
    raise SystemExit("worker B did not reclaim probe task")
token_b = str(claim_b["worker_lease_token"])

stale_result = {
    "item_order": 1,
    "semantic_status": "stale",
    "top1_book_name": "stale-worker-result",
    "top1_chapter_name": "",
    "top1_review_label": "",
    "top1_confidence_label": "",
    "top1_fine_score": 0.99,
    "result_payload": {"probe": "worker-a"},
}
save_task_item_outcomes_batch(db_path, task_id, successes=[stale_result], worker_lease_token=token_a)
finish_task(db_path, task_id, "completed", "stale worker attempted completion", worker_lease_token=token_a)
after_stale = list_compare_task_items(db_path, task_id, limit=10, offset=0)[0]
after_stale_task = get_compare_task(db_path, task_id)

fresh_result = {**stale_result, "semantic_status": "fresh", "top1_book_name": "fresh-worker-result"}
save_task_item_outcomes_batch(db_path, task_id, successes=[fresh_result], worker_lease_token=token_b)
finish_task(db_path, task_id, "completed", "fresh worker completed", worker_lease_token=token_b)
after_fresh = list_compare_task_items(db_path, task_id, limit=10, offset=0)[0]
after_fresh_task = get_compare_task(db_path, task_id)

ok = (
    int(recovery.get("running_to_queued", 0)) == 1
    and token_a != token_b
    and after_stale["status"] == "queued"
    and str(after_stale.get("top1_book_name") or "") == ""
    and after_stale_task is not None
    and after_stale_task["status"] == "running"
    and after_stale_task["counts"]["completed"] == 0
    and after_fresh["status"] == "completed"
    and after_fresh["top1_book_name"] == "fresh-worker-result"
    and after_fresh_task is not None
    and after_fresh_task["status"] == "completed"
    and after_fresh_task["counts"]["completed"] == 1
)
cleanup = delete_compare_task(db_path, task_id, reason="codex worker lease probe cleanup")
print(json.dumps({
    "ok": ok,
    "task_id": task_id,
    "recovery": recovery,
    "stale_item_status": after_stale["status"],
    "stale_task_status": None if after_stale_task is None else after_stale_task["status"],
    "fresh_item_status": after_fresh["status"],
    "fresh_book_name": after_fresh["top1_book_name"],
    "fresh_task_status": None if after_fresh_task is None else after_fresh_task["status"],
    "cleanup_deleted": None if cleanup is None else cleanup.get("is_deleted"),
}, ensure_ascii=False))
if not ok:
    raise SystemExit(2)
'''


def main() -> int:
    args = parse_args()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        username=args.user,
        password=require_password(args.password_env),
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    quoted_env = shlex.quote(args.env_file)
    quoted_release = shlex.quote(args.release_dir)
    quoted_python = shlex.quote(args.python_bin)
    command = (
        "bash -lc "
        + shlex.quote(
            "set -a; "
            f"source {quoted_env}; "
            "set +a; "
            f"cd {quoted_release}; "
            f"PYTHONPATH={quoted_release} {quoted_python} - <<'PY'\n"
            f"{build_remote_source()}\nPY"
        )
    )
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=120)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
