from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from v2_common import connect_db


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_LIMIT = 10
WHITESPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search drama subtitle windows with the lexical trigram FTS index.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--book-id", default="", help="Only search one target book id.")
    parser.add_argument("--max-grams", type=int, default=80, help="Cap generated trigrams in the MATCH expression.")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def normalize_query(text: str) -> str:
    normalized = str(text or "").replace("\u3000", " ").replace("\xa0", " ").strip()
    normalized = WHITESPACE_RE.sub(" ", normalized)
    return normalized


def build_match_query(query_text: str, *, max_grams: int) -> tuple[str | None, str]:
    normalized = normalize_query(query_text)
    compact = normalized.replace(" ", "")
    if len(compact) < 3:
        return None, normalized

    grams: list[str] = []
    seen: set[str] = set()
    for index in range(0, len(compact) - 2):
        gram = compact[index : index + 3]
        if gram in seen:
            continue
        seen.add(gram)
        grams.append(gram)
        if len(grams) >= max_grams:
            break
    if not grams:
        return None, normalized

    escaped_grams = ['"' + gram.replace('"', '""') + '"' for gram in grams]
    expression = " OR ".join(escaped_grams)
    return expression, normalized


def main() -> None:
    args = parse_args()
    db_path = resolve_path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    if args.limit <= 0:
        raise ValueError("--limit must be > 0")
    if args.max_grams <= 0:
        raise ValueError("--max-grams must be > 0")

    match_query, normalized_query = build_match_query(args.query, max_grams=args.max_grams)
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        if match_query is None:
            rows = conn.execute(
                """
                SELECT w.book_id,
                       w.book_name,
                       w.episode_order,
                       w.line_start,
                       w.line_end,
                       w.time_start,
                       w.time_end,
                       w.window_text_preview,
                       w.char_count
                  FROM drama_subtitle_windows w
                 WHERE w.window_text LIKE ?
                   AND (? = '' OR w.book_id = ?)
                 ORDER BY w.char_count DESC, w.book_id, w.episode_order, w.line_start
                 LIMIT ?
                """,
                (f"%{normalized_query}%", args.book_id.strip(), args.book_id.strip(), args.limit),
            ).fetchall()
            payload = {
                "mode": "like_fallback",
                "query_text": normalized_query,
                "results": [
                    {
                        "book_id": row[0],
                        "book_name": row[1],
                        "episode_order": row[2],
                        "line_start": row[3],
                        "line_end": row[4],
                        "time_start": row[5],
                        "time_end": row[6],
                        "window_text_preview": row[7],
                        "char_count": row[8],
                    }
                    for row in rows
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        rows = conn.execute(
            """
            SELECT w.book_id,
                   w.book_name,
                   w.episode_order,
                   w.line_start,
                   w.line_end,
                   w.time_start,
                   w.time_end,
                   w.window_text_preview,
                   w.char_count,
                   bm25(drama_subtitle_windows_fts) AS lexical_score
              FROM drama_subtitle_windows_fts f
              JOIN drama_subtitle_windows w ON w.window_uid = f.window_uid
             WHERE f.window_text MATCH ?
               AND (? = '' OR w.book_id = ?)
             ORDER BY lexical_score ASC, w.book_id, w.episode_order, w.line_start
             LIMIT ?
            """,
            (match_query, args.book_id.strip(), args.book_id.strip(), args.limit),
        ).fetchall()

        payload = {
            "mode": "fts5_trigram",
            "query_text": normalized_query,
            "match_query": match_query,
            "results": [
                {
                    "book_id": row[0],
                    "book_name": row[1],
                    "episode_order": row[2],
                    "line_start": row[3],
                    "line_end": row[4],
                    "time_start": row[5],
                    "time_end": row[6],
                    "window_text_preview": row[7],
                    "char_count": row[8],
                    "lexical_score": row[9],
                }
                for row in rows
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
