from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from v2_common import connect_db

REPEAT_PATTERN = re.compile(r"(.)\1{29,}")


def issue_types(is_empty: int, is_too_short: int, has_abnormal_repetition: int) -> str:
    issues: list[str] = []
    if is_empty:
        issues.append("empty")
    if is_too_short:
        issues.append("too_short")
    if has_abnormal_repetition:
        issues.append("abnormal_repetition")
    return ";".join(issues)


def build_excerpt(text: str, has_abnormal_repetition: int, width: int = 60) -> tuple[str, str, int]:
    if not text:
        return "", "", 0

    if has_abnormal_repetition:
        match = REPEAT_PATTERN.search(text)
        if match:
            start = max(0, match.start() - width)
            end = min(len(text), match.end() + width)
            excerpt = text[start:end].replace("\n", "\\n")
            repeated_char = match.group(1)
            repeated_len = match.end() - match.start()
            return excerpt, repeated_char, repeated_len

    excerpt = text[: width * 2].replace("\n", "\\n")
    return excerpt, "", 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--output",
        default="data_samples/quality_review/quality_flag_samples_v2.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    sql = """
        SELECT
            c.dataset_key,
            b.book_ext_id,
            b.book_name,
            c.chapter_ext_id,
            c.chapter_name,
            c.chapter_order,
            c.char_count_clean,
            c.is_empty,
            c.is_too_short,
            c.has_abnormal_repetition,
            c.download_status,
            c.raw_file_path,
            cc.content_clean
          FROM chapters c
          JOIN books b ON b.book_uid = c.book_uid
          LEFT JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
         WHERE c.is_empty = 1
            OR c.is_too_short = 1
            OR c.has_abnormal_repetition = 1
    """
    params: list[object] = []
    if args.dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(args.dataset_key)
    sql += " ORDER BY c.dataset_key, b.book_ext_id, c.chapter_order"
    if args.limit > 0:
        sql += " LIMIT ?"
        params.append(args.limit)

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    empty_count = 0
    too_short_count = 0
    abnormal_repetition_count = 0

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "issue_types",
                "dataset_key",
                "book_ext_id",
                "book_name",
                "chapter_ext_id",
                "chapter_name",
                "chapter_order",
                "char_count_clean",
                "download_status",
                "repeat_char",
                "repeat_len",
                "excerpt",
                "raw_file_path",
            ],
        )
        writer.writeheader()

        for row in rows:
            (
                dataset_key,
                book_ext_id,
                book_name,
                chapter_ext_id,
                chapter_name,
                chapter_order,
                char_count_clean,
                is_empty,
                is_too_short,
                has_abnormal_repetition,
                download_status,
                raw_file_path,
                content_clean,
            ) = row
            issues = issue_types(is_empty, is_too_short, has_abnormal_repetition)
            excerpt, repeat_char, repeat_len = build_excerpt(content_clean or "", has_abnormal_repetition)

            empty_count += int(is_empty)
            too_short_count += int(is_too_short)
            abnormal_repetition_count += int(has_abnormal_repetition)

            writer.writerow(
                {
                    "issue_types": issues,
                    "dataset_key": dataset_key,
                    "book_ext_id": book_ext_id,
                    "book_name": book_name,
                    "chapter_ext_id": chapter_ext_id,
                    "chapter_name": chapter_name,
                    "chapter_order": chapter_order,
                    "char_count_clean": char_count_clean,
                    "download_status": download_status,
                    "repeat_char": repeat_char,
                    "repeat_len": repeat_len,
                    "excerpt": excerpt,
                    "raw_file_path": raw_file_path,
                }
            )

    print(f"OK rows={len(rows)}")
    print(f"OK empty_count={empty_count}")
    print(f"OK too_short_count={too_short_count}")
    print(f"OK abnormal_repetition_count={abnormal_repetition_count}")
    print(f"OK output={output_path}")


if __name__ == "__main__":
    main()
