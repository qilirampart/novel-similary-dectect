from __future__ import annotations

import argparse
import os

import paramiko


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only status of the cloud language-aware subtitle FTS V2 build.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default="/opt/novel-similarity-service")
    parser.add_argument("--target-db", default="")
    return parser.parse_args()


def run(client: paramiko.SSHClient, command: str) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=60)
    status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out if status == 0 else f"[exit={status}] {err or out}"


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    root = args.remote_root.rstrip("/")
    state_dir = f"{root}/shared/runtime/language_fts_v2_build"
    db = args.target_db or f"{root}/shared/data/drama_subtitle_similarity_v1_20260902_lang_v2.sqlite3"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        commands = [
            ("process", f"if [ -f {state_dir}/build.pid ]; then PID=$(cat {state_dir}/build.pid); ps -p $PID -o pid,stat,%cpu,%mem,etime,args; else echo no_pid_file; fi"),
            ("storage", f"df -h {root}/shared/data; ls -lh {db} 2>/dev/null || true"),
            ("metadata", f"sqlite3 {db} \"select index_name,source_window_count,indexed_window_count,build_status,updated_at from drama_subtitle_lexical_index_metadata;\" 2>/dev/null || true"),
            ("log_tail", f"tail -n 20 {state_dir}/build.log 2>/dev/null || true"),
        ]
        for label, command in commands:
            print(f"===== {label} =====")
            print(run(client, command))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
