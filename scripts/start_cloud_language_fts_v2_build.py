from __future__ import annotations

import argparse
import os
import shlex

import paramiko


DEFAULT_REMOTE_ROOT = "/opt/novel-similarity-service"
DEFAULT_SOURCE_DB = f"{DEFAULT_REMOTE_ROOT}/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3"
DEFAULT_TARGET_DB = f"{DEFAULT_REMOTE_ROOT}/shared/data/drama_subtitle_similarity_v1_20260902_lang_v2.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start a low-priority cloud build of language-aware subtitle FTS V2.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    parser.add_argument("--target-db", default=DEFAULT_TARGET_DB)
    return parser.parse_args()


def run(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if status:
        raise RuntimeError(f"remote command failed ({status}): {err or out}")
    return out


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    remote_root = args.remote_root.rstrip("/")
    source_db = args.source_db
    target_db = args.target_db
    log_dir = f"{remote_root}/shared/runtime/language_fts_v2_build"
    log_path = f"{log_dir}/build.log"
    pid_path = f"{log_dir}/build.pid"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        q = shlex.quote
        status = run(
            client,
            f"mkdir -p {q(log_dir)}; "
            f"test -f {q(source_db)}; "
            f"df -h {q(remote_root)}/shared/data; "
            f"if [ -f {q(pid_path)} ] && kill -0 $(cat {q(pid_path)}) 2>/dev/null; then "
            f"echo already_running=$(cat {q(pid_path)}); exit 0; fi; "
            f"if [ ! -f {q(target_db)} ]; then cp --reflink=auto --sparse=always {q(source_db)} {q(target_db)}; fi; "
            f"rm -f {q(pid_path)}; "
            f"nohup nice -n 15 ionice -c2 -n7 {q(remote_root)}/shared/venv/bin/python "
            f"{q(remote_root)}/current/scripts/build_drama_subtitle_lexical_index_v1.py "
            f"--db {q(target_db)} "
            f"--base-schema {q(remote_root)}/current/service/drama_subtitle_schema_v1.sql "
            f"--window-schema {q(remote_root)}/current/service/drama_subtitle_windows_schema_v1.sql "
            f"--fts-schema {q(remote_root)}/current/service/drama_subtitle_lexical_schema_v1.sql "
            f"--output-root {q(log_dir)}/output --build-language-v2 "
            f"> {q(log_path)} 2>&1 < /dev/null & echo $! > {q(pid_path)}; "
            f"echo started_pid=$(cat {q(pid_path)}); echo log_path={q(log_path)}",
            timeout=180,
        )
        print(status)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
