from __future__ import annotations

import argparse
import os
import posixpath
import shlex
from pathlib import Path

import paramiko


DEFAULT_ROOT = "/opt/novel-similarity-service"
DEFAULT_INPUT = f"{DEFAULT_ROOT}/shared/runtime/task_uploads/a706b063-2a3d-467c-b840-d8699b2505bf/source.csv"


REMOTE_RUNNER = r'''from __future__ import annotations
import csv
import json
import sys
import time
from pathlib import Path

from service.drama_subtitle_retrieval import search_drama_subtitle_lexical_candidates

input_path = Path(sys.argv[1])
db_path = sys.argv[2]
item_limit = int(sys.argv[3])
passes = int(sys.argv[4])
include_window_text = sys.argv[5] == "1"
rows = list(csv.DictReader(input_path.open(encoding="utf-8-sig", newline="")))[:item_limit]
results = []
for pass_number in range(1, passes + 1):
    for order, row in enumerate(rows, start=1):
        started_at = time.perf_counter()
        payload = search_drama_subtitle_lexical_candidates(
            db_path=db_path,
            query_text=row.get("query_text", ""),
            candidate_limit=20,
            window_limit=200,
            include_window_text=include_window_text,
        )
        candidates = payload.get("candidates") or []
        results.append({
            "pass": pass_number,
            "order": order,
            "duration_seconds": round(time.perf_counter() - started_at, 4),
            "query_chars": len(row.get("query_text", "")),
            "match_query_chars": len(payload.get("match_query") or ""),
            "window_hit_count": payload.get("window_hit_count", 0),
            "candidate_count": payload.get("candidate_count", 0),
            "top_candidates": [
                [candidate.get("book_id"), candidate.get("episode_order")]
                for candidate in candidates[:10]
            ],
        })
print(json.dumps(results, ensure_ascii=False))
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only lexical precheck profiler against the cloud subtitle SQLite database."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default=DEFAULT_ROOT)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument(
        "--local-input",
        default="",
        help="Optional local CSV to upload for profiling; it must contain a query_text column.",
    )
    parser.add_argument(
        "--db-path",
        default=f"{DEFAULT_ROOT}/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3",
    )
    parser.add_argument("--item-limit", type=int, default=3)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--include-window-text", action="store_true")
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.item_limit <= 0 or args.passes <= 0:
        raise SystemExit("item-limit and passes must be > 0")
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")

    root = args.remote_root.rstrip("/")
    remote_dir = f"{root}/shared/runtime/subtitle_lexical_profile"
    remote_runner = f"{remote_dir}/run_profile.py"
    remote_input = args.input
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        sftp = client.open_sftp()
        try:
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")[1].channel.recv_exit_status()
            with sftp.file(remote_runner, "w") as handle:
                handle.write(REMOTE_RUNNER)
            if args.local_input:
                local_input = Path(args.local_input).resolve()
                if not local_input.is_file():
                    raise FileNotFoundError(f"local input not found: {local_input}")
                remote_input = f"{remote_dir}/uploaded_input.csv"
                sftp.put(str(local_input), remote_input)
        finally:
            sftp.close()

        command = (
            f"cd {shlex.quote(posixpath.join(root, 'current'))} && "
            f"PYTHONPATH={shlex.quote(posixpath.join(root, 'current'))} "
            f"{shlex.quote(posixpath.join(root, 'shared', 'venv', 'bin', 'python'))} "
            f"{shlex.quote(remote_runner)} {shlex.quote(remote_input)} {shlex.quote(args.db_path)} "
            f"{args.item_limit} {args.passes} {'1' if args.include_window_text else '0'}"
        )
        _, stdout, stderr = client.exec_command(command, timeout=1800)
        status = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace").strip()
        errors = stderr.read().decode("utf-8", errors="replace").strip()
        if status:
            raise RuntimeError(f"Remote profiler failed ({status}): {errors or output}")
        output_path = Path(args.out).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(output_path)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
