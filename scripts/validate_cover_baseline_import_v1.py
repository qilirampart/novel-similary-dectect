from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate cover baseline import against an isolated database.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if args.db.exists():
        raise FileExistsError(f"validation database already exists: {args.db}")

    store = CoverMonitorStore(args.db)
    scope = CoverAccessScope(workspace_key="validation", user_id=1)
    started = time.perf_counter()
    preview = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=source.name,
        source_file_sha256=f"validation-{source.stat().st_size}-{source.stat().st_mtime_ns}",
        source_file_path=str(source),
    )
    preview_seconds = time.perf_counter() - started
    result = {
        "source": str(source),
        "database": str(args.db.resolve()),
        "preview_seconds": round(preview_seconds, 3),
        "preview": preview,
    }
    if args.confirm:
        confirm_started = time.perf_counter()
        confirmed = store.confirm_import(scope, preview["import_id"])
        result["confirm_seconds"] = round(time.perf_counter() - confirm_started, 3)
        result["confirmed"] = confirmed
        result["overview"] = store.get_overview(scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
