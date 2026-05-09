from __future__ import annotations

import argparse
import re

from retrieve_candidates_v1 import normalize_scoring_text
from v2_common import connect_db, now_ts


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_ident(value: str) -> str:
    if not IDENT_RE.match(value):
        raise ValueError(f"Invalid SQLite identifier: {value!r}")
    return value


def ensure_fts_table(conn, table_name: str) -> None:
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table_name}
        USING fts5(
            content_retrieval,
            tokenize='trigram',
            content=''
        )
        """
    )


def ensure_fts_table_with_detail(conn, table_name: str, detail: str) -> None:
    if detail not in {"full", "column", "none"}:
        raise ValueError(f"Unsupported FTS detail level: {detail}")

    detail_sql = "" if detail == "full" else f", detail='{detail}'"
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table_name}
        USING fts5(
            content_retrieval,
            tokenize='trigram',
            content=''
            {detail_sql}
        )
        """
    )


def ensure_fts_meta_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_fts_indexes (
            table_name TEXT PRIMARY KEY,
            dataset_key TEXT,
            detail TEXT NOT NULL,
            source_column TEXT NOT NULL,
            text_normalized INTEGER NOT NULL DEFAULT 0,
            row_count INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--table", default="chapter_fts_v2")
    parser.add_argument(
        "--detail",
        default="column",
        choices=["full", "column", "none"],
        help="FTS detail level. column keeps BM25 while reducing index size.",
    )
    parser.add_argument("--dataset-key", default="")
    parser.add_argument(
        "--canonical-only",
        action="store_true",
        help="Index only canonical chapters after exact dedup for the selected dataset",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-index already indexed chapters in the selected scope",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop and recreate the whole FTS table before indexing",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Run the FTS optimize command after indexing",
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    if args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.rebuild and args.dataset_key:
        raise ValueError("--rebuild cannot be combined with --dataset-key")

    table_name = validate_ident(args.table)

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA temp_store = MEMORY")
        ensure_fts_meta_table(conn)
        if args.rebuild:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        ensure_fts_table_with_detail(conn, table_name, args.detail)

        sql = f"""
            SELECT c.chapter_uid, cc.content_retrieval
              FROM chapters c
              JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
             WHERE cc.content_retrieval IS NOT NULL
               AND cc.content_retrieval != ''
        """
        params: list[object] = []
        if args.dataset_key:
            sql += " AND c.dataset_key = ?"
            params.append(args.dataset_key)
        if args.canonical_only:
            sql += """
               AND NOT EXISTS (
                    SELECT 1
                      FROM chapter_exact_dedup_members dm
                     WHERE dm.chapter_uid = c.chapter_uid
                       AND dm.is_canonical = 0
               )
            """
        if not args.overwrite:
            sql += f"""
               AND NOT EXISTS (
                    SELECT 1
                      FROM {table_name} f
                     WHERE f.rowid = c.chapter_uid
               )
            """
        sql += " ORDER BY c.chapter_uid"
        if args.limit > 0:
            sql += f" LIMIT {args.limit}"

        cursor = conn.execute(sql, params)
        processed_rows = 0
        inserted_rows = 0

        while True:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                break

            fts_rows = [
                (chapter_uid, normalize_scoring_text(content_retrieval))
                for chapter_uid, content_retrieval in rows
            ]
            fts_rows = [(chapter_uid, text) for chapter_uid, text in fts_rows if text]
            if not fts_rows:
                processed_rows += len(rows)
                print(
                    f"PROGRESS processed_rows={processed_rows} "
                    f"inserted_rows={inserted_rows} "
                    f"last_batch=0"
                )
                continue

            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {table_name}(rowid, content_retrieval)
                VALUES (?, ?)
                """,
                fts_rows,
            )
            conn.commit()

            processed_rows += len(rows)
            inserted_rows += len(fts_rows)
            print(
                f"PROGRESS processed_rows={processed_rows} "
                f"inserted_rows={inserted_rows} "
                f"last_batch={len(fts_rows)}"
            )

        if args.optimize:
            conn.execute(f"INSERT INTO {table_name}({table_name}) VALUES('optimize')")
            conn.commit()
            print("OK optimize=done")

        indexed_total = conn.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
        now = now_ts()
        created_at = conn.execute(
            """
            SELECT created_at
              FROM retrieval_fts_indexes
             WHERE table_name = ?
            """,
            (table_name,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO retrieval_fts_indexes(
                table_name, dataset_key, detail, source_column, text_normalized, row_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_name) DO UPDATE SET
                dataset_key=excluded.dataset_key,
                detail=excluded.detail,
                source_column=excluded.source_column,
                text_normalized=excluded.text_normalized,
                row_count=excluded.row_count,
                updated_at=excluded.updated_at
            """,
            (
                table_name,
                args.dataset_key or None,
                args.detail,
                "content_retrieval_canonical_only" if args.canonical_only else "content_retrieval",
                1,
                indexed_total,
                created_at[0] if created_at else now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"OK table={table_name}")
    print(f"OK detail={args.detail}")
    print(f"OK canonical_only={1 if args.canonical_only else 0}")
    print(f"OK processed_rows={processed_rows}")
    print(f"OK inserted_rows={inserted_rows}")
    print(f"OK indexed_total={indexed_total}")


if __name__ == "__main__":
    main()
