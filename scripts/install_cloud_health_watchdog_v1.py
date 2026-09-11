from __future__ import annotations

import argparse
import os
import posixpath
import stat
from pathlib import Path

import paramiko


WATCHDOG_SCRIPT_PATH = "/usr/local/bin/novel-similarity-api-watchdog.sh"
WATCHDOG_SERVICE_PATH = "/etc/systemd/system/novel-similarity-api-watchdog.service"
WATCHDOG_TIMER_PATH = "/etc/systemd/system/novel-similarity-api-watchdog.timer"
DEFAULT_LIVENESS_URL = "http://127.0.0.1:18101/api/v1/health/live"
DEFAULT_READINESS_URL = "http://127.0.0.1:18101/api/v1/health/ready"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--api-service-name", default="novel-similarity-api.service")
    parser.add_argument("--health-url", default=DEFAULT_LIVENESS_URL)
    parser.add_argument("--readiness-url", default=DEFAULT_READINESS_URL)
    parser.add_argument("--listen-port", type=int, default=18101)
    parser.add_argument("--timer-unit", default="novel-similarity-api-watchdog.timer")
    parser.add_argument("--service-unit", default="novel-similarity-api-watchdog.service")
    parser.add_argument("--state-dir", default="/opt/novel-similarity-service/shared/watchdog/novel-similarity-api")
    parser.add_argument("--fail-threshold", type=int, default=3)
    parser.add_argument("--health-timeout-seconds", type=int, default=8)
    parser.add_argument("--recovery-wait-seconds", type=int, default=40)
    parser.add_argument("--timer-interval-seconds", type=int, default=30)
    parser.add_argument("--inspect-only", action="store_true")
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def build_watchdog_script(args: argparse.Namespace) -> str:
    fail_threshold = max(int(args.fail_threshold), 1)
    health_timeout_seconds = max(int(args.health_timeout_seconds), 1)
    recovery_wait_seconds = max(int(args.recovery_wait_seconds), 5)
    state_dir = args.state_dir.rstrip("/")
    return f"""#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="{args.api_service_name}"
HEALTH_URL="{args.health_url}"
READINESS_URL="{args.readiness_url}"
LISTEN_PORT="{int(args.listen_port)}"
STATE_DIR="{state_dir}"
FAIL_THRESHOLD="{fail_threshold}"
HEALTH_TIMEOUT_SECONDS="{health_timeout_seconds}"
RECOVERY_WAIT_SECONDS="{recovery_wait_seconds}"
FAIL_FILE="$STATE_DIR/fail_count"
LOG_FILE="$STATE_DIR/watchdog.log"
EVENT_ROOT="$STATE_DIR/events"

mkdir -p "$STATE_DIR" "$EVENT_ROOT"
touch "$LOG_FILE"

timestamp="$(date '+%Y%m%d-%H%M%S')"

log_line() {{
  printf '%s %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG_FILE"
}}

health_check() {{
  curl -fsS --max-time "$HEALTH_TIMEOUT_SECONDS" "$HEALTH_URL" >/dev/null
}}

readiness_check() {{
  curl -fsS --max-time "$HEALTH_TIMEOUT_SECONDS" "$READINESS_URL" >/dev/null
}}

read_fail_count() {{
  if [[ -f "$FAIL_FILE" ]]; then
    cat "$FAIL_FILE"
  else
    echo "0"
  fi
}}

write_fail_count() {{
  printf '%s\\n' "$1" >"$FAIL_FILE"
}}

if health_check; then
  write_fail_count 0
  exit 0
fi

service_active_state="$(systemctl show "$SERVICE_NAME" --property=ActiveState --value || true)"
service_sub_state="$(systemctl show "$SERVICE_NAME" --property=SubState --value || true)"
if [[ "$service_active_state" != "active" || "$service_sub_state" != "running" ]]; then
  write_fail_count 0
  log_line "skip_unhealthy_during_service_transition state=$service_active_state/$service_sub_state"
  exit 0
fi

current_fail_count="$(( $(read_fail_count) + 1 ))"
write_fail_count "$current_fail_count"
log_line "health_failed count=$current_fail_count url=$HEALTH_URL"

if [[ "$current_fail_count" -lt "$FAIL_THRESHOLD" ]]; then
  exit 0
fi

event_dir="$EVENT_ROOT/$timestamp"
mkdir -p "$event_dir"
ln -sfn "$event_dir" "$STATE_DIR/last_event"

pid="$(systemctl show "$SERVICE_NAME" --property=MainPID --value || true)"
readiness_state="not_checked"
if readiness_check; then
  readiness_state="ready"
else
  readiness_state="not_ready_or_unavailable"
fi
printf 'timestamp=%s\\nservice=%s\\nhealth_url=%s\\npid=%s\\n' \
  "$timestamp" "$SERVICE_NAME" "$HEALTH_URL" "$pid" >"$event_dir/summary.txt"
printf 'readiness_url=%s\\nreadiness_state=%s\\n' \\
  "$READINESS_URL" "$readiness_state" >>"$event_dir/summary.txt"
systemctl show "$SERVICE_NAME" >"$event_dir/systemctl_show.before.txt" 2>&1 || true
ss -tanp | grep "$LISTEN_PORT" >"$event_dir/socket_state.before.txt" 2>&1 || true
journalctl -u "$SERVICE_NAME" -n 200 --no-pager >"$event_dir/journal.before_restart.log" 2>&1 || true

if [[ -n "$pid" && "$pid" != "0" ]]; then
  kill -USR1 "$pid" >/dev/null 2>&1 || true
  sleep 2
fi

log_line "restarting service=$SERVICE_NAME after threshold=$FAIL_THRESHOLD"
systemctl restart "$SERVICE_NAME"

recovered=0
for ((i=0; i<RECOVERY_WAIT_SECONDS; i++)); do
  if health_check; then
    recovered=1
    break
  fi
  sleep 1
done

systemctl show "$SERVICE_NAME" >"$event_dir/systemctl_show.after.txt" 2>&1 || true
ss -tanp | grep "$LISTEN_PORT" >"$event_dir/socket_state.after.txt" 2>&1 || true
journalctl -u "$SERVICE_NAME" -n 200 --no-pager >"$event_dir/journal.after_restart.log" 2>&1 || true

if [[ "$recovered" -eq 1 ]]; then
  write_fail_count 0
  log_line "recovered service=$SERVICE_NAME"
  exit 0
fi

log_line "recovery_failed service=$SERVICE_NAME"
exit 1
"""


def build_watchdog_service(args: argparse.Namespace) -> str:
    return f"""[Unit]
Description=Novel Similarity API health watchdog
After=network.target {args.api_service_name}

[Service]
Type=oneshot
ExecStart={WATCHDOG_SCRIPT_PATH}
"""


def build_watchdog_timer(args: argparse.Namespace) -> str:
    interval = max(int(args.timer_interval_seconds), 10)
    return f"""[Unit]
Description=Run Novel Similarity API health watchdog every {interval}s

[Timer]
OnBootSec=45s
OnUnitActiveSec={interval}s
Unit={args.service_unit}

[Install]
WantedBy=timers.target
"""


class RemoteSession:
    def __init__(self, host: str, user: str, password: str) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        self._sftp = self._client.open_sftp()

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()

    def run(self, command: str, *, timeout: int = 60, allow_failure: bool = False) -> str:
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if exit_status != 0 and not allow_failure:
            raise RuntimeError(
                f"Remote command failed ({exit_status}): {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
            )
        return out if out.strip() else err

    def mkdir_p(self, remote_dir: str) -> None:
        self.run(f"mkdir -p {remote_dir}")

    def write_text(self, remote_path: str, content: str, *, mode: int | None = None) -> None:
        remote_dir = posixpath.dirname(remote_path)
        if remote_dir:
            self.mkdir_p(remote_dir)
        with self._sftp.open(remote_path, "w") as remote_file:
            remote_file.write(content)
        if mode is not None:
            self._sftp.chmod(remote_path, mode)


def inspect_remote(remote: RemoteSession, args: argparse.Namespace) -> None:
    print("service_state:")
    print(
        remote.run(
            f"systemctl show {args.api_service_name} --property=ActiveState,SubState,MainPID,ExecMainStartTimestamp",
            allow_failure=True,
        ).strip()
    )
    print("watchdog_timer:")
    print(
        remote.run(
            f"systemctl status {args.timer_unit} --no-pager -l",
            allow_failure=True,
        ).strip()
    )
    print("watchdog_service:")
    print(
        remote.run(
            f"systemctl status {args.service_unit} --no-pager -l",
            allow_failure=True,
        ).strip()
    )
    print("watchdog_state:")
    print(remote.run(f"ls -la {args.state_dir}", allow_failure=True).strip())
    print("watchdog_log_tail:")
    print(remote.run(f"tail -n 40 {args.state_dir}/watchdog.log", allow_failure=True).strip())


def install_remote(remote: RemoteSession, args: argparse.Namespace) -> None:
    remote.write_text(
        WATCHDOG_SCRIPT_PATH,
        build_watchdog_script(args),
        mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )
    remote.write_text(WATCHDOG_SERVICE_PATH, build_watchdog_service(args))
    remote.write_text(WATCHDOG_TIMER_PATH, build_watchdog_timer(args))
    remote.run("systemctl daemon-reload", timeout=120)
    remote.run(f"systemctl enable --now {args.timer_unit}", timeout=120)
    remote.run(f"systemctl start {args.service_unit}", timeout=120, allow_failure=True)


def main() -> int:
    args = parse_args()
    password = require_password(args.password_env)
    remote = RemoteSession(args.host, args.user, password)
    try:
        if not args.inspect_only:
            install_remote(remote, args)
        inspect_remote(remote, args)
        return 0
    finally:
        remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
