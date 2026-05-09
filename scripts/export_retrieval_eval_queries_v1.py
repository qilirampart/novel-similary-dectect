from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

from retrieve_candidates_v1 import normalize_scoring_text
from v2_common import connect_db


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.eval_query_modes import (
    default_eval_queries_out_path,
    get_eval_query_preset,
    list_eval_query_modes,
)


def choose_query_start(text: str, query_len: int, skip_prefix: int) -> int:
    if len(text) <= query_len:
        return 0
    max_start = len(text) - query_len
    return min(max(skip_prefix, len(text) // 3), max_start)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="self_short_novels")
    parser.add_argument(
        "--eval-mode",
        default="reuse",
        choices=list_eval_query_modes(),
        help="Which evaluation query preset to use",
    )
    parser.add_argument("--sample-limit", type=int, default=200)
    parser.add_argument("--min-chapter-uid", type=int, default=0)
    parser.add_argument("--max-chapter-uid", type=int, default=0)
    parser.add_argument("--min-chars", type=int, default=0)
    parser.add_argument("--query-len", type=int, default=0)
    parser.add_argument("--skip-prefix", type=int, default=-1)
    parser.add_argument(
        "--out",
        default="",
    )
    args = parser.parse_args()

    preset = get_eval_query_preset(args.eval_mode)
    min_chars = args.min_chars if args.min_chars > 0 else preset.min_chars
    query_len = args.query_len if args.query_len > 0 else preset.query_len
    skip_prefix = args.skip_prefix if args.skip_prefix >= 0 else preset.skip_prefix
    out_path = args.out or default_eval_queries_out_path(
        dataset_key=args.dataset_key,
        eval_mode=args.eval_mode,
        query_len=query_len,
    )

    if args.sample_limit <= 0:
        raise ValueError("--sample-limit must be > 0")
    if min_chars <= 0:
        raise ValueError("--min-chars must be > 0")
    if args.min_chapter_uid < 0:
        raise ValueError("--min-chapter-uid must be >= 0")
    if args.max_chapter_uid < 0:
        raise ValueError("--max-chapter-uid must be >= 0")
    if args.max_chapter_uid > 0 and args.min_chapter_uid > args.max_chapter_uid:
        raise ValueError("--min-chapter-uid must be <= --max-chapter-uid")
    if query_len <= 0:
        raise ValueError("--query-len must be > 0")
    if skip_prefix < 0:
        raise ValueError("--skip-prefix must be >= 0")

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        where_sql = """
            WHERE c.dataset_key = ?
              AND cc.content_retrieval IS NOT NULL
              AND cc.content_retrieval != ''
              AND c.char_count_clean >= ?
        """
        params: list[object] = [args.dataset_key, min_chars]
        if args.min_chapter_uid > 0:
            where_sql += " AND c.chapter_uid >= ?"
            params.append(args.min_chapter_uid)
        if args.max_chapter_uid > 0:
            where_sql += " AND c.chapter_uid <= ?"
            params.append(args.max_chapter_uid)

        eligible_count = conn.execute(
            f"""
            SELECT count(*)
              FROM chapters c
              JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
              {where_sql}
            """,
            params,
        ).fetchone()[0]
        if eligible_count == 0:
            raise ValueError("No eligible chapters found for eval query export")

        step = max(1, eligible_count // args.sample_limit)
        cursor = conn.execute(
            f"""
            SELECT c.chapter_uid,
                   c.dataset_key,
                   b.book_ext_id,
                   b.book_name,
                   c.chapter_ext_id,
                   c.chapter_name,
                   c.char_count_clean,
                   cc.content_retrieval
              FROM chapters c
              JOIN books b ON b.book_uid = c.book_uid
              JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
              {where_sql}
             ORDER BY c.chapter_uid
            """,
            params,
        )

        rows_out: list[dict[str, object]] = []
        eligible_idx = 0
        query_id = 1
        for (
            chapter_uid,
            dataset_key,
            book_ext_id,
            book_name,
            chapter_ext_id,
            chapter_name,
            char_count_clean,
            content_retrieval,
        ) in cursor:
            if eligible_idx % step != 0:
                eligible_idx += 1
                continue

            query_start = choose_query_start(content_retrieval, query_len, skip_prefix)
            query_text = content_retrieval[query_start : query_start + query_len]
            if len(normalize_scoring_text(query_text)) < 3:
                eligible_idx += 1
                continue

            rows_out.append(
                {
                    "query_id": query_id,
                    "dataset_key": dataset_key,
                    "chapter_uid": chapter_uid,
                    "book_ext_id": book_ext_id,
                    "book_name": book_name,
                    "chapter_ext_id": chapter_ext_id,
                    "chapter_name": chapter_name,
                    "char_count_clean": char_count_clean,
                    "eval_mode": args.eval_mode,
                    "query_start": query_start,
                    "query_len": len(query_text),
                    "query_text": query_text,
                }
            )
            query_id += 1
            eligible_idx += 1
            if len(rows_out) >= args.sample_limit:
                break

        if not rows_out:
            raise ValueError("No queries exported after snippet filtering")
    finally:
        conn.close()

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "dataset_key",
                "chapter_uid",
                "book_ext_id",
                "book_name",
                "chapter_ext_id",
                "chapter_name",
                "char_count_clean",
                "eval_mode",
                "query_start",
                "query_len",
                "query_text",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"OK eligible_count={eligible_count}")
    print(f"OK sample_step={step}")
    print(f"OK exported_queries={len(rows_out)}")
    print(f"OK eval_mode={args.eval_mode}")
    print(f"OK query_len={query_len}")
    print(f"OK min_chars={min_chars}")
    print(f"OK skip_prefix={skip_prefix}")
    print(f"OK min_chapter_uid={args.min_chapter_uid}")
    print(f"OK max_chapter_uid={args.max_chapter_uid}")
    print(f"OK out={path}")


if __name__ == "__main__":
    main()
