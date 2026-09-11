from __future__ import annotations

import argparse
import os

import paramiko


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def run(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if exit_code != 0:
        return f"[exit={exit_code}]\nSTDOUT:\n{out}\nSTDERR:\n{err}"
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
        commands = [
            ("service_state", "systemctl show novel-similarity-api.service --property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,Environment"),
            ("systemctl_status", "systemctl status novel-similarity-api.service --no-pager -l | tail -n 40"),
            ("local_health", "curl -i --max-time 20 http://127.0.0.1:18101/api/v1/health"),
            ("listen_18101", "ss -ltnp | grep 18101 || true"),
            ("business_db_paths", "find /opt/novel-similarity-service -name 'novel_similarity_web_v1.sqlite3' 2>/dev/null | head -n 10"),
            (
                "recent_task_rows",
                "DB=/opt/novel-similarity-service/shared/data/novel_similarity_web_v1.sqlite3; "
                "if [ -n \"$DB\" ]; then sqlite3 -header -column \"$DB\" "
                "\"select task_id,status,worker_name,created_at,started_at,finished_at,updated_at,status_message "
                "from compare_tasks order by created_at desc limit 8;\"; "
                "else echo 'db not found'; fi",
            ),
            (
                "recent_task_item_counts",
                "DB=/opt/novel-similarity-service/shared/data/novel_similarity_web_v1.sqlite3; "
                "if [ -n \"$DB\" ]; then sqlite3 -header -column \"$DB\" "
                "\"select task_id,status,count(*) as item_count from compare_task_items "
                "where task_id in (select task_id from compare_tasks order by created_at desc limit 4) "
                "group by task_id,status order by task_id desc,status;\"; "
                "else echo 'db not found'; fi",
            ),
            (
                "running_task_items",
                "DB=/opt/novel-similarity-service/shared/data/novel_similarity_web_v1.sqlite3; "
                "if [ -n \"$DB\" ]; then sqlite3 -header -column \"$DB\" "
                "\"select task_id,item_order,source_ref,query_text_preview,started_at,updated_at "
                "from compare_task_items where status='running' order by updated_at desc, task_id asc, item_order asc limit 20;\"; "
                "else echo 'db not found'; fi",
            ),
            ("recent_logs", "journalctl -u novel-similarity-api.service -n 120 --no-pager"),
        ]
        for label, command in commands:
            print(f"===== {label} =====")
            print(run(client, command, timeout=120).strip())
            print()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
