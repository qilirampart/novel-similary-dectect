from __future__ import annotations

import argparse
import os
import posixpath
import re
import shlex
import sys
import time
from pathlib import Path

import paramiko


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REMOTE_ROOT = "/opt/novel-similarity-service"
DEFAULT_SERVICE_NAME = "novel-similarity-api.service"
DEFAULT_HEALTH_URL = "http://127.0.0.1:18101/api/v1/health/ready"
DEFAULT_HEALTH_WAIT_SECONDS = 60
DEFAULT_HEALTH_RETRY_INTERVAL_SECONDS = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a rollback-safe novel similarity ECS release.",
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--health-wait-seconds", type=int, default=DEFAULT_HEALTH_WAIT_SECONDS)
    parser.add_argument(
        "--health-retry-interval-seconds",
        type=int,
        default=DEFAULT_HEALTH_RETRY_INTERVAL_SECONDS,
    )
    parser.add_argument("--release-name", default="")
    parser.add_argument(
        "--backup-business-db",
        action="store_true",
        help="Create a consistent remote SQLite backup before switching the release.",
    )
    parser.add_argument(
        "--business-db-path",
        default="/opt/novel-similarity-service/shared/data/novel_similarity_web_v1.sqlite3",
    )
    parser.add_argument(
        "--backup-dir",
        default="/opt/novel-similarity-service/shared/backups",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        required=True,
        help="Repo-relative files to upload into the new release.",
    )
    parser.add_argument(
        "--file-map",
        action="append",
        default=[],
        help="Upload mapping in LOCAL=REMOTE form, useful for web-dist assets.",
    )
    parser.add_argument(
        "--set-env",
        action="append",
        default=[],
        help="Optional systemd override env entry like KEY=VALUE. Can be repeated.",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Only inspect remote release/service state without mutating anything.",
    )
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Skip remote curl health verification after restart.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create and upload a release without switching current or restarting the service.",
    )
    return parser.parse_args()


def build_release_name(explicit_name: str) -> str:
    if explicit_name.strip():
        return explicit_name.strip()
    return f"{time.strftime('%Y%m%d')}-auto-worker-2-sync-{time.strftime('%H%M%S')}"


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def to_remote_release_path(remote_root: str, release_name: str) -> str:
    return posixpath.join(remote_root.rstrip("/"), "releases", release_name)


def quote_remote(path: str) -> str:
    return shlex.quote(path)


def safe_release_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "release"


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
        self.run(f"mkdir -p {quote_remote(remote_dir)}")

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.mkdir_p(posixpath.dirname(remote_path))
        self._sftp.put(str(local_path), remote_path)


def inspect_remote(
    remote: RemoteSession,
    *,
    remote_root: str,
    service_name: str,
) -> None:
    current_target = remote.run(f"readlink -f {quote_remote(posixpath.join(remote_root, 'current'))}")
    print("current_release:", current_target.strip())
    print("recent_releases:")
    print(
        remote.run(
            f"ls -1 {quote_remote(posixpath.join(remote_root, 'releases'))} | tail -n 10",
            allow_failure=True,
        ).strip()
    )
    print("service_state:")
    print(
        remote.run(
            f"systemctl show {quote_remote(service_name)} --property=ActiveState,SubState,FragmentPath,Environment",
            allow_failure=True,
        ).strip()
    )


def wait_for_remote_health(
    remote: RemoteSession,
    *,
    health_url: str,
    wait_seconds: int,
    retry_interval_seconds: int,
) -> str:
    deadline = time.time() + max(int(wait_seconds), 1)
    interval = max(int(retry_interval_seconds), 1)
    last_error = ""
    while time.time() < deadline:
        try:
            return remote.run(f"curl -fsS {quote_remote(health_url)}", timeout=60)
        except Exception as exc:
            last_error = str(exc)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval, max(remaining, 0)))
    raise RuntimeError(
        f"Remote health check did not recover within {max(int(wait_seconds), 1)}s: {last_error}"
    )


def write_systemd_override(
    remote: RemoteSession,
    *,
    service_name: str,
    env_pairs: list[str],
) -> None:
    if not env_pairs:
        return
    override_dir = f"/etc/systemd/system/{service_name}.d"
    override_path = posixpath.join(override_dir, "10-codex-runtime.conf")
    lines = ["[Service]"] + [f'Environment="{entry}"' for entry in env_pairs]
    payload = "\n".join(lines) + "\n"
    escaped = payload.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    remote.run(f"mkdir -p {quote_remote(override_dir)}")
    remote.run(f'printf "{escaped}" > {quote_remote(override_path)}')
    remote.run("systemctl daemon-reload")


def deploy_release(
    remote: RemoteSession,
    *,
    remote_root: str,
    service_name: str,
    health_url: str,
    release_name: str,
    files: list[str],
    file_maps: list[str],
    prepare_only: bool,
    set_env: list[str],
    skip_health_check: bool,
    health_wait_seconds: int,
    health_retry_interval_seconds: int,
    backup_business_db: bool,
    business_db_path: str,
    backup_dir: str,
) -> None:
    current_symlink = posixpath.join(remote_root, "current")
    current_target = remote.run(f"readlink -f {quote_remote(current_symlink)}").strip()
    if not current_target:
        raise RuntimeError("Unable to resolve current release symlink.")

    new_release = to_remote_release_path(remote_root, release_name)
    print("previous_release:", current_target)
    print("new_release:", new_release)

    if backup_business_db:
        backup_name = (
            f"business-{safe_release_component(release_name)}-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
        )
        backup_path = posixpath.join(backup_dir.rstrip("/"), backup_name)
        remote.run(
            f"test -f {quote_remote(business_db_path)} && "
            f"mkdir -p {quote_remote(backup_dir)} && "
            f"sqlite3 {quote_remote(business_db_path)} "
            f"\".backup '{backup_path}'\""
        )
        print("business_db_backup:", backup_path)

    remote.run(f"test ! -e {quote_remote(new_release)}")
    remote.run(f"mkdir -p {quote_remote(new_release)}")
    remote.run(f"cp -a {quote_remote(current_target)}/. {quote_remote(new_release)}/")

    for relative_file in files:
        local_path = (ROOT_DIR / relative_file).resolve()
        if not local_path.exists() or not local_path.is_file():
            raise RuntimeError(f"Local file not found for deploy: {local_path}")
        remote_path = posixpath.join(new_release, relative_file.replace("\\", "/"))
        print("upload:", relative_file, "->", remote_path)
        remote.upload_file(local_path, remote_path)

    for mapping in file_maps:
        if "=" not in mapping:
            raise RuntimeError(f"Invalid --file-map value (expected LOCAL=REMOTE): {mapping}")
        local_relative, remote_relative = mapping.split("=", 1)
        local_path = (ROOT_DIR / local_relative).resolve()
        if not local_path.exists() or not local_path.is_file():
            raise RuntimeError(f"Local file not found for deploy: {local_path}")
        remote_path = posixpath.join(new_release, remote_relative.replace("\\", "/").lstrip("/"))
        print("upload:", local_relative, "->", remote_path)
        remote.upload_file(local_path, remote_path)

    if set_env:
        write_systemd_override(remote, service_name=service_name, env_pairs=set_env)

    if prepare_only:
        print("prepare_status: success")
        return

    def rollback(reason: str) -> None:
        print("rollback_reason:", reason)
        remote.run(f"ln -sfn {quote_remote(current_target)} {quote_remote(current_symlink)}")
        remote.run(f"systemctl restart {quote_remote(service_name)}", timeout=120)
        if not skip_health_check:
            health_output = wait_for_remote_health(
                remote,
                health_url=health_url,
                wait_seconds=health_wait_seconds,
                retry_interval_seconds=health_retry_interval_seconds,
            )
            print("rollback_health_check:")
            print(health_output.strip())

    try:
        remote.run(f"ln -sfn {quote_remote(new_release)} {quote_remote(current_symlink)}")
        remote.run(f"systemctl restart {quote_remote(service_name)}", timeout=120)
        if not skip_health_check:
            health_output = wait_for_remote_health(
                remote,
                health_url=health_url,
                wait_seconds=health_wait_seconds,
                retry_interval_seconds=health_retry_interval_seconds,
            )
            print("health_check:")
            print(health_output.strip())
    except Exception as exc:
        rollback(str(exc))
        raise


def main() -> int:
    args = parse_args()
    password = require_password(args.password_env)
    release_name = build_release_name(args.release_name)
    remote = RemoteSession(args.host, args.user, password)
    try:
        inspect_remote(
            remote,
            remote_root=args.remote_root,
            service_name=args.service_name,
        )
        if args.inspect_only:
            return 0
        deploy_release(
            remote,
            remote_root=args.remote_root,
            service_name=args.service_name,
            health_url=args.health_url,
            release_name=release_name,
            files=args.files,
            file_maps=args.file_map,
            prepare_only=bool(args.prepare_only),
            set_env=args.set_env,
            skip_health_check=bool(args.skip_health_check),
            health_wait_seconds=args.health_wait_seconds,
            health_retry_interval_seconds=args.health_retry_interval_seconds,
            backup_business_db=bool(args.backup_business_db),
            business_db_path=args.business_db_path,
            backup_dir=args.backup_dir,
        )
        print("deploy_status: success")
        return 0
    finally:
        remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
