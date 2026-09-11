from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from v2_common import connect_db


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_BASE_SCHEMA_PATH = "service/drama_subtitle_schema_v1.sql"
DEFAULT_WINDOW_SCHEMA_PATH = "service/drama_subtitle_windows_schema_v1.sql"
DEFAULT_FTS_SCHEMA_PATH = "service/drama_subtitle_lexical_schema_v1.sql"
DEFAULT_OUTPUT_ROOT = "data_samples/middle_platform_subtitles_20260722_phase3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build lexical FTS index for drama subtitle windows.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--base-schema", default=DEFAULT_BASE_SCHEMA_PATH)
    parser.add_argument("--window-schema", default=DEFAULT_WINDOW_SCHEMA_PATH)
    parser.add_argument("--fts-schema", default=DEFAULT_FTS_SCHEMA_PATH)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--book-id", default="", help="Only index one target book id.")
    parser.add_argument("--book-ids-file", default="", help="Only index newline-delimited target book ids.")
    parser.add_argument("--overwrite", action="store_true", help="Clear existing FTS rows first.")
    parser.add_argument(
        "--build-language-v2",
        action="store_true",
        help="Build the full language-aware V2 FTS tables and mark them ready after count validation.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_book_ids_file(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    }


def write_summary_json(path: Path, payload: dict[str, object]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    db_path = resolve_path(args.db)
    base_schema_path = resolve_path(args.base_schema)
    window_schema_path = resolve_path(args.window_schema)
    fts_schema_path = resolve_path(args.fts_schema)
    output_root = resolve_path(args.output_root)
    ensure_dir(output_root)

    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    for schema_path in (base_schema_path, window_schema_path, fts_schema_path):
        if not schema_path.exists():
            raise FileNotFoundError(f"schema not found: {schema_path}")

    book_id_file = resolve_path(args.book_ids_file) if args.book_ids_file.strip() else None
    if args.book_id.strip() and book_id_file:
        raise ValueError("--book-id and --book-ids-file cannot be used together")
    if args.build_language_v2 and (args.book_id.strip() or book_id_file):
        raise ValueError("--build-language-v2 requires the complete subtitle corpus, not a book filter")
    if book_id_file and not book_id_file.exists():
        raise FileNotFoundError(f"book ids file not found: {book_id_file}")
    target_book_ids = load_book_ids_file(book_id_file) if book_id_file else set()

    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(load_sql(base_schema_path))
        conn.executescript(load_sql(window_schema_path))
        conn.executescript(load_sql(fts_schema_path))
        if target_book_ids:
            conn.execute("CREATE TEMP TABLE temp_target_book_ids (book_id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO temp_target_book_ids(book_id) VALUES (?)",
                [(book_id,) for book_id in sorted(target_book_ids)],
            )
            conn.commit()

        started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        if args.overwrite and not args.build_language_v2:
            conn.execute("DELETE FROM drama_subtitle_windows_fts")
            conn.execute("DELETE FROM drama_subtitle_windows_word_fts")
            conn.commit()

        params: list[object] = []
        where_sql = ""
        if args.book_id.strip():
            where_sql = "WHERE w.book_id = ?"
            params.append(args.book_id.strip())
        elif target_book_ids:
            where_sql = "WHERE w.book_id IN (SELECT book_id FROM temp_target_book_ids)"

        source_window_count = conn.execute(
            f"SELECT COUNT(*) FROM drama_subtitle_windows w {where_sql}",
            params,
        ).fetchone()[0]

        if not args.build_language_v2:
            conn.execute("BEGIN")
            if args.book_id.strip():
                conn.execute(
                    "DELETE FROM drama_subtitle_windows_fts WHERE book_id = ?",
                    (args.book_id.strip(),),
                )
                conn.execute(
                    "DELETE FROM drama_subtitle_windows_word_fts WHERE book_id = ?",
                    (args.book_id.strip(),),
                )
            elif target_book_ids:
                conn.execute(
                    "DELETE FROM drama_subtitle_windows_fts WHERE book_id IN (SELECT book_id FROM temp_target_book_ids)"
                )
                conn.execute(
                    "DELETE FROM drama_subtitle_windows_word_fts WHERE book_id IN (SELECT book_id FROM temp_target_book_ids)"
                )
            conn.execute(
                f"""
                INSERT INTO drama_subtitle_windows_fts(
                    window_uid,
                    book_id,
                    book_name,
                    episode_uid,
                    window_text
                )
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.window_text
                  FROM drama_subtitle_windows w
                  {where_sql}
                """,
                params,
            )
            conn.execute(
                f"""
                INSERT INTO drama_subtitle_windows_word_fts(
                    window_uid,
                    book_id,
                    book_name,
                    episode_uid,
                    window_text
                )
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.window_text
                  FROM drama_subtitle_windows w
                  {where_sql}
                """,
                params,
            )
            conn.commit()

        if args.build_language_v2:
            indexed_row_count = 0
            word_indexed_row_count = 0
        else:
            indexed_row_count = conn.execute(
                f"SELECT COUNT(*) FROM drama_subtitle_windows_fts f "
                f"{'WHERE f.book_id = ?' if args.book_id.strip() else 'WHERE f.book_id IN (SELECT book_id FROM temp_target_book_ids)' if target_book_ids else ''}",
                params,
            ).fetchone()[0]
            word_indexed_row_count = conn.execute(
                f"SELECT COUNT(*) FROM drama_subtitle_windows_word_fts f "
                f"{'WHERE f.book_id = ?' if args.book_id.strip() else 'WHERE f.book_id IN (SELECT book_id FROM temp_target_book_ids)' if target_book_ids else ''}",
                params,
            ).fetchone()[0]

        language_v2_counts: dict[str, int] = {}
        if args.build_language_v2:
            v2_tables = (
                "drama_subtitle_windows_fts_lang_v2",
                "drama_subtitle_windows_word_fts_lang_v2",
            )
            conn.execute(
                """
                INSERT INTO drama_subtitle_lexical_index_metadata(
                    index_name, source_window_count, indexed_window_count, build_status, updated_at
                )
                VALUES (?, ?, 0, 'building', ?)
                ON CONFLICT(index_name) DO UPDATE SET
                    source_window_count = excluded.source_window_count,
                    indexed_window_count = 0,
                    build_status = 'building',
                    updated_at = excluded.updated_at
                """,
                (v2_tables[0], int(source_window_count), started_at),
            )
            conn.execute(
                """
                INSERT INTO drama_subtitle_lexical_index_metadata(
                    index_name, source_window_count, indexed_window_count, build_status, updated_at
                )
                VALUES (?, ?, 0, 'building', ?)
                ON CONFLICT(index_name) DO UPDATE SET
                    source_window_count = excluded.source_window_count,
                    indexed_window_count = 0,
                    build_status = 'building',
                    updated_at = excluded.updated_at
                """,
                (v2_tables[1], int(source_window_count), started_at),
            )
            conn.commit()
            for table_name in v2_tables:
                conn.execute(f"DELETE FROM {table_name}")
            conn.commit()
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO drama_subtitle_windows_fts_lang_v2(window_uid, language_code, window_text)
                SELECT w.window_uid, 'lang_' || lower(w.language_code), w.window_text
                  FROM drama_subtitle_windows w
                """
            )
            conn.execute(
                """
                INSERT INTO drama_subtitle_windows_word_fts_lang_v2(window_uid, language_code, window_text)
                SELECT w.window_uid, 'lang_' || lower(w.language_code), w.window_text
                  FROM drama_subtitle_windows w
                """
            )
            conn.commit()
            language_v2_counts = {
                table_name: int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
                for table_name in v2_tables
            }
            if any(count != int(source_window_count) for count in language_v2_counts.values()):
                raise RuntimeError(
                    f"language V2 count mismatch: source={source_window_count}, indexed={language_v2_counts}"
                )
            finished_for_metadata = datetime.now().isoformat(sep=" ", timespec="seconds")
            conn.executemany(
                """
                UPDATE drama_subtitle_lexical_index_metadata
                   SET indexed_window_count = ?, build_status = 'ready', updated_at = ?
                 WHERE index_name = ?
                """,
                [
                    (count, finished_for_metadata, table_name)
                    for table_name, count in language_v2_counts.items()
                ],
            )
            conn.commit()

        finished_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_json_path = output_root / "logs" / f"phase3_lexical_index_summary_{run_stamp}.json"
        write_summary_json(
            summary_json_path,
            {
                "started_at": started_at,
                "finished_at": finished_at,
                "db_path": str(db_path),
                "book_filter": args.book_id.strip(),
                "book_ids_file": str(book_id_file) if book_id_file else "",
                "target_book_id_count": len(target_book_ids),
                "overwrite": int(args.overwrite),
                "source_window_count": int(source_window_count),
                "indexed_row_count": int(indexed_row_count),
                "word_indexed_row_count": int(word_indexed_row_count),
                "language_v2_enabled": int(args.build_language_v2),
                "language_v2_indexed_counts": language_v2_counts,
            },
        )
    finally:
        conn.close()

    print(f"OK source_window_count={int(source_window_count)}")
    print(f"OK indexed_row_count={int(indexed_row_count)}")
    print(f"OK word_indexed_row_count={int(word_indexed_row_count)}")
    if language_v2_counts:
        print(f"OK language_v2_indexed_counts={language_v2_counts}")
    print(f"OK db_path={db_path}")


if __name__ == "__main__":
    main()
