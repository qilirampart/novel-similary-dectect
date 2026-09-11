from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from v2_common import connect_db


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_CANDIDATE_LIMIT = 10
DEFAULT_WINDOW_LIMIT = 200
WHITESPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search drama subtitle windows, then aggregate hits into drama episode candidates."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument(
        "--window-limit",
        type=int,
        default=DEFAULT_WINDOW_LIMIT,
        help="Maximum window-level lexical hits considered before aggregation.",
    )
    parser.add_argument("--book-id", default="", help="Only search one target book id.")
    parser.add_argument(
        "--max-grams",
        type=int,
        default=80,
        help="Cap generated trigrams in the FTS MATCH expression.",
    )
    parser.add_argument(
        "--include-window-text",
        action="store_true",
        help="Include the complete evidence window instead of only its preview.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def normalize_query(text: str) -> str:
    normalized = str(text or "").replace("\u3000", " ").replace("\xa0", " ").strip()
    return WHITESPACE_RE.sub(" ", normalized)


def build_match_query(query_text: str, *, max_grams: int) -> tuple[str | None, str]:
    normalized = normalize_query(query_text)
    compact = normalized.replace(" ", "")
    if len(compact) < 3:
        return None, normalized

    grams: list[str] = []
    seen: set[str] = set()
    for index in range(len(compact) - 2):
        gram = compact[index : index + 3]
        if gram in seen:
            continue
        seen.add(gram)
        grams.append(gram)
        if len(grams) >= max_grams:
            break

    expression = " OR ".join('"' + gram.replace('"', '""') + '"' for gram in grams)
    return expression or None, normalized


def serialize_evidence(row: tuple[Any, ...], *, include_window_text: bool) -> dict[str, Any]:
    evidence = {
        "window_uid": row[0],
        "line_start": row[5],
        "line_end": row[6],
        "time_start": row[7],
        "time_end": row[8],
        "window_text_preview": row[9],
        "line_count": row[10],
        "char_count": row[11],
    }
    if include_window_text:
        evidence["window_text"] = row[12]
    return evidence


def aggregate_rows(
    rows: list[tuple[Any, ...]],
    *,
    candidate_limit: int,
    include_window_text: bool,
    mode: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row[1]), int(row[4]))
        candidate = grouped.get(key)
        score = float(row[13]) if mode == "fts5_trigram" else None
        if candidate is None:
            candidate = {
                "book_id": row[1],
                "book_name": row[2],
                "episode_uid": row[3],
                "episode_order": row[4],
                "retrieved_window_count": 0,
                "best_lexical_score": score,
                "evidence": serialize_evidence(row, include_window_text=include_window_text),
            }
            grouped[key] = candidate
        candidate["retrieved_window_count"] += 1
        if mode == "fts5_trigram" and score is not None and score < candidate["best_lexical_score"]:
            candidate["best_lexical_score"] = score
            candidate["evidence"] = serialize_evidence(row, include_window_text=include_window_text)

    candidates = list(grouped.values())
    if mode == "fts5_trigram":
        candidates.sort(
            key=lambda item: (
                item["best_lexical_score"],
                -item["retrieved_window_count"],
                item["book_id"],
                item["episode_order"],
            )
        )
    else:
        candidates.sort(
            key=lambda item: (
                -item["retrieved_window_count"],
                item["book_id"],
                item["episode_order"],
            )
        )

    for index, candidate in enumerate(candidates[:candidate_limit], start=1):
        candidate["rank"] = index
    return candidates[:candidate_limit]


def main() -> None:
    args = parse_args()
    db_path = resolve_path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    if args.limit <= 0 or args.window_limit <= 0 or args.max_grams <= 0:
        raise ValueError("--limit, --window-limit and --max-grams must be > 0")

    match_query, normalized_query = build_match_query(args.query, max_grams=args.max_grams)
    if not normalized_query:
        raise ValueError("--query must contain non-whitespace text")

    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        book_id = args.book_id.strip()
        if match_query is None:
            mode = "like_fallback"
            rows = conn.execute(
                """
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.episode_order,
                       w.line_start,
                       w.line_end,
                       w.time_start,
                       w.time_end,
                       w.window_text_preview,
                       w.line_count,
                       w.char_count,
                       w.window_text,
                       NULL AS lexical_score
                  FROM drama_subtitle_windows w
                 WHERE w.window_text LIKE ?
                   AND (? = '' OR w.book_id = ?)
                 ORDER BY w.book_id, w.episode_order, w.line_start
                 LIMIT ?
                """,
                (f"%{normalized_query}%", book_id, book_id, args.window_limit),
            ).fetchall()
        else:
            mode = "fts5_trigram"
            rows = conn.execute(
                """
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.episode_order,
                       w.line_start,
                       w.line_end,
                       w.time_start,
                       w.time_end,
                       w.window_text_preview,
                       w.line_count,
                       w.char_count,
                       w.window_text,
                       bm25(drama_subtitle_windows_fts) AS lexical_score
                  FROM drama_subtitle_windows_fts f
                  JOIN drama_subtitle_windows w ON w.window_uid = f.window_uid
                 WHERE f.window_text MATCH ?
                   AND (? = '' OR w.book_id = ?)
                 ORDER BY lexical_score ASC, w.book_id, w.episode_order, w.line_start
                 LIMIT ?
                """,
                (match_query, book_id, book_id, args.window_limit),
            ).fetchall()

        payload = {
            "mode": mode,
            "query_text": normalized_query,
            "match_query": match_query,
            "window_hit_count": len(rows),
            "candidate_count": 0,
            "candidates": aggregate_rows(
                rows,
                candidate_limit=args.limit,
                include_window_text=args.include_window_text,
                mode=mode,
            ),
        }
        payload["candidate_count"] = len(payload["candidates"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
