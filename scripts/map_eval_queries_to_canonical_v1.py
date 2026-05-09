from __future__ import annotations

import argparse
import csv
from pathlib import Path

from v2_common import connect_db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_v1.csv",
    )
    parser.add_argument(
        "--out",
        default="data_samples/retrieval_eval/self_short_eval_queries_canonical_v1.csv",
    )
    args = parser.parse_args()

    queries_path = Path(args.queries_csv)
    with queries_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("queries csv is empty")

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        out_rows = []
        remapped_count = 0
        for row in rows:
            query_chapter_uid = int(row["chapter_uid"])
            hit = conn.execute(
                """
                SELECT g.canonical_chapter_uid, m.is_canonical
                  FROM chapter_exact_dedup_members m
                  JOIN chapter_exact_dedup_groups g
                    ON g.group_uid = m.group_uid
                 WHERE m.chapter_uid = ?
                """,
                (query_chapter_uid,),
            ).fetchone()

            canonical_chapter_uid = query_chapter_uid
            is_noncanonical_query = 0
            if hit:
                canonical_chapter_uid = int(hit[0])
                is_noncanonical_query = 0 if int(hit[1]) == 1 else 1
                if canonical_chapter_uid != query_chapter_uid:
                    remapped_count += 1

            out_row = dict(row)
            out_row["target_chapter_uid"] = str(canonical_chapter_uid)
            out_row["is_noncanonical_query"] = str(is_noncanonical_query)
            out_rows.append(out_row)
    finally:
        conn.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) + ["target_chapter_uid", "is_noncanonical_query"]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"OK total_queries={len(out_rows)}")
    print(f"OK remapped_to_canonical={remapped_count}")
    print(f"OK out={out_path}")


if __name__ == "__main__":
    main()
