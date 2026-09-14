from __future__ import annotations

import argparse
import json
import posixpath
import shlex
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.deploy_ecs_release_v1 import safe_release_component, wait_for_remote_health
from scripts.install_cloud_cover_worker_v1 import RemoteSession


REMOTE_ROOT = "/opt/novel-similarity-service"
SERVICE_NAME = "novel-similarity-api.service"
HEALTH_URL = "http://127.0.0.1:18101/api/v1/health/ready"
BUSINESS_DB = f"{REMOTE_ROOT}/shared/data/novel_similarity_web_v1.sqlite3"
BACKUP_DIR = f"{REMOTE_ROOT}/shared/backups"


def activate_candidate(
    remote: Any,
    *,
    release_name: str,
    execute: bool,
) -> dict[str, object]:
    if safe_release_component(release_name) != release_name:
        raise ValueError("Unsafe release name")
    current_link = posixpath.join(REMOTE_ROOT, "current")
    candidate = posixpath.join(REMOTE_ROOT, "releases", release_name)
    previous = remote.run(f"readlink -f {shlex.quote(current_link)}").strip()
    remote.run(
        f"test -d {shlex.quote(candidate)} && "
        f"test \"$(realpath -m -- {shlex.quote(candidate)})\" = {shlex.quote(candidate)}"
    )
    service_state = remote.run(f"systemctl is-active {SERVICE_NAME}").strip()
    report: dict[str, object] = {
        "mode": "execute" if execute else "preflight",
        "previous_release": previous,
        "candidate_release": candidate,
        "service_state_before": service_state,
        "activated": False,
    }
    if not execute:
        return report
    if previous == candidate:
        raise RuntimeError("Candidate release is already active")
    if service_state != "active":
        raise RuntimeError(f"API service is not active before switch: {service_state}")

    backup_name = f"business-{safe_release_component(release_name)}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    backup_path = posixpath.join(BACKUP_DIR, backup_name)
    remote.run(
        f"test -f {shlex.quote(BUSINESS_DB)} && mkdir -p {shlex.quote(BACKUP_DIR)} && "
        f"sqlite3 {shlex.quote(BUSINESS_DB)} \".backup '{backup_path}'\""
    )
    report["business_db_backup"] = backup_path

    try:
        remote.run(f"ln -sfn {shlex.quote(candidate)} {shlex.quote(current_link)}")
        remote.run(f"systemctl restart {SERVICE_NAME}", timeout=120)
        health = wait_for_remote_health(
            remote,
            health_url=HEALTH_URL,
            wait_seconds=90,
            retry_interval_seconds=2,
        )
    except Exception:
        remote.run(f"ln -sfn {shlex.quote(previous)} {shlex.quote(current_link)}")
        remote.run(f"systemctl restart {SERVICE_NAME}", timeout=120)
        wait_for_remote_health(
            remote,
            health_url=HEALTH_URL,
            wait_seconds=90,
            retry_interval_seconds=2,
        )
        raise

    report["activated"] = True
    report["service_state_after"] = remote.run(f"systemctl is-active {SERVICE_NAME}").strip()
    report["health_ready"] = bool(health.strip())
    report["current_release"] = remote.run(f"readlink -f {shlex.quote(current_link)}").strip()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Activate a verified cloud release candidate.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--release-name", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remote = RemoteSession(args.resource_file)
    try:
        report = activate_candidate(
            remote,
            release_name=args.release_name,
            execute=bool(args.execute),
        )
    finally:
        remote.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
