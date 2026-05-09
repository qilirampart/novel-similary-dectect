from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from v2_common import connect_db, now_ts


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def retrieval_text(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def abnormal_repetition(text: str) -> int:
    return 1 if re.search(r"(.)\1{29,}", text) else 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        sql = """
            SELECT c.chapter_uid, c.raw_file_path
              FROM chapters c
              LEFT JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
             WHERE c.raw_file_path IS NOT NULL
        """
        params: list[object] = []
        if args.dataset_key:
            sql += " AND c.dataset_key = ?"
            params.append(args.dataset_key)
        if not args.overwrite:
            sql += " AND cc.chapter_uid IS NULL"
        sql += " ORDER BY c.chapter_uid"
        if args.limit > 0:
            sql += f" LIMIT {args.limit}"

        cursor = conn.execute(sql, params)
        loaded_total = 0
        missing_files = 0

        while True:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                break

            now = now_ts()
            content_rows = []
            chapter_rows = []

            for chapter_uid, raw_file_path in rows:
                path = Path(raw_file_path)
                if not path.exists():
                    missing_files += 1
                    continue
                data = path.read_bytes()
                raw = decode_text(data)
                clean = clean_text(raw)
                retrieval = retrieval_text(clean)
                char_count_raw = len(raw)
                char_count_clean = len(clean)
                is_empty = 1 if not clean else 0
                is_too_short = 1 if 0 < char_count_clean < 20 else 0
                repeat_flag = abnormal_repetition(clean)
                sha = sha256_bytes(data)

                content_rows.append(
                    (
                        chapter_uid,
                        raw,
                        clean,
                        retrieval,
                        now,
                        now,
                    )
                )
                chapter_rows.append(
                    (
                        sha,
                        char_count_raw,
                        char_count_clean,
                        is_empty,
                        is_too_short,
                        repeat_flag,
                        now,
                        chapter_uid,
                    )
                )

            if content_rows:
                conn.executemany(
                    """
                    INSERT INTO chapter_contents(
                        chapter_uid, content_raw, content_clean, content_retrieval, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chapter_uid) DO UPDATE SET
                        content_raw=excluded.content_raw,
                        content_clean=excluded.content_clean,
                        content_retrieval=excluded.content_retrieval,
                        updated_at=excluded.updated_at
                    """,
                    content_rows,
                )
                conn.executemany(
                    """
                    UPDATE chapters
                       SET content_sha256 = ?,
                           char_count_raw = ?,
                           char_count_clean = ?,
                           is_empty = ?,
                           is_too_short = ?,
                           has_abnormal_repetition = ?,
                           updated_at = ?
                     WHERE chapter_uid = ?
                    """,
                    chapter_rows,
                )
                conn.commit()

            loaded_total += len(content_rows)
            print(
                f"PROGRESS loaded_total={loaded_total} "
                f"last_batch_loaded={len(content_rows)} "
                f"missing_files={missing_files}"
            )
    finally:
        conn.close()

    print(f"OK loaded_chapters={loaded_total}")
    print(f"OK missing_files={missing_files}")


if __name__ == "__main__":
    main()
