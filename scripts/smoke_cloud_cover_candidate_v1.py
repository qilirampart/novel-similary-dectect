from __future__ import annotations

import argparse
import posixpath
import shlex
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.deploy_ecs_release_v1 import safe_release_component
from scripts.install_cloud_cover_worker_v1 import RemoteSession


REMOTE_ROOT = "/opt/novel-similarity-service"
HEALTH_URL = "http://127.0.0.1:18101/api/v1/health/ready"


def build_smoke_command(release_name: str) -> str:
    if safe_release_component(release_name) != release_name:
        raise ValueError("Unsafe release name")
    candidate = posixpath.join(REMOTE_ROOT, "releases", release_name)
    current = posixpath.join(REMOTE_ROOT, "current")
    environment_file = posixpath.join(REMOTE_ROOT, "shared", "novel-similarity.env")
    python_path = posixpath.join(REMOTE_ROOT, "shared", "venv", "bin", "python")
    import_code = "from api.app import app; print('candidate_api_import=ok')"
    return (
        f"test \"$(readlink -f {shlex.quote(current)})\" != {shlex.quote(candidate)} && "
        f"test -f {shlex.quote(environment_file)} && "
        f"set -a && . {shlex.quote(environment_file)} && set +a && "
        f"cd {shlex.quote(candidate)} && export PYTHONPATH={shlex.quote(candidate)} && "
        f"{shlex.quote(python_path)} -c {shlex.quote(import_code)} && "
        f"timeout 180 {shlex.quote(python_path)} scripts/run_cover_worker_v1.py "
        "--once --worker-name candidate-smoke"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test an inactive cloud cover release.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--release-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remote = RemoteSession(args.resource_file)
    try:
        health_before = remote.run(f"curl -fsS {shlex.quote(HEALTH_URL)}").strip()
        smoke_output = remote.run(build_smoke_command(args.release_name), timeout=240).strip()
        health_after = remote.run(f"curl -fsS {shlex.quote(HEALTH_URL)}").strip()
    finally:
        remote.close()
    print(smoke_output)
    print(f"active_api_healthy_before={bool(health_before)}")
    print(f"active_api_healthy_after={bool(health_after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
