from __future__ import annotations

import argparse

from v2_common import connect_db, now_ts


DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-8B"
DEFAULT_EMBEDDING_DIM = 1024


def iter_chunks(text: str, window_size: int, overlap_size: int) -> list[tuple[int, int, int, str]]:
    if window_size <= 0:
        raise ValueError("window_size must be > 0")
    if overlap_size < 0:
        raise ValueError("overlap_size must be >= 0")
    if overlap_size >= window_size:
        raise ValueError("overlap_size must be < window_size")

    text_len = len(text)
    if text_len == 0:
        return []
    if text_len <= window_size:
        return [(0, 0, text_len, text)]

    step_size = window_size - overlap_size
    chunks: list[tuple[int, int, int, str]] = []
    chunk_order = 0
    last_start = text_len - window_size
    start = 0

    while start <= last_start:
        end = start + window_size
        chunks.append((chunk_order, start, end, text[start:end]))
        chunk_order += 1
        start += step_size

    if chunks[-1][1] != last_start:
        chunks.append((chunk_order, last_start, text_len, text[last_start:text_len]))

    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--canonical-only", action="store_true")
    parser.add_argument("--max-chapter-order", type=int, default=0)
    parser.add_argument("--window-size", type=int, default=800)
    parser.add_argument("--overlap-size", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=200, help="Chapters per batch")
    parser.add_argument("--limit", type=int, default=0, help="Limit chapters")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--embedding-dim", type=int, default=DEFAULT_EMBEDDING_DIM)
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    if args.embedding_dim <= 0:
        raise ValueError("--embedding-dim must be > 0")
    if args.max_chapter_order < 0:
        raise ValueError("--max-chapter-order must be >= 0")

    sql = """
        SELECT c.chapter_uid, cc.content_clean
          FROM chapters c
          JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
         WHERE cc.content_clean IS NOT NULL
           AND cc.content_clean != ''
    """
    params: list[object] = []
    if args.dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(args.dataset_key)
    if args.max_chapter_order > 0:
        sql += " AND c.chapter_order <= ?"
        params.append(args.max_chapter_order)
    if args.canonical_only:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
           )
        """
    if not args.overwrite:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM semantic_chunks sc
                 WHERE sc.chapter_uid = c.chapter_uid
           )
        """
    sql += " ORDER BY c.chapter_uid"
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        cursor = conn.execute(sql, params)
        processed_chapters = 0
        inserted_chunks = 0

        while True:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                break

            chapter_ids = [chapter_uid for chapter_uid, _ in rows]
            if args.overwrite:
                conn.executemany(
                    "DELETE FROM semantic_chunks WHERE chapter_uid = ?",
                    [(chapter_uid,) for chapter_uid in chapter_ids],
                )

            now = now_ts()
            chunk_rows: list[tuple[int, int, int, int, str, str, int, str]] = []
            for chapter_uid, content_clean in rows:
                chunks = iter_chunks(content_clean, args.window_size, args.overlap_size)
                for chunk_order, start_offset, end_offset, content in chunks:
                    chunk_rows.append(
                        (
                            chapter_uid,
                            chunk_order,
                            start_offset,
                            end_offset,
                            content,
                            args.embedding_model,
                            args.embedding_dim,
                            now,
                        )
                    )

            if chunk_rows:
                conn.executemany(
                    """
                    INSERT INTO semantic_chunks(
                        chapter_uid,
                        chunk_order,
                        start_offset,
                        end_offset,
                        content,
                        embedding_model,
                        embedding_dim,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chapter_uid, chunk_order) DO UPDATE SET
                        start_offset=excluded.start_offset,
                        end_offset=excluded.end_offset,
                        content=excluded.content,
                        embedding_model=excluded.embedding_model,
                        embedding_dim=excluded.embedding_dim,
                        created_at=excluded.created_at
                    """,
                    chunk_rows,
                )
                conn.commit()

            processed_chapters += len(rows)
            inserted_chunks += len(chunk_rows)
            print(
                f"PROGRESS processed_chapters={processed_chapters} "
                f"inserted_chunks={inserted_chunks} "
                f"last_batch_chunks={len(chunk_rows)}"
            )
    finally:
        conn.close()

    print(f"OK processed_chapters={processed_chapters}")
    print(f"OK inserted_chunks={inserted_chunks}")
    print(f"OK window_size={args.window_size}")
    print(f"OK overlap_size={args.overlap_size}")
    print(f"OK embedding_model={args.embedding_model}")
    print(f"OK embedding_dim={args.embedding_dim}")
    print(f"OK canonical_only={int(args.canonical_only)}")
    print(f"OK max_chapter_order={args.max_chapter_order}")


if __name__ == "__main__":
    main()
