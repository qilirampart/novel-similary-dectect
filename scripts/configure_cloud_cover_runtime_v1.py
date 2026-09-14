from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import stat
from pathlib import Path, PurePosixPath
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import paramiko


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.install_cloud_cover_worker_v1 import validate_remote_root


MAX_COOKIE_BYTES = 5 * 1024 * 1024
CLOUD_PROXY_URL = "http://127.0.0.1:17890"
_COVER_ENV_LINE = re.compile(r"^\s*COVER_MONITOR_[A-Z0-9_]+\s*=")
_MANAGED_COMMENT = "# Cover monitor runtime (managed by project tooling)"


@dataclass(frozen=True)
class CoverRuntimeInputs:
    api_base: str
    api_key: str
    model: str
    proxy_url: str
    cookie_bytes: bytes


def load_runtime_inputs(assistant_runtime: str | Path) -> CoverRuntimeInputs:
    runtime = Path(assistant_runtime).resolve()
    payload = json.loads((runtime / "api_config.json").read_text(encoding="utf-8"))
    profiles = (payload.get("llm") or {}).get("profiles") or []
    cover = payload.get("cover_review") or payload.get("video_review") or {}
    selected_id = str(cover.get("profile_id") or "").strip()
    ready = [
        item
        for item in profiles
        if isinstance(item, dict)
        and item.get("enabled", True)
        and item.get("api_base")
        and item.get("api_key")
        and item.get("model")
    ]
    selected = next(
        (item for item in ready if str(item.get("id") or "") == selected_id),
        ready[0] if ready else None,
    )
    if selected is None:
        raise ValueError("no enabled cover vision profile is available")
    inputs = CoverRuntimeInputs(
        api_base=str(selected["api_base"]).strip(),
        api_key=str(selected["api_key"]).strip(),
        model=str(selected["model"]).strip(),
        proxy_url=CLOUD_PROXY_URL,
        cookie_bytes=(runtime / "youtube_cookies.txt").read_bytes(),
    )
    validate_runtime_inputs(inputs)
    return inputs


def validate_runtime_inputs(inputs: CoverRuntimeInputs) -> None:
    endpoint = urlsplit(inputs.api_base)
    if endpoint.scheme.lower() != "https" or not endpoint.hostname:
        raise ValueError("cover vision API must use HTTPS")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("cover vision API URL cannot contain credentials or query data")
    if inputs.proxy_url != "http://127.0.0.1:17890":
        raise ValueError("cover proxy must be the isolated localhost proxy")
    for name, value in (
        ("api_key", inputs.api_key),
        ("model", inputs.model),
        ("api_base", inputs.api_base),
    ):
        if not str(value).strip() or any(marker in str(value) for marker in ("\x00", "\r", "\n")):
            raise ValueError(f"{name} is invalid")
    if not inputs.cookie_bytes or len(inputs.cookie_bytes) > MAX_COOKIE_BYTES:
        raise ValueError("cookie file size is invalid")


def build_environment_values(
    inputs: CoverRuntimeInputs,
    *,
    remote_root: str,
) -> dict[str, str]:
    validate_runtime_inputs(inputs)
    root = validate_remote_root(remote_root)
    runtime = root / "shared" / "runtime" / "cover_monitor"
    return {
        "COVER_MONITOR_DB": str(runtime / "cover_monitor_v1.sqlite3"),
        "COVER_MONITOR_ASSET_ROOT": str(runtime / "assets"),
        "COVER_MONITOR_STAGING_ROOT": str(runtime / "staging"),
        "COVER_MONITOR_IMPORT_ROOT": str(runtime / "imports"),
        "COVER_MONITOR_EXPORT_ROOT": str(runtime / "exports"),
        "COVER_MONITOR_STORAGE_BACKEND": "local",
        "COVER_MONITOR_PROXY_URL": inputs.proxy_url,
        "COVER_MONITOR_COOKIE_PATH": str(root / "shared" / "secrets" / "youtube_cookies.txt"),
        "COVER_MONITOR_NETWORK_TIMEOUT_SECONDS": "30",
        "COVER_MONITOR_VISION_API_BASE": inputs.api_base,
        "COVER_MONITOR_VISION_API_KEY": inputs.api_key,
        "COVER_MONITOR_VISION_MODEL": inputs.model,
        "COVER_MONITOR_VISION_TIMEOUT_SECONDS": "90",
        "COVER_MONITOR_WORKER_POLL_SECONDS": "3",
        "COVER_MONITOR_WORKER_MAX_ATTEMPTS": "3",
        "COVER_MONITOR_WORKER_RETRY_SECONDS": "5",
        "COVER_MONITOR_WORKER_HEARTBEAT_SECONDS": "5",
        "COVER_MONITOR_WORKER_STALE_SECONDS": "120",
        "COVER_MONITOR_WORKER_RECOVERY_SWEEP_SECONDS": "15",
    }


def merge_environment_file(existing: bytes, updates: dict[str, str]) -> bytes:
    try:
        original = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("remote environment file is not UTF-8") from exc
    retained = [
        line
        for line in original.splitlines()
        if not _COVER_ENV_LINE.match(line) and line.strip() != _MANAGED_COMMENT
    ]
    while retained and not retained[-1].strip():
        retained.pop()
    rendered = [*retained, "", _MANAGED_COMMENT]
    for key in sorted(updates):
        if re.fullmatch(r"COVER_MONITOR_[A-Z0-9_]+", key) is None:
            raise ValueError("environment key is invalid")
        rendered.append(f"{key}={_encode_environment_value(updates[key])}")
    return ("\n".join(rendered) + "\n").encode("utf-8")


def _encode_environment_value(value: str) -> str:
    text = str(value)
    if any(marker in text for marker in ("\x00", "\r", "\n")):
        raise ValueError("environment value contains a line break")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def configure_runtime(
    remote: Any,
    inputs: CoverRuntimeInputs,
    *,
    remote_root: str,
    execute: bool,
) -> dict[str, object]:
    validate_runtime_inputs(inputs)
    root = validate_remote_root(remote_root)
    environment_path = root / "shared" / "novel-similarity.env"
    cookie_path = root / "shared" / "secrets" / "youtube_cookies.txt"
    existing = remote.read_optional(environment_path)
    if existing is None:
        raise RuntimeError("remote environment file does not exist")
    values = build_environment_values(inputs, remote_root=str(root))
    environment_bytes = merge_environment_file(existing, values)
    api_host = urlsplit(inputs.api_base).hostname or ""
    report: dict[str, object] = {
        "mode": "execute" if execute else "preflight",
        "remote_root": str(root),
        "environment_path": str(environment_path),
        "cookie_path": str(cookie_path),
        "updated_keys": sorted(values),
        "model": inputs.model,
        "api_host": api_host,
        "cookie_bytes": len(inputs.cookie_bytes),
        "cookie_sha256": sha256(inputs.cookie_bytes).hexdigest(),
        "configured": False,
        "services_restarted": False,
    }
    if not execute:
        return report
    remote.apply_runtime_config(
        environment_path=environment_path,
        environment_bytes=environment_bytes,
        cookie_path=cookie_path,
        cookie_bytes=inputs.cookie_bytes,
        runtime_root=root / "shared" / "runtime" / "cover_monitor",
    )
    report["configured"] = True
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

    def read_optional(self, path: PurePosixPath) -> bytes | None:
        try:
            attributes = self._sftp.lstat(str(path))
            if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISREG(attributes.st_mode):
                raise RuntimeError(f"refusing non-regular remote file: {path}")
            with self._sftp.file(str(path), "rb") as stream:
                return stream.read()
        except OSError:
            return None

    def apply_runtime_config(
        self,
        *,
        environment_path: PurePosixPath,
        environment_bytes: bytes,
        cookie_path: PurePosixPath,
        cookie_bytes: bytes,
        runtime_root: PurePosixPath,
    ) -> None:
        old_environment = self.read_optional(environment_path)
        old_cookie = self.read_optional(cookie_path)
        if old_environment is None:
            raise RuntimeError("remote environment file disappeared")
        self._mkdir_tree(cookie_path.parent, mode=0o700)
        self._mkdir_tree(runtime_root, mode=0o700)
        for child in ("assets", "staging", "imports", "exports"):
            self._mkdir_tree(runtime_root / child, mode=0o700)
        self._write_atomic(
            PurePosixPath(str(environment_path) + ".cover-backup"),
            old_environment,
            mode=0o600,
        )
        if old_cookie is not None:
            self._write_atomic(
                PurePosixPath(str(cookie_path) + ".cover-backup"),
                old_cookie,
                mode=0o600,
            )
        try:
            self._write_atomic(cookie_path, cookie_bytes, mode=0o600)
            self._write_atomic(environment_path, environment_bytes, mode=0o600)
        except Exception:
            self._write_atomic(environment_path, old_environment, mode=0o600)
            if old_cookie is None:
                try:
                    self._sftp.remove(str(cookie_path))
                except OSError:
                    pass
            else:
                self._write_atomic(cookie_path, old_cookie, mode=0o600)
            raise

    def _mkdir_tree(self, path: PurePosixPath, *, mode: int) -> None:
        current = PurePosixPath("/")
        for part in path.parts[1:]:
            current /= part
            try:
                attributes = self._sftp.lstat(str(current))
            except OSError:
                self._sftp.mkdir(str(current), mode=mode)
                attributes = self._sftp.lstat(str(current))
            if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISDIR(attributes.st_mode):
                raise RuntimeError(f"refusing unsafe remote directory: {current}")
        self._sftp.chmod(str(path), mode)

    def _write_atomic(self, path: PurePosixPath, content: bytes, *, mode: int) -> None:
        temporary = PurePosixPath(str(path) + f".tmp-{uuid4().hex}")
        try:
            with self._sftp.file(str(temporary), "wb") as stream:
                stream.write(content)
                stream.flush()
            self._sftp.chmod(str(temporary), mode)
            self._sftp.posix_rename(str(temporary), str(path))
        finally:
            try:
                self._sftp.remove(str(temporary))
            except OSError:
                pass


def _ascii_value(lines: list[bytes], line_number: int) -> str:
    if line_number >= len(lines):
        raise ValueError(f"resource file has no line {line_number + 1}")
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no credential value")
    return matches[-1].decode("ascii")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight or atomically configure the cloud cover runtime.",
    )
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--assistant-runtime", type=Path, required=True)
    parser.add_argument("--remote-root", default="/opt/novel-similarity-service")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = load_runtime_inputs(args.assistant_runtime)
    remote = RemoteSession(args.resource_file)
    try:
        report = configure_runtime(
            remote,
            inputs,
            remote_root=args.remote_root,
            execute=bool(args.execute),
        )
    finally:
        remote.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
