from __future__ import annotations

import argparse
import os

import paramiko


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely stop only the cloud language FTS V2 builder.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default="/opt/novel-similarity-service")
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")

    state_dir = f"{args.remote_root.rstrip('/')}/shared/runtime/language_fts_v2_build"
    command = (
        f"PID=$(cat {state_dir}/build.pid 2>/dev/null || true); "
        "if [ -z \"$PID\" ] || ! kill -0 \"$PID\" 2>/dev/null; then echo not_running; exit 0; fi; "
        "ARGS=$(ps -p \"$PID\" -o args=); "
        "case \"$ARGS\" in *build_drama_subtitle_lexical_index_v1.py*--build-language-v2*) ;; "
        "*) echo unexpected_process; exit 2;; esac; "
        "kill \"$PID\"; sleep 2; if kill -0 \"$PID\" 2>/dev/null; then kill -9 \"$PID\"; fi; echo stopped=$PID"
    )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=30)
        status = stdout.channel.recv_exit_status()
        output = (stdout.read().decode("utf-8", errors="replace") + stderr.read().decode("utf-8", errors="replace")).strip()
        if status:
            raise RuntimeError(output)
        print(output)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
