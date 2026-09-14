from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.install_cloud_cover_worker_v1 import REQUIRED_PYTHON_MODULES, RemoteSession


PYTHON_PATH = "/opt/novel-similarity-service/shared/venv/bin/python"
PROXY_URL = "http://127.0.0.1:17890"
HEALTH_URL = "http://127.0.0.1:18101/api/v1/health/ready"
PACKAGE_INDEX_URL = "https://pypi.org/simple"
PACKAGES = (
    "Pillow>=10.4,<12",
    "yt-dlp>=2025.10.14",
    "alibabacloud-oss-v2>=1.2,<2",
    "alibabacloud-credentials>=0.3.5,<1",
)


def build_install_command(*, execute: bool) -> str:
    arguments = [PYTHON_PATH, "-m", "pip", "install", "--index-url", PACKAGE_INDEX_URL]
    if not execute:
        arguments.append("--dry-run")
    arguments.extend(PACKAGES)
    command = " ".join(shlex.quote(argument) for argument in arguments)
    return f"HTTPS_PROXY={PROXY_URL} HTTP_PROXY={PROXY_URL} {command}"


def dependency_probe_command() -> str:
    code = (
        "import importlib.util,json;"
        f"names={list(REQUIRED_PYTHON_MODULES)!r};"
        "print(json.dumps([n for n in names if importlib.util.find_spec(n) is None]))"
    )
    return f"{shlex.quote(PYTHON_PATH)} -c {shlex.quote(code)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install isolated cover-monitor dependencies.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remote = RemoteSession(args.resource_file)
    try:
        before = json.loads(remote.run(dependency_probe_command()).strip())
        health_before = remote.run(f"curl -fsS {shlex.quote(HEALTH_URL)}").strip()
        install_output = remote.run(build_install_command(execute=bool(args.execute)), timeout=900)
        after = json.loads(remote.run(dependency_probe_command()).strip())
        health_after = remote.run(f"curl -fsS {shlex.quote(HEALTH_URL)}").strip()
    finally:
        remote.close()

    report = {
        "mode": "execute" if args.execute else "dry_run",
        "missing_before": before,
        "missing_after": after,
        "api_healthy_before": bool(health_before),
        "api_healthy_after": bool(health_after),
        "pip_output": install_output.strip(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.execute and after:
        raise SystemExit("Cover dependencies remain missing after installation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
