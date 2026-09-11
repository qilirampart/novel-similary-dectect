from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from urllib.parse import quote

import paramiko


def _connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        compress=True,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def _run(client: paramiko.SSHClient, command: str, *, timeout: int = 300) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(f"remote command failed ({code}):\n{err or out}")
    return out


def restore_snapshot(
    *,
    host: str,
    user: str,
    password: str,
    qdrant_url: str,
    collection_name: str,
    snapshot_path: str,
    wait: bool,
    priority: str | None,
    checksum: str | None,
) -> None:
    payload: dict[str, object] = {
        "location": f"file://{snapshot_path}",
    }
    if priority:
        payload["priority"] = priority
    if checksum:
        payload["checksum"] = checksum

    query = f"wait={'true' if wait else 'false'}"
    url = f"{qdrant_url.rstrip('/')}/collections/{quote(collection_name, safe='')}/snapshots/recover?{query}"
    body = json.dumps(payload, ensure_ascii=False)
    command = (
        "curl -sS -X PUT "
        + shlex.quote(url)
        + " -H "
        + shlex.quote("Content-Type: application/json")
        + " --data-raw "
        + shlex.quote(body)
    )

    client = _connect(host, user, password)
    try:
        out = _run(client, command)
    finally:
        client.close()
    print(out, end="" if out.endswith("\n") else "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--password-env", default="")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection-name", required=True)
    parser.add_argument("--snapshot-path", required=True)
    parser.add_argument("--wait", action="store_true", default=True)
    parser.add_argument("--no-wait", dest="wait", action="store_false")
    parser.add_argument("--priority")
    parser.add_argument("--checksum")
    args = parser.parse_args()

    password = args.password or os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit("Provide --password or --password-env with a populated environment variable.")
    restore_snapshot(
        host=args.host,
        user=args.user,
        password=password,
        qdrant_url=args.qdrant_url,
        collection_name=args.collection_name,
        snapshot_path=args.snapshot_path,
        wait=args.wait,
        priority=args.priority,
        checksum=args.checksum,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
