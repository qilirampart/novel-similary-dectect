from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.v2_common import connect_db  # noqa: E402
from scripts.embed_drama_subtitle_windows_v1 import (  # noqa: E402
    DEFAULT_COLLECTION,
    DEFAULT_GITEE_DIMENSIONS,
    DEFAULT_MODEL,
)
from scripts.embed_to_qdrant_v1 import (  # noqa: E402
    DEFAULT_EMBEDDING_BACKEND,
    DEFAULT_OLLAMA_URL,
    DEFAULT_QDRANT_URL,
    embed_texts_by_backend,
)
from scripts.gitee_embedding_client_v1 import (  # noqa: E402
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    resolve_gitee_token,
)
from service.semantic_retrieval import qdrant_search, slice_query_semantic_chunks  # noqa: E402
from service.drama_subtitle_language import SUPPORTED_LANGUAGE_CODES, classify_subtitle_text  # noqa: E402


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_CANDIDATE_LIMIT = 10
DEFAULT_WINDOW_LIMIT = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the dedicated semantic index for drama subtitle windows."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--query", required=True)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--window-limit", type=int, default=DEFAULT_WINDOW_LIMIT)
    parser.add_argument("--book-id", default="", help="Only retain one target drama book id.")
    parser.add_argument("--embedding-backend", default="gitee_api", choices=("ollama", "gitee_api"))
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gitee-endpoint", default=DEFAULT_GITEE_API_ENDPOINT)
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default=DEFAULT_GITEE_TOKEN_ENV)
    parser.add_argument("--gitee-dimensions", type=int, default=DEFAULT_GITEE_DIMENSIONS)
    parser.add_argument("--language-code", default="", help="Optional language override: zh, en, ja or ko.")
    parser.add_argument("--query-window-size", type=int, default=800)
    parser.add_argument("--query-overlap-size", type=int, default=200)
    parser.add_argument("--include-window-text", action="store_true")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def fetch_evidence_rows(
    db_path: Path,
    window_uids: list[str],
    *,
    include_window_text: bool,
) -> dict[str, dict[str, Any]]:
    if not window_uids:
        return {}
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        placeholders = ",".join("?" for _ in window_uids)
        rows = conn.execute(
            f"""
            SELECT window_uid,
                   line_start,
                   line_end,
                   time_start,
                   time_end,
                   window_text_preview,
                   line_count,
                   char_count,
                   window_text
              FROM drama_subtitle_windows
             WHERE window_uid IN ({placeholders})
            """,
            window_uids,
        ).fetchall()
    finally:
        conn.close()

    evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = {
            "window_uid": row[0],
            "line_start": row[1],
            "line_end": row[2],
            "time_start": row[3],
            "time_end": row[4],
            "window_text_preview": row[5],
            "line_count": row[6],
            "char_count": row[7],
        }
        if include_window_text:
            item["window_text"] = row[8]
        evidence[str(row[0])] = item
    return evidence


def candidate_from_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    payload = hit.get("payload")
    if not isinstance(payload, dict):
        return None
    window_uid = str(payload.get("window_uid") or "")
    book_id = str(payload.get("book_id") or "")
    if not window_uid or not book_id:
        return None
    try:
        episode_order = int(payload.get("episode_order"))
    except (TypeError, ValueError):
        return None
    return {
        "window_uid": window_uid,
        "book_id": book_id,
        "book_name": str(payload.get("book_name") or ""),
        "episode_uid": str(payload.get("episode_uid") or ""),
        "episode_order": episode_order,
        "language_code": str(payload.get("language_code") or "unknown"),
        "semantic_score": float(hit.get("score") or 0.0),
        "payload": payload,
    }


def aggregate_hits(
    hits: list[dict[str, Any]],
    evidence_by_uid: dict[str, dict[str, Any]],
    *,
    candidate_limit: int,
    book_id_filter: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    seen_windows: dict[tuple[str, int], set[str]] = defaultdict(set)
    for hit in hits:
        parsed = candidate_from_hit(hit)
        if parsed is None or (book_id_filter and parsed["book_id"] != book_id_filter):
            continue
        key = (parsed["book_id"], parsed["episode_order"])
        candidate = grouped.get(key)
        if candidate is None:
            candidate = {
                "book_id": parsed["book_id"],
                "book_name": parsed["book_name"],
                "episode_uid": parsed["episode_uid"],
                "episode_order": parsed["episode_order"],
                "language_code": parsed["language_code"],
                "retrieved_window_count": 0,
                "semantic_score": parsed["semantic_score"],
                "evidence": evidence_by_uid.get(parsed["window_uid"], {
                    "window_uid": parsed["window_uid"],
                    "window_text_preview": str(parsed["payload"].get("text_preview") or ""),
                }),
            }
            grouped[key] = candidate
        if parsed["window_uid"] not in seen_windows[key]:
            seen_windows[key].add(parsed["window_uid"])
            candidate["retrieved_window_count"] += 1
        if parsed["semantic_score"] > candidate["semantic_score"]:
            candidate["semantic_score"] = parsed["semantic_score"]
            candidate["evidence"] = evidence_by_uid.get(parsed["window_uid"], {
                "window_uid": parsed["window_uid"],
                "window_text_preview": str(parsed["payload"].get("text_preview") or ""),
            })

    candidates = list(grouped.values())
    candidates.sort(
        key=lambda item: (
            -item["semantic_score"],
            -item["retrieved_window_count"],
            item["book_id"],
            item["episode_order"],
        )
    )
    for rank, candidate in enumerate(candidates[:candidate_limit], start=1):
        candidate["rank"] = rank
    return candidates[:candidate_limit]


def main() -> None:
    args = parse_args()
    if args.limit <= 0 or args.window_limit <= 0 or args.gitee_dimensions <= 0:
        raise ValueError("--limit, --window-limit and --gitee-dimensions must be > 0")
    if not args.query.strip():
        raise ValueError("--query must contain non-whitespace text")
    detected_language = classify_subtitle_text(args.query)
    requested_language = args.language_code.strip().lower()
    if requested_language and requested_language not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"unsupported --language-code: {requested_language}")
    effective_language = requested_language or detected_language.language_code
    if effective_language not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(
            "semantic subtitle search requires a supported query language; "
            "pass --language-code for zh, en, ja or ko."
        )

    db_path = resolve_path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    gitee_token = ""
    if args.embedding_backend == "gitee_api":
        try:
            gitee_token = resolve_gitee_token(args.gitee_token, args.gitee_token_env)
        except GiteeEmbeddingError as exc:
            raise ValueError(str(exc)) from exc

    query_chunks = slice_query_semantic_chunks(
        query_text=args.query,
        window_size=args.query_window_size,
        overlap_size=args.query_overlap_size,
    )
    vectors = embed_texts_by_backend(
        backend=args.embedding_backend,
        ollama_url=args.ollama_url,
        model=args.model,
        texts=[str(chunk["text"]) for chunk in query_chunks],
        gitee_endpoint=args.gitee_endpoint,
        gitee_token=gitee_token,
        gitee_dimensions=args.gitee_dimensions,
    )
    raw_hits: list[dict[str, Any]] = []
    for query_chunk, vector in zip(query_chunks, vectors):
        hits = qdrant_search(
            qdrant_url=args.qdrant_url,
            collection=args.collection,
            vector=vector,
            limit=args.window_limit,
            timeout_seconds=30,
            query_filter={
                "must": [
                    {
                        "key": "language_code",
                        "match": {"value": effective_language},
                    }
                ]
            },
        )
        for hit in hits:
            item = dict(hit)
            item["query_chunk_order"] = int(query_chunk["chunk_order"])
            raw_hits.append(item)

    window_uids = sorted(
        {
            str((hit.get("payload") or {}).get("window_uid") or "")
            for hit in raw_hits
        }
        - {""}
    )
    evidence_by_uid = fetch_evidence_rows(
        db_path,
        window_uids,
        include_window_text=args.include_window_text,
    )
    candidates = aggregate_hits(
        raw_hits,
        evidence_by_uid,
        candidate_limit=args.limit,
        book_id_filter=args.book_id.strip(),
    )
    print(
        json.dumps(
            {
                "mode": "semantic_qdrant",
                "query_text": args.query.strip(),
                "collection": args.collection,
                "query_language_code": detected_language.language_code,
                "language_filter": effective_language,
                "query_chunk_count": len(query_chunks),
                "window_hit_count": len(raw_hits),
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
