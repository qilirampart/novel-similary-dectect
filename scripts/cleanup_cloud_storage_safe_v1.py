from __future__ import annotations

import argparse
import json
import posixpath
import re
import stat
import time
from pathlib import Path, PurePosixPath

import paramiko


PROJECT_ROOT = PurePosixPath("/opt/novel-similarity-service")

PART_DIRECTORIES = {
    "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3.parts":
        "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3",
    "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_first7_20260904.sqlite3.parts":
        "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_first7_20260904.sqlite3",
    "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1.sqlite3.parts":
        "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1.sqlite3",
    "/opt/novel-similarity-service/shared/qdrant_snapshots/drama_subtitle_window_embeddings_qwen3_4b_2560_v1-20260903.snapshot.parts":
        "/opt/novel-similarity-service/shared/qdrant_snapshots/drama_subtitle_window_embeddings_qwen3_4b_2560_v1-20260903.snapshot",
}

FILES = [
    "/opt/novel-similarity-service/shared/data/novel_similarity_v2_pipeline_smoketest.sqlite3",
]

RELEASE_DIRECTORIES = [
    "/opt/novel-similarity-service/releases/20260515-review-responsive-sync",
]


def _ascii_value(resource_file: Path, line_number: int) -> str:
    lines = resource_file.read_bytes().splitlines()
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no ASCII value")
    return matches[-1].decode("ascii")


def _connect(resource_file: Path) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=_ascii_value(resource_file, 2),
        username=_ascii_value(resource_file, 3),
        password=_ascii_value(resource_file, 4),
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(15)
    return client


def _run(client: paramiko.SSHClient, command: str) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=120)
    status = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace").strip()
    error = stderr.read().decode("utf-8", errors="replace").strip()
    if status != 0:
        raise RuntimeError(f"command failed ({status}): {error or output}")
    return output


def _assert_allowed(path: str) -> None:
    candidate = PurePosixPath(path)
    if candidate == PROJECT_ROOT or PROJECT_ROOT not in candidate.parents:
        raise ValueError(f"path escapes project root: {path}")


def _tree_size(sftp: paramiko.SFTPClient, path: str) -> int:
    attrs = sftp.lstat(path)
    if stat.S_ISLNK(attrs.st_mode):
        raise ValueError(f"symlink is not allowed in cleanup tree: {path}")
    if stat.S_ISREG(attrs.st_mode):
        return attrs.st_size
    if not stat.S_ISDIR(attrs.st_mode):
        raise ValueError(f"unsupported file type: {path}")
    total = 0
    for entry in sftp.listdir_attr(path):
        total += _tree_size(sftp, posixpath.join(path, entry.filename))
    return total


def _tree_latest_mtime(sftp: paramiko.SFTPClient, path: str) -> int:
    attrs = sftp.lstat(path)
    latest = int(attrs.st_mtime)
    if stat.S_ISDIR(attrs.st_mode):
        for entry in sftp.listdir_attr(path):
            latest = max(
                latest,
                _tree_latest_mtime(sftp, posixpath.join(path, entry.filename)),
            )
    return latest


def _remove_tree(sftp: paramiko.SFTPClient, path: str) -> None:
    attrs = sftp.lstat(path)
    if stat.S_ISLNK(attrs.st_mode):
        raise ValueError(f"refusing to remove symlink: {path}")
    if stat.S_ISREG(attrs.st_mode):
        sftp.remove(path)
        return
    if not stat.S_ISDIR(attrs.st_mode):
        raise ValueError(f"unsupported file type: {path}")
    for entry in sftp.listdir_attr(path):
        _remove_tree(sftp, posixpath.join(path, entry.filename))
    sftp.rmdir(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely remove verified cloud transfer remnants.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    all_paths = [*PART_DIRECTORIES, *FILES, *RELEASE_DIRECTORIES]
    for path in all_paths:
        _assert_allowed(path)

    client = _connect(args.resource_file)
    report: dict[str, object] = {"mode": "execute" if args.execute else "dry-run"}
    try:
        sftp = client.open_sftp()
        current_release = _run(
            client, "readlink -f /opt/novel-similarity-service/current"
        )
        if current_release in RELEASE_DIRECTORIES:
            raise RuntimeError("cleanup release is the current release")

        configured_paths = _run(
            client,
            "(grep -E '^(NOVEL_SIMILARITY_DB|NOVEL_SIMILARITY_BUSINESS_DB|"
            "DRAMA_SUBTITLE_SIMILARITY_DB)=' "
            "/opt/novel-similarity-service/shared/novel-similarity.env 2>/dev/null; "
            "systemctl show novel-similarity-api.service --property=Environment --value) "
            "| grep -oE '/opt/novel-similarity-service/[^ ]+' || true",
        )
        referenced = set(configured_paths.splitlines())

        verified: list[dict[str, object]] = []
        for parts_path, assembled_path in PART_DIRECTORIES.items():
            parts_size = _tree_size(sftp, parts_path)
            assembled_size = sftp.stat(assembled_path).st_size
            latest_mtime = _tree_latest_mtime(sftp, parts_path)
            reason = "parts_match_assembled_file"
            if parts_size != assembled_size and not (
                0 < parts_size < assembled_size
                and latest_mtime < int(time.time()) - 48 * 60 * 60
            ):
                raise RuntimeError(
                    f"part size mismatch: {parts_path}={parts_size}, "
                    f"{assembled_path}={assembled_size}"
                )
            if parts_size != assembled_size:
                reason = "stale_incomplete_parts_with_assembled_file"
            verified.append(
                {
                    "path": parts_path,
                    "bytes": parts_size,
                    "latest_mtime": latest_mtime,
                    "reason": reason,
                }
            )

        for path in FILES:
            if path in referenced:
                raise RuntimeError(f"configured file cannot be removed: {path}")
            verified.append(
                {"path": path, "bytes": _tree_size(sftp, path), "reason": "smoketest_only"}
            )

        for path in RELEASE_DIRECTORIES:
            verified.append(
                {
                    "path": path,
                    "bytes": _tree_size(sftp, path),
                    "reason": "not_current_release",
                }
            )

        open_files = _run(
            client,
            "lsof +D /opt/novel-similarity-service 2>/dev/null || true",
        )
        open_matches = [
            line for line in open_files.splitlines() if any(path in line for path in all_paths)
        ]
        if open_matches:
            raise RuntimeError(
                "cleanup candidate is open by a process: " + "\n".join(open_matches)
            )

        report["current_release"] = current_release
        report["verified"] = verified
        report["total_bytes"] = sum(int(item["bytes"]) for item in verified)

        if args.execute:
            for path in all_paths:
                _remove_tree(sftp, path)
            report["removed"] = all_paths
        sftp.close()
    finally:
        client.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
