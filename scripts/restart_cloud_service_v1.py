from __future__ import annotations

import argparse
import os
import time

import paramiko


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--service-name", default="novel-similarity-api.service")
    parser.add_argument("--health-url", default="http://127.0.0.1:18101/api/v1/health/ready")
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--retry-interval-seconds", type=int, default=3)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def run(client: paramiko.SSHClient, command: str, timeout: int = 60, allow_failure: bool = False) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if exit_code != 0 and not allow_failure:
        raise RuntimeError(f"Remote command failed ({exit_code}): {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out if out.strip() else err


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
    try:
        print("restarting_service")
        print(run(client, f"systemctl restart {args.service_name}", timeout=120))
        deadline = time.time() + max(args.wait_seconds, 1)
        last_error = ""
        while time.time() < deadline:
            try:
                health = run(client, f"curl -fsS {args.health_url}", timeout=30)
                print("health_ok")
                print(health.strip())
                print("service_state")
                print(run(client, f"systemctl show {args.service_name} --property=ActiveState,SubState,MainPID"))
                return 0
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(max(args.retry_interval_seconds, 1))
        raise RuntimeError(f"health did not recover in {args.wait_seconds}s: {last_error}")
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
