from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import json
from pathlib import Path
from pathlib import PurePosixPath
import shlex
import time

from install_cloud_cover_worker_v1 import RemoteSession


ROOT_DIR = Path(__file__).resolve().parents[1]
RESOURCE_FILE = ROOT_DIR / ".codex" / "测试环境资源清单.md"
REMOTE_ROOT = PurePosixPath("/opt/novel-similarity-service")
REMOTE_RUNTIME = REMOTE_ROOT / "shared" / "runtime" / "cover_monitor"
REMOTE_DB = REMOTE_RUNTIME / "cover_monitor_v1.sqlite3"
REMOTE_PYTHON = REMOTE_ROOT / "shared" / "venv" / "bin" / "python"
REMOTE_CURRENT = REMOTE_ROOT / "current"


REMOTE_CODE = r"""
import json
from pathlib import Path
import sqlite3
import sys
import time

current_root, db_path, source_path, source_name, digest = sys.argv[1:]
sys.path.insert(0, current_root)
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore

with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT workspace_key, created_by_user_id FROM cover_import_batches ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT workspace_key, created_by_user_id FROM cover_runs ORDER BY created_at LIMIT 1"
        ).fetchone()
if row is None:
    raise RuntimeError("cannot determine cover workspace")

store = CoverMonitorStore(db_path)
scope = CoverAccessScope(
    workspace_key=str(row["workspace_key"]),
    user_id=int(row["created_by_user_id"]),
)
started = time.perf_counter()
preview = store.create_import_preview(
    scope,
    import_kind="baseline",
    source_file_name=source_name,
    source_file_sha256=digest,
    source_file_path=source_path,
)
preview_seconds = time.perf_counter() - started
confirm_started = time.perf_counter()
confirmed = store.confirm_import(scope, preview["import_id"])
result = {
    "import_id": confirmed["import_id"],
    "status": confirmed["status"],
    "sheet_name": confirmed.get("sheet_name"),
    "stats": confirmed.get("stats", {}),
    "preview_seconds": round(preview_seconds, 3),
    "confirm_seconds": round(time.perf_counter() - confirm_started, 3),
}
print(json.dumps(result, ensure_ascii=False, indent=2))
"""


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _upload(remote: RemoteSession, source: Path, target: str) -> None:
    total = source.stat().st_size
    transferred = 0
    next_report = 10
    temporary = f"{target}.part"
    with source.open("rb") as input_stream, remote._sftp.file(temporary, "wb") as output_stream:
        while chunk := input_stream.read(1024 * 1024):
            output_stream.write(chunk)
            transferred += len(chunk)
            percent = int(transferred * 100 / total)
            if percent >= next_report or transferred == total:
                print(json.dumps({"upload_percent": percent}, ensure_ascii=False), flush=True)
                next_report = percent + 10
        output_stream.flush()
    remote._sftp.posix_rename(temporary, target)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import the historical cover baseline into cloud storage.")
    parser.add_argument("source", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        raise ValueError(f"valid .xlsx source required: {source}")
    digest = _digest(source)
    remote_source = str(REMOTE_RUNTIME / "imports" / f"baseline-{digest[:16]}.xlsx")
    remote = RemoteSession(RESOURCE_FILE)
    try:
        remote.run(f"mkdir -p {shlex.quote(str(REMOTE_RUNTIME / 'imports'))}")
        backup = str(REMOTE_RUNTIME / f"cover_monitor_v1.pre-import-{int(time.time())}.sqlite3")
        remote.run(
            f"test -f {shlex.quote(str(REMOTE_DB))} && "
            f"sqlite3 {shlex.quote(str(REMOTE_DB))} "
            f"\".backup '{backup}'\""
        )
        print(json.dumps({"backup": backup}, ensure_ascii=False), flush=True)
        _upload(remote, source, remote_source)
        encoded = base64.b64encode(REMOTE_CODE.encode("utf-8")).decode("ascii")
        bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
        command = " ".join(
            shlex.quote(value)
            for value in (
                str(REMOTE_PYTHON),
                "-c",
                bootstrap,
                str(REMOTE_CURRENT),
                str(REMOTE_DB),
                remote_source,
                source.name,
                digest,
            )
        )
        print(remote.run(command, timeout=600))
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
