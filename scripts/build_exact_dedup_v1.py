from __future__ import annotations

import argparse

from v2_common import connect_db, now_ts


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chapter_exact_dedup_groups (
            group_uid INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            canonical_chapter_uid INTEGER NOT NULL,
            member_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (dataset_key, content_sha256),
            FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key),
            FOREIGN KEY (canonical_chapter_uid) REFERENCES chapters (chapter_uid)
        );

        CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_groups_dataset
            ON chapter_exact_dedup_groups (dataset_key, member_count);

        CREATE TABLE IF NOT EXISTS chapter_exact_dedup_members (
            chapter_uid INTEGER PRIMARY KEY,
            group_uid INTEGER NOT NULL,
            dataset_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            is_canonical INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (group_uid) REFERENCES chapter_exact_dedup_groups (group_uid),
            FOREIGN KEY (chapter_uid) REFERENCES chapters (chapter_uid),
            FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
        );

        CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_members_group
            ON chapter_exact_dedup_members (group_uid, is_canonical);

        CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_members_dataset
            ON chapter_exact_dedup_members (dataset_key, is_canonical, chapter_uid);
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", required=True)
    parser.add_argument(
        "--prefer-order",
        choices=["chapter_uid", "book_then_chapter"],
        default="book_then_chapter",
        help="How to choose canonical chapter within one exact-duplicate group",
    )
    args = parser.parse_args()

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        ensure_tables(conn)
        now = now_ts()

        conn.execute("DELETE FROM chapter_exact_dedup_members WHERE dataset_key = ?", (args.dataset_key,))
        conn.execute("DELETE FROM chapter_exact_dedup_groups WHERE dataset_key = ?", (args.dataset_key,))
        conn.commit()

        if args.prefer_order == "book_then_chapter":
            order_expr = "b.book_ext_id ASC, c.chapter_order ASC, c.chapter_ext_id ASC, c.chapter_uid ASC"
        else:
            order_expr = "c.chapter_uid ASC"

        groups = conn.execute(
            f"""
            SELECT c.content_sha256,
                   min(c.chapter_uid) AS fallback_canonical,
                   count(*) AS member_count
              FROM chapters c
             WHERE c.dataset_key = ?
               AND c.content_sha256 IS NOT NULL
             GROUP BY c.content_sha256
            HAVING count(*) >= 2
             ORDER BY member_count DESC, fallback_canonical ASC
            """,
            (args.dataset_key,),
        ).fetchall()

        group_rows = []
        member_rows = []
        duplicate_chapter_count = 0

        for content_sha256, fallback_canonical, member_count in groups:
            canonical_row = conn.execute(
                f"""
                SELECT c.chapter_uid
                  FROM chapters c
                  JOIN books b ON b.book_uid = c.book_uid
                 WHERE c.dataset_key = ?
                   AND c.content_sha256 = ?
                 ORDER BY {order_expr}
                 LIMIT 1
                """,
                (args.dataset_key, content_sha256),
            ).fetchone()
            canonical_chapter_uid = int(canonical_row[0] if canonical_row else fallback_canonical)

            group_rows.append(
                (
                    args.dataset_key,
                    content_sha256,
                    canonical_chapter_uid,
                    int(member_count),
                    now,
                    now,
                )
            )

        if group_rows:
            conn.executemany(
                """
                INSERT INTO chapter_exact_dedup_groups(
                    dataset_key, content_sha256, canonical_chapter_uid, member_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                group_rows,
            )
            conn.commit()

            for dataset_key, content_sha256, canonical_chapter_uid, member_count, _, _ in group_rows:
                duplicate_chapter_count += int(member_count)
                group_uid = conn.execute(
                    """
                    SELECT group_uid
                      FROM chapter_exact_dedup_groups
                     WHERE dataset_key = ?
                       AND content_sha256 = ?
                    """,
                    (dataset_key, content_sha256),
                ).fetchone()[0]
                rows = conn.execute(
                    """
                    SELECT chapter_uid
                      FROM chapters
                     WHERE dataset_key = ?
                       AND content_sha256 = ?
                     ORDER BY chapter_uid
                    """,
                    (dataset_key, content_sha256),
                ).fetchall()
                for (chapter_uid,) in rows:
                    member_rows.append(
                        (
                            int(chapter_uid),
                            int(group_uid),
                            dataset_key,
                            content_sha256,
                            1 if int(chapter_uid) == int(canonical_chapter_uid) else 0,
                            now,
                            now,
                        )
                    )

            conn.executemany(
                """
                INSERT INTO chapter_exact_dedup_members(
                    chapter_uid, group_uid, dataset_key, content_sha256, is_canonical, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                member_rows,
            )
            conn.commit()

        unique_chapter_count = conn.execute(
            """
            SELECT count(*)
              FROM chapters
             WHERE dataset_key = ?
               AND content_sha256 IS NOT NULL
               AND chapter_uid NOT IN (
                   SELECT chapter_uid
                     FROM chapter_exact_dedup_members
                    WHERE dataset_key = ?
                      AND is_canonical = 0
               )
            """,
            (args.dataset_key, args.dataset_key),
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"OK dataset_key={args.dataset_key}")
    print(f"OK duplicate_groups={len(group_rows)}")
    print(f"OK duplicate_chapters={duplicate_chapter_count}")
    print(f"OK canonical_chapters_after_dedup={unique_chapter_count}")
    print(f"OK prefer_order={args.prefer_order}")


if __name__ == "__main__":
    main()
