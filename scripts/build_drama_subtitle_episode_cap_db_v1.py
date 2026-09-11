from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an isolated first-N-episodes subtitle SQLite corpus.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--max-episode-order", type=int, required=True)
    return parser.parse_args()


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0])


def main() -> int:
    args = parse_args()
    if args.max_episode_order <= 0:
        raise SystemExit("--max-episode-order must be positive")
    source = Path(args.source_db).resolve()
    target = Path(args.target_db).resolve()
    if not source.is_file():
        raise SystemExit(f"source database not found: {source}")
    if target.exists():
        raise SystemExit(f"target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_conn = sqlite3.connect(source)
    try:
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()

    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        before = {name: table_count(conn, name) for name in (
            "drama_episodes", "drama_subtitle_lines", "drama_subtitle_windows",
        )}
        # FTS rows must be rebuilt after trimming, otherwise deleted windows remain searchable.
        conn.execute("DELETE FROM drama_subtitle_windows_fts")
        conn.execute("DELETE FROM drama_subtitle_windows_word_fts")
        conn.execute("DELETE FROM drama_subtitle_window_embedding_sync_state")
        conn.execute("DELETE FROM drama_subtitle_windows WHERE episode_order > ?", (args.max_episode_order,))
        conn.execute("DELETE FROM drama_subtitle_lines WHERE episode_order > ?", (args.max_episode_order,))
        conn.execute("DELETE FROM drama_episodes WHERE episode_order > ?", (args.max_episode_order,))
        conn.execute(
            """INSERT INTO drama_subtitle_windows_fts(window_uid, book_id, book_name, episode_uid, window_text)
               SELECT window_uid, book_id, book_name, episode_uid, window_text FROM drama_subtitle_windows"""
        )
        conn.execute(
            """INSERT INTO drama_subtitle_windows_word_fts(window_uid, book_id, book_name, episode_uid, window_text)
               SELECT window_uid, book_id, book_name, episode_uid, window_text FROM drama_subtitle_windows"""
        )
        # Merge newly inserted FTS segments so a fresh reduced corpus is not
        # benchmarked against a long-lived, already-optimized source index.
        conn.execute("INSERT INTO drama_subtitle_windows_fts(drama_subtitle_windows_fts) VALUES('optimize')")
        conn.execute("INSERT INTO drama_subtitle_windows_word_fts(drama_subtitle_windows_word_fts) VALUES('optimize')")
        conn.commit()
        after = {name: table_count(conn, name) for name in (
            "drama_episodes", "drama_subtitle_lines", "drama_subtitle_windows",
            "drama_subtitle_windows_fts", "drama_subtitle_windows_word_fts",
        )}
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
    finally:
        conn.close()

    vacuum_conn = sqlite3.connect(target)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()
    print({"target_db": str(target), "max_episode_order": args.max_episode_order, "before": before, "after": after, "size_bytes": target.stat().st_size})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
