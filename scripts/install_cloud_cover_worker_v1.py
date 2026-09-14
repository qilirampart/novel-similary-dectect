from __future__ import annotations

import argparse
import json
import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import paramiko


DEFAULT_REMOTE_ROOT = "/opt/novel-similarity-service"
DEFAULT_SERVICE_NAME = "novel-similarity-cover-worker.service"
DEFAULT_API_SERVICE = "novel-similarity-api.service"
REQUIRED_ENVIRONMENT_KEYS = frozenset(
    {
        "COVER_MONITOR_DB",
        "COVER_MONITOR_ASSET_ROOT",
        "COVER_MONITOR_STAGING_ROOT",
        "COVER_MONITOR_IMPORT_ROOT",
        "COVER_MONITOR_EXPORT_ROOT",
        "COVER_MONITOR_STORAGE_BACKEND",
        "COVER_MONITOR_PROXY_URL",
        "COVER_MONITOR_VISION_API_BASE",
        "COVER_MONITOR_VISION_API_KEY",
        "COVER_MONITOR_VISION_MODEL",
    }
)
REQUIRED_PYTHON_MODULES = ("PIL", "yt_dlp", "alibabacloud_oss_v2")
_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.@-]+\.service")


def validate_remote_root(remote_root: str) -> PurePosixPath:
    raw = str(remote_root).strip()
    root = PurePosixPath(raw)
    if raw != DEFAULT_REMOTE_ROOT or ".." in root.parts:
        raise ValueError("remote root must be the isolated novel similarity project root")
    return root


def validate_service_name(service_name: str) -> str:
    normalized = str(service_name).strip()
    if _SERVICE_NAME.fullmatch(normalized) is None:
        raise ValueError("service name is invalid")
    return normalized


def render_worker_unit(*, remote_root: str, api_service: str) -> str:
    root = validate_remote_root(remote_root)
    parent_service = validate_service_name(api_service)
    return "\n".join(
        [
            "[Unit]",
            "Description=Novel Similarity Cover Monitor Worker",
            f"PartOf={parent_service}",
            f"After=network-online.target {parent_service} mihomo-cover.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={root}/current",
            f"EnvironmentFile={root}/shared/novel-similarity.env",
            "Environment=PYTHONUNBUFFERED=1",
            f"ExecStart={root}/shared/venv/bin/python scripts/run_cover_worker_v1.py",
            "Restart=always",
            "RestartSec=5",
            "TimeoutStopSec=120",
            "KillSignal=SIGTERM",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectHome=true",
            "ProtectSystem=full",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def missing_required_environment_keys(keys: set[str]) -> list[str]:
    return sorted(REQUIRED_ENVIRONMENT_KEYS - {str(key).strip() for key in keys})


def provision_worker(
    remote: Any,
    *,
    remote_root: str,
    service_name: str,
    api_service: str,
    execute: bool,
) -> dict[str, object]:
    root = validate_remote_root(remote_root)
    worker_service = validate_service_name(service_name)
    parent_service = validate_service_name(api_service)
    prerequisites = remote.inspect_worker_prerequisites(root)
    environment_keys = {
        str(key) for key in prerequisites.get("environment_keys", [])
    }
    missing_keys = missing_required_environment_keys(environment_keys)
    blockers = []
    for key in (
        "worker_script_ready",
        "python_ready",
        "dependencies_ready",
        "environment_file_ready",
    ):
        if prerequisites.get(key) is not True:
            blockers.append(key)
    if missing_keys:
        blockers.append("missing_environment_keys")
    if prerequisites.get("proxy_service") != "active":
        blockers.append("proxy_service_not_active")

    report: dict[str, object] = {
        "mode": "execute" if execute else "preflight",
        "remote_root": str(root),
        "service_name": worker_service,
        "prerequisites": prerequisites,
        "missing_environment_keys": missing_keys,
        "blockers": blockers,
        "installed": False,
    }
    if not execute:
        return report
    if blockers:
        raise RuntimeError("cover worker preflight blocked: " + ", ".join(blockers))

    unit = render_worker_unit(remote_root=str(root), api_service=parent_service)
    previous_unit = remote.install_unit(worker_service, unit)
    try:
        remote.run("systemctl daemon-reload")
        remote.run(f"systemctl enable --now {worker_service}")
        state = remote.run(f"systemctl is-active {worker_service}").strip()
        if state != "active":
            raise RuntimeError(f"cover worker did not become active: {state}")
    except Exception:
        restore = getattr(remote, "restore_unit", None)
        if callable(restore):
            restore(worker_service, previous_unit)
        raise
    report["installed"] = True
    report["service_state"] = state
    return report


class RemoteSession:
    def __init__(self, resource_file: Path) -> None:
        lines = resource_file.read_bytes().splitlines()
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=_ascii_value(lines, 2),
            username=_ascii_value(lines, 3),
            password=_ascii_value(lines, 4),
            timeout=20,
            banner_timeout=30,
            auth_timeout=30,
        )
        self._sftp = self._client.open_sftp()

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()

    def run(self, command: str, *, timeout: int = 120) -> str:
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        status = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        if status != 0:
            raise RuntimeError(f"remote command failed ({status}): {error or output}")
        return output if output.strip() else error

    def inspect_worker_prerequisites(self, remote_root: PurePosixPath) -> dict[str, object]:
        root = str(remote_root)
        current = posixpath.join(root, "current")
        python_path = posixpath.join(root, "shared", "venv", "bin", "python")
        env_path = posixpath.join(root, "shared", "novel-similarity.env")
        worker_path = posixpath.join(current, "scripts", "run_cover_worker_v1.py")
        environment_output = self.run(
            f"test -f {shlex.quote(env_path)} && "
            f"awk -F= '/^COVER_MONITOR_[A-Z0-9_]+=/ "
            "{value=substr($0,index($0,\"=\")+1); if(length(value)>0) print $1}' "
            f"{shlex.quote(env_path)} "
            "|| true"
        )
        dependencies_ready, missing_dependencies = self._dependency_status(python_path)
        return {
            "current_release": self.run(f"readlink -f {shlex.quote(current)}").strip(),
            "worker_script_ready": self._path_test(worker_path, "-f"),
            "python_ready": self._path_test(python_path, "-x"),
            "dependencies_ready": dependencies_ready,
            "missing_dependencies": missing_dependencies,
            "environment_file_ready": self._path_test(env_path, "-f"),
            "environment_keys": sorted(set(environment_output.splitlines())),
            "proxy_service": self.run(
                "systemctl is-active mihomo-cover.service || true"
            ).strip(),
        }

    def _path_test(self, path: str, predicate: str) -> bool:
        result = self.run(
            f"if test {predicate} {shlex.quote(path)}; then printf ready; else printf missing; fi"
        )
        return result.strip() == "ready"

    def _dependency_status(self, python_path: str) -> tuple[bool, list[str]]:
        command = (
            "import importlib.util,json;"
            f"names={list(REQUIRED_PYTHON_MODULES)!r};"
            "print(json.dumps([name for name in names if importlib.util.find_spec(name) is None]))"
        )
        result = self.run(
            f"{shlex.quote(python_path)} -c {shlex.quote(command)} || true"
        )
        try:
            missing = json.loads(result)
        except json.JSONDecodeError:
            return False, list(REQUIRED_PYTHON_MODULES)
        if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
            return False, list(REQUIRED_PYTHON_MODULES)
        return not missing, sorted(set(missing))

    def install_unit(self, service_name: str, content: str) -> bytes | None:
        unit_path = f"/etc/systemd/system/{service_name}"
        previous = self._read_optional(unit_path)
        temporary_path = f"{unit_path}.tmp-{uuid4().hex}"
        try:
            with self._sftp.file(temporary_path, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
            self._sftp.chmod(temporary_path, 0o644)
            self._sftp.posix_rename(temporary_path, unit_path)
        finally:
            try:
                self._sftp.remove(temporary_path)
            except OSError:
                pass
        return previous

    def restore_unit(self, service_name: str, previous: bytes | None) -> None:
        unit_path = f"/etc/systemd/system/{service_name}"
        if previous is None:
            try:
                self._sftp.remove(unit_path)
            except OSError:
                pass
        else:
            temporary_path = f"{unit_path}.rollback-{uuid4().hex}"
            with self._sftp.file(temporary_path, "wb") as stream:
                stream.write(previous)
                stream.flush()
            self._sftp.chmod(temporary_path, 0o644)
            self._sftp.posix_rename(temporary_path, unit_path)
        self.run("systemctl daemon-reload")

    def _read_optional(self, path: str) -> bytes | None:
        try:
            with self._sftp.file(path, "rb") as stream:
                return stream.read()
        except OSError:
            return None


def _ascii_value(lines: list[bytes], line_number: int) -> str:
    if line_number >= len(lines):
        raise ValueError(f"resource file has no line {line_number + 1}")
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no credential value")
    return matches[-1].decode("ascii")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight or install the isolated cloud cover worker service.",
    )
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--api-service", default=DEFAULT_API_SERVICE)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remote = RemoteSession(args.resource_file)
    try:
        report = provision_worker(
            remote,
            remote_root=args.remote_root,
            service_name=args.service_name,
            api_service=args.api_service,
            execute=bool(args.execute),
        )
    finally:
        remote.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
