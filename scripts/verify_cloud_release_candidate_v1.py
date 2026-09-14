from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.deploy_ecs_release_v1 import RemoteSession, safe_release_component


REMOTE_ROOT = "/opt/novel-similarity-service"
REQUIRED_FILES = (
    "api/app.py",
    "api/config.py",
    "api/cover_routes.py",
    "api/runtime_worker.py",
    "scripts/run_cover_worker_v1.py",
    "requirements-deploy.txt",
    "web-dist/index.html",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify an inactive cloud release candidate.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--release-name", required=True)
    parser.add_argument("--remote-root", default=REMOTE_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    if safe_release_component(args.release_name) != args.release_name:
        raise SystemExit("Unsafe release name")

    candidate = posixpath.join(args.remote_root.rstrip("/"), "releases", args.release_name)
    required_checks = " && ".join(
        f"test -f {shlex.quote(posixpath.join(candidate, relative))}"
        for relative in REQUIRED_FILES
    )
    web_dist = posixpath.join(candidate, "web-dist")
    command = (
        f"current=$(readlink -f {shlex.quote(posixpath.join(args.remote_root, 'current'))}) && "
        f"test \"$current\" != {shlex.quote(candidate)} && "
        f"test -d {shlex.quote(candidate)} && {required_checks} && "
        f"printf 'candidate_status=inactive_complete\\n' && "
        f"printf 'code_files=' && find {shlex.quote(posixpath.join(candidate, 'api'))} "
        f"{shlex.quote(posixpath.join(candidate, 'service'))} "
        f"{shlex.quote(posixpath.join(candidate, 'scripts'))} -type f | wc -l && "
        f"printf 'web_files=' && find {shlex.quote(web_dist)} -type f | wc -l && "
        f"find {shlex.quote(web_dist)} -type f -printf '%P %s\\n' | sort"
    )

    remote = RemoteSession(args.host, args.user, password)
    try:
        print(remote.run(command, timeout=120).strip())
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
