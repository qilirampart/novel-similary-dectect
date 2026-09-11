from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.drama_subtitle_language import classify_subtitle_text, ensure_language_columns
from v2_common import connect_db


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_OUTPUT_ROOT = "data_samples/middle_platform_subtitles_20260722_language"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill language labels for normalized drama subtitle lines and windows."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--force", action="store_true", help="Reclassify rows that already have a language label.")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def classify_table(
    conn: Any,
    *,
    table_name: str,
    id_column: str,
    text_column: str,
    batch_size: int,
    force: bool,
) -> tuple[int, Counter[str]]:
    where_sql = "" if force else "WHERE language_code IS NULL OR language_code = '' OR language_code = 'unknown'"
    cursor = conn.execute(
        f"SELECT {id_column}, {text_column} FROM {table_name} {where_sql} ORDER BY {id_column}"
    )
    updated = 0
    counts: Counter[str] = Counter()
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        values: list[tuple[object, ...]] = []
        for row_id, text in rows:
            prediction = classify_subtitle_text(str(text or ""))
            values.append((prediction.language_code, prediction.confidence, row_id))
            counts[prediction.language_code] += 1
        conn.executemany(
            f"UPDATE {table_name} SET language_code = ?, language_confidence = ? WHERE {id_column} = ?",
            values,
        )
        conn.commit()
        updated += len(values)
        print(f"PROGRESS table={table_name} updated={updated}")
    return updated, counts


def count_languages(conn: Any, table_name: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT language_code, COUNT(*) FROM {table_name} GROUP BY language_code ORDER BY language_code"
    ).fetchall()
    return {str(language_code): int(count) for language_code, count in rows}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    db_path = resolve_path(args.db)
    output_root = resolve_path(args.output_root)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")

    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        ensure_language_columns(conn)
        conn.commit()
        started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        line_updated, line_counts = classify_table(
            conn,
            table_name="drama_subtitle_lines",
            id_column="line_uid",
            text_column="text_normalized",
            batch_size=args.batch_size,
            force=args.force,
        )
        window_updated, window_counts = classify_table(
            conn,
            table_name="drama_subtitle_windows",
            id_column="window_uid",
            text_column="window_text",
            batch_size=args.batch_size,
            force=args.force,
        )
        payload = {
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "db_path": str(db_path),
            "force": int(args.force),
            "line_updated": line_updated,
            "window_updated": window_updated,
            "line_updated_language_counts": dict(sorted(line_counts.items())),
            "window_updated_language_counts": dict(sorted(window_counts.items())),
            "line_language_counts": count_languages(conn, "drama_subtitle_lines"),
            "window_language_counts": count_languages(conn, "drama_subtitle_windows"),
        }
    finally:
        conn.close()

    output_path = output_root / "logs" / (
        "language_backfill_summary_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"OK summary_path={output_path}")


if __name__ == "__main__":
    main()
