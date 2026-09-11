from __future__ import annotations

import argparse
import os

import paramiko


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only cloud runtime inspection for subtitle retrieval performance.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    return parser.parse_args()


def run(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if status:
        return f"[exit={status}] {err or out}"
    return out or err


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    commands = [
        ("host", "printf 'cores='; nproc; free -h; uptime"),
        ("storage", "df -h /opt/novel-similarity-service/shared/data /opt/novel-similarity-qdrant 2>/dev/null"),
        ("service_process", "ps -C python -o pid,%cpu,%mem,rss,etime,args --sort=-%cpu | grep 'uvicorn api.app' || true"),
        ("qdrant_process", "ps -C qdrant -o pid,%cpu,%mem,rss,etime,args --sort=-%cpu || true"),
        (
            "subtitle_sqlite",
            "DB=/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3; "
            "sqlite3 \"$DB\" \"pragma page_count; pragma page_size; pragma cache_size; pragma journal_mode; "
            "select 'windows=' || count(*) from drama_subtitle_windows; "
            "select 'lines=' || count(*) from drama_subtitle_lines;\"",
        ),
        (
            "window_indexes",
            "DB=/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3; "
            "sqlite3 \"$DB\" \"pragma index_list('drama_subtitle_windows'); pragma index_list('drama_subtitle_lines');\"",
        ),
        (
            "qdrant_collection",
            "curl -sS --max-time 20 http://127.0.0.1:6333/collections/drama_subtitle_window_embeddings_qwen3_4b_2560_v1",
        ),
    ]
    try:
        for label, command in commands:
            print(f"===== {label} =====")
            print(run(client, command, timeout=90))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
