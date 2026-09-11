from __future__ import annotations

import argparse
import os

import paramiko


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only status of the isolated cloud V2 subtitle probe.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default="/opt/novel-similarity-service")
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    state = f"{args.remote_root.rstrip('/')}/shared/runtime/v2_subtitle_probe"
    command = (
        "echo '===== processes ====='; ps -eo pid,stat,etime,args | grep -E '18102|run_probe.py' | grep -v grep || true; "
        "echo '===== health ====='; curl -sS --max-time 5 http://127.0.0.1:18102/api/v1/health/ready || true; "
        "echo; echo '===== result ====='; ls -lh " + state + "/result.json 2>/dev/null || true; "
        "echo '===== log ====='; tail -n 20 " + state + "/api.log 2>/dev/null || true"
    )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=30)
        status = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace") + stderr.read().decode("utf-8", errors="replace")
        if status:
            raise RuntimeError(output)
        print(output.strip())
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
