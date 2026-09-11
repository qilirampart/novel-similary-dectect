from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source-ref", action="append", dest="source_refs", required=True)
    parser.add_argument("--mode", choices=("sequential", "concurrent", "both"), default="both")
    parser.add_argument("--scenario", choices=("fresh2", "resume26", "both"), default="both")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--release-dir", default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--python-bin", default=DEFAULT_VENV_PYTHON)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def build_remote_python(task_id: str, source_refs: list[str], mode: str, scenario: str) -> str:
    return f"""import json
import os
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from api.config import SETTINGS
from service.business_store import (
    create_compare_task,
    get_compare_task,
    init_business_db,
    replace_task_items,
)
import service.task_executor as task_executor

SOURCE_TASK_ID = {json.dumps(task_id, ensure_ascii=False)}
SOURCE_REFS = {json.dumps(source_refs, ensure_ascii=False)}
MODE = {json.dumps(mode, ensure_ascii=False)}
SCENARIO = {json.dumps(scenario, ensure_ascii=False)}


def emit(event, **payload):
    print(json.dumps({{"event": event, **payload}}, ensure_ascii=False), flush=True)


def load_inputs():
    conn = sqlite3.connect(SETTINGS.business_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in SOURCE_REFS)
        rows = conn.execute(
            f\"\"\"
            SELECT i.item_order,
                   i.source_ref,
                   COALESCE(NULLIF(p.query_text, ''), i.query_text) AS query_text,
                   i.source_short_drama,
                   i.source_novel_name,
                   i.source_excel_row,
                   i.source_episode,
                   i.source_author,
                   i.source_platform,
                   i.source_display_title,
                   i.source_description
              FROM compare_task_items i
              LEFT JOIN compare_task_item_payloads p
                ON p.result_id = i.result_id
             WHERE i.task_id = ?
               AND i.source_ref IN ({{placeholders}})
             ORDER BY i.item_order ASC
            \"\"\",
            [SOURCE_TASK_ID, *SOURCE_REFS],
        ).fetchall()
    finally:
        conn.close()
    return [
        {{
            "item_order": int(row["item_order"]),
            "source_ref": str(row["source_ref"]),
            "query_text": str(row["query_text"] or ""),
            "source_short_drama": str(row["source_short_drama"] or ""),
            "source_novel_name": str(row["source_novel_name"] or ""),
            "source_excel_row": str(row["source_excel_row"] or ""),
            "source_episode": str(row["source_episode"] or ""),
            "source_author": str(row["source_author"] or ""),
            "source_platform": str(row["source_platform"] or ""),
            "source_display_title": str(row["source_display_title"] or ""),
            "source_description": str(row["source_description"] or ""),
        }}
        for row in rows
    ]


def load_full_source_rows():
    conn = sqlite3.connect(SETTINGS.business_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            \"\"\"
            SELECT i.item_order,
                   i.source_ref,
                   COALESCE(NULLIF(p.query_text, ''), i.query_text) AS query_text,
                   i.source_short_drama,
                   i.source_novel_name,
                   i.source_excel_row,
                   i.source_episode,
                   i.source_author,
                   i.source_platform,
                   i.source_display_title,
                   i.source_description,
                   i.status,
                   i.started_at,
                   i.finished_at,
                   i.duration_seconds,
                   i.semantic_status,
                   i.top1_book_name,
                   i.top1_chapter_name,
                   i.top1_review_label,
                   i.top1_confidence_label,
                   i.top1_fine_score,
                   i.error_message,
                   i.created_at,
                   i.updated_at,
                   p.result_payload_json
              FROM compare_task_items i
              LEFT JOIN compare_task_item_payloads p
                ON p.result_id = i.result_id
             WHERE i.task_id = ?
             ORDER BY i.item_order ASC
            \"\"\",
            [SOURCE_TASK_ID],
        ).fetchall()
    finally:
        conn.close()
    payload = []
    for row in rows:
        payload.append(
            {{
                "item_order": int(row["item_order"]),
                "source_ref": str(row["source_ref"]),
                "query_text": str(row["query_text"] or ""),
                "source_short_drama": str(row["source_short_drama"] or ""),
                "source_novel_name": str(row["source_novel_name"] or ""),
                "source_excel_row": str(row["source_excel_row"] or ""),
                "source_episode": str(row["source_episode"] or ""),
                "source_author": str(row["source_author"] or ""),
                "source_platform": str(row["source_platform"] or ""),
                "source_display_title": str(row["source_display_title"] or ""),
                "source_description": str(row["source_description"] or ""),
                "status": str(row["status"] or ""),
                "started_at": str(row["started_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
                "duration_seconds": row["duration_seconds"],
                "semantic_status": str(row["semantic_status"] or ""),
                "top1_book_name": str(row["top1_book_name"] or ""),
                "top1_chapter_name": str(row["top1_chapter_name"] or ""),
                "top1_review_label": str(row["top1_review_label"] or ""),
                "top1_confidence_label": str(row["top1_confidence_label"] or ""),
                "top1_fine_score": row["top1_fine_score"],
                "error_message": str(row["error_message"] or ""),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "result_payload_json": None if row["result_payload_json"] is None else str(row["result_payload_json"]),
            }}
        )
    return payload


def make_wrapped(name, fn):
    def wrapped(*args, **kwargs):
        thread_name = threading.current_thread().name
        started = time.perf_counter()
        emit("db_call_start", name=name, thread=thread_name)
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed = round(time.perf_counter() - started, 4)
            emit("db_call_end", name=name, thread=thread_name, seconds=elapsed)
    return wrapped


def install_wrappers():
    task_executor.mark_task_items_running = make_wrapped("mark_task_items_running", task_executor.mark_task_items_running)
    task_executor.save_task_item_outcomes_batch = make_wrapped("save_task_item_outcomes_batch", task_executor.save_task_item_outcomes_batch)
    task_executor.get_compare_task_runtime_state = make_wrapped("get_compare_task_runtime_state", task_executor.get_compare_task_runtime_state)
    task_executor.update_task_progress = make_wrapped("update_task_progress", task_executor.update_task_progress)
    task_executor.finish_task = make_wrapped("finish_task", task_executor.finish_task)


def create_temp_task(temp_db_path, export_root, items, label):
    task_id = str(uuid4())
    create_compare_task(
        db_path=temp_db_path,
        task_id=task_id,
        detection_mode="rewrite",
        source_file_name=f"probe_{{label}}.xlsx",
        source_file_ext=".xlsx",
        source_file_path=str(Path(export_root) / f"missing_{{label}}.xlsx"),
        source_file_sha256="probe",
        source_file_size=0,
        params={{"candidate_display_score_threshold": SETTINGS.candidate_display_score_threshold}},
        created_by="cloud-probe",
    )
    replace_task_items(
        db_path=temp_db_path,
        task_id=task_id,
        items=items,
    )
    return task_id


def create_resume_state_task(temp_db_path, export_root, source_rows, label):
    task_id = create_temp_task(temp_db_path, export_root, source_rows, label)
    running_orders = {{21, 23}}
    queued_orders = {{22, 24, 25}}
    conn = sqlite3.connect(temp_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        temp_rows = conn.execute(
            "SELECT result_id, item_order, created_at FROM compare_task_items WHERE task_id = ? ORDER BY item_order ASC",
            (task_id,),
        ).fetchall()
        temp_by_order = {{
            int(row["item_order"]): {{
                "result_id": int(row["result_id"]),
                "created_at": str(row["created_at"] or now),
            }}
            for row in temp_rows
        }}
        completed_source_rows = [
            row
            for row in source_rows
            if int(row["item_order"]) not in running_orders and int(row["item_order"]) not in queued_orders
        ]
        for row in completed_source_rows:
            item_order = int(row["item_order"])
            temp_result = temp_by_order[item_order]
            conn.execute(
                \"\"\"
                UPDATE compare_task_items
                   SET status = 'completed',
                       started_at = ?,
                       finished_at = ?,
                       duration_seconds = ?,
                       semantic_status = ?,
                       top1_book_name = ?,
                       top1_chapter_name = ?,
                       top1_review_label = ?,
                       top1_confidence_label = ?,
                       top1_fine_score = ?,
                       result_payload_json = '',
                       error_message = '',
                       updated_at = ?
                 WHERE task_id = ?
                   AND item_order = ?
                \"\"\",
                (
                    row["started_at"] or now,
                    row["finished_at"] or now,
                    row["duration_seconds"],
                    row["semantic_status"],
                    row["top1_book_name"],
                    row["top1_chapter_name"],
                    row["top1_review_label"],
                    row["top1_confidence_label"],
                    row["top1_fine_score"],
                    row["updated_at"] or now,
                    task_id,
                    item_order,
                ),
            )
            conn.execute(
                \"\"\"
                INSERT INTO compare_task_item_payloads (
                    result_id,
                    query_text,
                    result_payload_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(result_id) DO UPDATE SET
                    query_text = excluded.query_text,
                    result_payload_json = excluded.result_payload_json,
                    updated_at = excluded.updated_at
                \"\"\",
                (
                    temp_result["result_id"],
                    row["query_text"],
                    row["result_payload_json"],
                    temp_result["created_at"],
                    row["updated_at"] or now,
                ),
            )
        for item_order in running_orders:
            conn.execute(
                \"\"\"
                UPDATE compare_task_items
                   SET status = 'running',
                       started_at = ?,
                       finished_at = NULL,
                       duration_seconds = NULL,
                       updated_at = ?
                 WHERE task_id = ?
                   AND item_order = ?
                \"\"\",
                (now, now, task_id, item_order),
            )
        for item_order in queued_orders:
            conn.execute(
                \"\"\"
                UPDATE compare_task_items
                   SET status = 'queued',
                       started_at = NULL,
                       finished_at = NULL,
                       duration_seconds = NULL,
                       updated_at = ?
                 WHERE task_id = ?
                   AND item_order = ?
                \"\"\",
                (now, task_id, item_order),
            )
        conn.execute(
            \"\"\"
            UPDATE compare_tasks
               SET status = 'running',
                   accepted_input_count = 26,
                   completed_input_count = 21,
                   failed_input_count = 0,
                   started_at = ?,
                   finished_at = NULL,
                   worker_name = 'resume-probe',
                   status_message = 'Task claimed by worker. Parsing input file.',
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            \"\"\",
            (now, now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def run_one_task(temp_db_path, export_root, task_id, label):
    semantic_config = SETTINGS.build_semantic_config()
    started = time.perf_counter()
    emit("task_start", task_id=task_id, label=label, thread=threading.current_thread().name)
    result = task_executor.execute_claimed_task(
        task_id=task_id,
        business_db_path=temp_db_path,
        retrieval_db_path=SETTINGS.db_path,
        semantic_config=semantic_config,
        export_root=export_root,
        item_parallelism=2,
    )
    elapsed = round(time.perf_counter() - started, 4)
    refreshed = get_compare_task(temp_db_path, task_id)
    emit(
        "task_end",
        task_id=task_id,
        label=label,
        thread=threading.current_thread().name,
        seconds=elapsed,
        status=(refreshed or result).get("status"),
        counts=(refreshed or result).get("counts"),
    )
    return {{
        "task_id": task_id,
        "label": label,
        "seconds": elapsed,
        "status": (refreshed or result).get("status"),
        "counts": (refreshed or result).get("counts"),
    }}


inputs = load_inputs()
emit("loaded_inputs", count=len(inputs), source_refs=[item["source_ref"] for item in inputs])
if len(inputs) != len(SOURCE_REFS):
    raise SystemExit(f"Expected {{len(SOURCE_REFS)}} inputs, got {{len(inputs)}}")
full_source_rows = load_full_source_rows()
emit("loaded_full_source_rows", count=len(full_source_rows))

temp_root = Path(tempfile.mkdtemp(prefix="novel-similarity-probe-"))
temp_db_path = str(temp_root / "business.sqlite3")
export_root = str(temp_root / "exports")
Path(export_root).mkdir(parents=True, exist_ok=True)
init_business_db(temp_db_path)
install_wrappers()
emit("temp_paths", temp_root=str(temp_root), temp_db_path=temp_db_path, export_root=export_root)

summary = {{"mode": MODE, "scenario": SCENARIO, "results": {{}}}}

def run_scenario(task_factory, result_key, label_prefix):
    if MODE in ("sequential", "both"):
        seq_task_id = task_factory(f"{{label_prefix}}_seq")
        seq_started = time.perf_counter()
        summary["results"].setdefault(result_key, {{}})["sequential"] = [
            run_one_task(temp_db_path, export_root, seq_task_id, f"{{label_prefix}}_seq")
        ]
        summary[f"{{result_key}}_sequential_wall_seconds"] = round(time.perf_counter() - seq_started, 4)
    if MODE in ("concurrent", "both"):
        concurrent_pairs = [
            (task_factory(f"{{label_prefix}}_concurrent_a"), f"{{label_prefix}}_concurrent_a"),
            (task_factory(f"{{label_prefix}}_concurrent_b"), f"{{label_prefix}}_concurrent_b"),
        ]
        conc_started = time.perf_counter()
        conc_results = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"executor-{{label_prefix}}") as executor:
            futures = [
                executor.submit(run_one_task, temp_db_path, export_root, task_id, label)
                for task_id, label in concurrent_pairs
            ]
            for future in as_completed(futures):
                conc_results.append(future.result())
        conc_results.sort(key=lambda item: item["label"])
        summary["results"].setdefault(result_key, {{}})["concurrent"] = conc_results
        summary[f"{{result_key}}_concurrent_wall_seconds"] = round(time.perf_counter() - conc_started, 4)

if SCENARIO in ("fresh2", "both"):
    run_scenario(
        task_factory=lambda label: create_temp_task(temp_db_path, export_root, inputs, label),
        result_key="fresh2",
        label_prefix="fresh2",
    )

if SCENARIO in ("resume26", "both"):
    run_scenario(
        task_factory=lambda label: create_resume_state_task(temp_db_path, export_root, full_source_rows, label),
        result_key="resume26",
        label_prefix="resume26",
    )

emit("final_summary", **summary)
"""


def build_remote_command(args: argparse.Namespace) -> str:
    python_source = build_remote_python(
        task_id=args.task_id,
        source_refs=list(args.source_refs),
        mode=args.mode,
        scenario=args.scenario,
    )
    quoted_env_file = shlex.quote(args.env_file)
    quoted_release_dir = shlex.quote(args.release_dir)
    quoted_python_bin = shlex.quote(args.python_bin)
    quoted_timeout = shlex.quote(str(max(int(args.timeout_seconds), 1)))
    return (
        "bash -lc "
        + shlex.quote(
            "set -a; "
            f"source {quoted_env_file}; "
            "set +a; "
            f"cd {quoted_release_dir}; "
            f"/usr/bin/timeout {quoted_timeout}s {quoted_python_bin} - <<'PY'\n"
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
