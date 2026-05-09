from __future__ import annotations

import argparse

from v2_common import connect_db, now_ts


def iter_windows(text: str, window_size: int, step_size: int) -> list[tuple[int, int, int, str]]:
    text_len = len(text)
    if text_len == 0:
        return []
    if text_len <= window_size:
        return [(0, 0, text_len, text)]

    windows: list[tuple[int, int, int, str]] = []
    window_order = 0
    last_start = text_len - window_size
    start = 0

    while start <= last_start:
        end = start + window_size
        windows.append((window_order, start, end, text[start:end]))
        window_order += 1
        start += step_size

    if windows[-1][1] != last_start:
        windows.append((window_order, last_start, text_len, text[last_start:text_len]))

    return windows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--window-size", type=int, default=200)
    parser.add_argument("--step-size", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=200, help="Chapters per batch")
    parser.add_argument("--limit", type=int, default=0, help="Limit chapters")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.window_size <= 0:
        raise ValueError("--window-size must be > 0")
    if args.step_size <= 0:
        raise ValueError("--step-size must be > 0")
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")

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
    if not args.overwrite:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM evidence_windows ew
                 WHERE ew.chapter_uid = c.chapter_uid
           )
        """
    sql += " ORDER BY c.chapter_uid"
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        cursor = conn.execute(sql, params)
        processed_chapters = 0
        inserted_windows = 0

        while True:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                break

            chapter_ids = [chapter_uid for chapter_uid, _ in rows]
            if args.overwrite:
                conn.executemany(
                    "DELETE FROM evidence_windows WHERE chapter_uid = ?",
                    [(chapter_uid,) for chapter_uid in chapter_ids],
                )

            now = now_ts()
            window_rows: list[tuple[int, int, int, int, str, str]] = []
            for chapter_uid, content_clean in rows:
                windows = iter_windows(content_clean, args.window_size, args.step_size)
                for window_order, start_offset, end_offset, content in windows:
                    window_rows.append(
                        (
                            chapter_uid,
                            window_order,
                            start_offset,
                            end_offset,
                            content,
                            now,
                        )
                    )

            if window_rows:
                conn.executemany(
                    """
                    INSERT INTO evidence_windows(
                        chapter_uid, window_order, start_offset, end_offset, content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chapter_uid, window_order) DO UPDATE SET
                        start_offset=excluded.start_offset,
                        end_offset=excluded.end_offset,
                        content=excluded.content,
                        created_at=excluded.created_at
                    """,
                    window_rows,
                )
                conn.commit()

            processed_chapters += len(rows)
            inserted_windows += len(window_rows)
            print(
                f"PROGRESS processed_chapters={processed_chapters} "
                f"inserted_windows={inserted_windows} "
                f"last_batch_windows={len(window_rows)}"
            )
    finally:
        conn.close()

    print(f"OK processed_chapters={processed_chapters}")
    print(f"OK inserted_windows={inserted_windows}")
    print(f"OK window_size={args.window_size}")
    print(f"OK step_size={args.step_size}")


if __name__ == "__main__":
    main()
