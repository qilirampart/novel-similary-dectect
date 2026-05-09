from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from v2_common import connect_db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", required=True)
    parser.add_argument("--limit-groups", type=int, default=200)
    parser.add_argument(
        "--csv-out",
        default="data_samples/quality_review/self_short_exact_dedup_groups_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/quality_review/self_short_exact_dedup_summary_v1.json",
    )
    args = parser.parse_args()

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        rows = conn.execute(
            """
            SELECT g.group_uid,
                   g.dataset_key,
                   g.content_sha256,
                   g.canonical_chapter_uid,
                   g.member_count,
                   m.chapter_uid,
                   m.is_canonical,
                   b.book_ext_id,
                   b.book_name,
                   c.chapter_ext_id,
                   c.chapter_name,
                   c.chapter_order,
                   c.raw_file_path,
                   c.object_key,
                   c.raw_file_size
              FROM chapter_exact_dedup_groups g
              JOIN chapter_exact_dedup_members m
                ON m.group_uid = g.group_uid
              JOIN chapters c
                ON c.chapter_uid = m.chapter_uid
              JOIN books b
                ON b.book_uid = c.book_uid
             WHERE g.dataset_key = ?
             ORDER BY g.member_count DESC, g.group_uid ASC, m.is_canonical DESC, m.chapter_uid ASC
             LIMIT ?
            """,
            (args.dataset_key, args.limit_groups * 20),
        ).fetchall()

        summary = conn.execute(
            """
            SELECT count(*) AS group_count,
                   COALESCE(sum(member_count), 0) AS member_total,
                   COALESCE(max(member_count), 0) AS max_group_size
              FROM chapter_exact_dedup_groups
             WHERE dataset_key = ?
            """,
            (args.dataset_key,),
        ).fetchone()

        distinct_books = conn.execute(
            """
            SELECT count(DISTINCT c.book_uid)
              FROM chapter_exact_dedup_members m
              JOIN chapters c ON c.chapter_uid = m.chapter_uid
             WHERE m.dataset_key = ?
            """,
            (args.dataset_key,),
        ).fetchone()[0]
    finally:
        conn.close()

    csv_rows = [
        {
            "group_uid": row[0],
            "dataset_key": row[1],
            "content_sha256": row[2],
            "canonical_chapter_uid": row[3],
            "member_count": row[4],
            "chapter_uid": row[5],
            "is_canonical": row[6],
            "book_ext_id": row[7],
            "book_name": row[8],
            "chapter_ext_id": row[9],
            "chapter_name": row[10],
            "chapter_order": row[11],
            "raw_file_path": row[12],
            "object_key": row[13],
            "raw_file_size": row[14],
        }
        for row in rows
    ]

    summary_payload = {
        "dataset_key": args.dataset_key,
        "group_count": int(summary[0]),
        "member_total": int(summary[1]),
        "max_group_size": int(summary[2]),
        "distinct_books_in_groups": int(distinct_books),
        "csv_preview_rows": len(csv_rows),
    }

    csv_out_path = Path(args.csv_out)
    csv_out_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "group_uid",
                "dataset_key",
                "content_sha256",
                "canonical_chapter_uid",
                "member_count",
                "chapter_uid",
                "is_canonical",
                "book_ext_id",
                "book_name",
                "chapter_ext_id",
                "chapter_name",
                "chapter_order",
                "raw_file_path",
                "object_key",
                "raw_file_size",
            ],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    json_out_path = Path(args.json_out)
    json_out_path.parent.mkdir(parents=True, exist_ok=True)
    json_out_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK dataset_key={args.dataset_key}")
    print(f"OK group_count={summary_payload['group_count']}")
    print(f"OK member_total={summary_payload['member_total']}")
    print(f"OK max_group_size={summary_payload['max_group_size']}")
    print(f"OK distinct_books_in_groups={summary_payload['distinct_books_in_groups']}")
    print(f"OK csv_out={csv_out_path}")
    print(f"OK json_out={json_out_path}")


if __name__ == "__main__":
    main()
