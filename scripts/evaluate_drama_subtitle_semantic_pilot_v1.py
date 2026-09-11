from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from v2_common import connect_db


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.embed_drama_subtitle_windows_v1 import (  # noqa: E402
    DEFAULT_COLLECTION,
    DEFAULT_GITEE_DIMENSIONS,
    DEFAULT_MODEL,
)
from scripts.embed_to_qdrant_v1 import (  # noqa: E402
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
from service.drama_subtitle_language import SUPPORTED_LANGUAGE_CODES  # noqa: E402
from service.semantic_retrieval import qdrant_search  # noqa: E402


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_OUTPUT_PATH = "docs/semantic_pilot_eval_latest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate same-language semantic subtitle retrieval on already embedded windows."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gitee-dimensions", type=int, default=DEFAULT_GITEE_DIMENSIONS)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--book-id", default="")
    parser.add_argument("--language-code", default="")
    parser.add_argument("--embedding-backend", default="gitee_api", choices=("ollama", "gitee_api"))
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--gitee-endpoint", default=DEFAULT_GITEE_API_ENDPOINT)
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default=DEFAULT_GITEE_TOKEN_ENV)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def alternating_line_excerpt(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return " ".join(lines)
    excerpt = lines[::2]
    return "\n".join(excerpt if len(excerpt) >= 3 else lines[: max(3, len(lines) // 2)])


def load_samples(
    db_path: Path,
    *,
    collection: str,
    model: str,
    vector_size: int,
    limit: int,
    book_id: str,
    language_code: str,
) -> list[dict[str, Any]]:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        sql = """
            SELECT w.window_uid,
                   w.book_id,
                   w.book_name,
                   w.episode_order,
                   w.language_code,
                   w.window_text
              FROM drama_subtitle_window_embedding_sync_state s
              JOIN drama_subtitle_windows w
                ON w.window_uid = s.window_uid
             WHERE s.collection_name = ?
               AND s.embedding_model = ?
               AND s.embedding_dim = ?
               AND w.language_code IN ('zh', 'en', 'ja', 'ko')
        """
        params: list[object] = [collection, model, vector_size]
        if book_id:
            sql += " AND w.book_id = ?"
            params.append(book_id)
        if language_code:
            sql += " AND w.language_code = ?"
            params.append(language_code)
        # Read a bounded pool, then round-robin books below so the pilot does
        # not overrepresent the first book IDs in the database.
        sql += " ORDER BY w.book_id, w.episode_order, w.line_start LIMIT ?"
        params.append(limit * 12)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = [
        {
            "window_uid": str(row[0]),
            "book_id": str(row[1]),
            "book_name": str(row[2]),
            "episode_order": int(row[3]),
            "language_code": str(row[4]),
            "window_text": str(row[5]),
        }
        for row in rows
    ]
    by_book: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_book.setdefault(item["book_id"], []).append(item)
    selected: list[dict[str, Any]] = []
    positions = {book_id: 0 for book_id in by_book}
    while len(selected) < limit:
        added = False
        for book_id, book_items in by_book.items():
            position = positions[book_id]
            if position >= len(book_items):
                continue
            selected.append(book_items[position])
            positions[book_id] += 1
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


def compact_hit(hit: dict[str, Any]) -> dict[str, Any]:
    payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
    return {
        "book_id": str(payload.get("book_id") or ""),
        "book_name": str(payload.get("book_name") or ""),
        "episode_order": int(payload.get("episode_order") or 0),
        "language_code": str(payload.get("language_code") or "unknown"),
        "window_uid": str(payload.get("window_uid") or ""),
        "score": round(float(hit.get("score") or 0.0), 6),
    }


def main() -> None:
    args = parse_args()
    if args.limit <= 0 or args.top_k <= 0 or args.gitee_dimensions <= 0:
        raise ValueError("--limit, --top-k and --gitee-dimensions must be > 0")
    language_code = args.language_code.strip().lower()
    if language_code and language_code not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"unsupported --language-code: {language_code}")
    db_path = resolve_path(args.db)
    output_path = resolve_path(args.output)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")

    token = ""
    if args.embedding_backend == "gitee_api":
        try:
            token = resolve_gitee_token(args.gitee_token, args.gitee_token_env)
        except GiteeEmbeddingError as exc:
            raise ValueError(str(exc)) from exc

    samples = load_samples(
        db_path,
        collection=args.collection,
        model=args.model,
        vector_size=args.gitee_dimensions,
        limit=args.limit,
        book_id=args.book_id.strip(),
        language_code=language_code,
    )
    if not samples:
        raise ValueError("no embedded subtitle windows match the requested pilot scope")

    queries = [alternating_line_excerpt(item["window_text"]) for item in samples]
    vectors = embed_texts_by_backend(
        backend=args.embedding_backend,
        ollama_url=args.ollama_url,
        model=args.model,
        texts=queries,
        gitee_endpoint=args.gitee_endpoint,
        gitee_token=token,
        gitee_dimensions=args.gitee_dimensions,
    )

    results: list[dict[str, Any]] = []
    for sample, query_text, vector in zip(samples, queries, vectors):
        hits = qdrant_search(
            qdrant_url=args.qdrant_url,
            collection=args.collection,
            vector=vector,
            limit=args.top_k,
            timeout_seconds=30,
            query_filter={
                "must": [
                    {
                        "key": "language_code",
                        "match": {"value": sample["language_code"]},
                    }
                ]
            },
        )
        compact_hits = [compact_hit(dict(hit)) for hit in hits]
        top1 = compact_hits[0] if compact_hits else {}
        results.append(
            {
                "source_window_uid": sample["window_uid"],
                "source_book_id": sample["book_id"],
                "source_book_name": sample["book_name"],
                "source_episode_order": sample["episode_order"],
                "language_code": sample["language_code"],
                "query_char_count": len(query_text),
                "top1_same_episode": bool(
                    top1
                    and top1["book_id"] == sample["book_id"]
                    and top1["episode_order"] == sample["episode_order"]
                ),
                "topk_same_episode": any(
                    hit["book_id"] == sample["book_id"]
                    and hit["episode_order"] == sample["episode_order"]
                    for hit in compact_hits
                ),
                "cross_language_hit_count": sum(
                    hit["language_code"] != sample["language_code"] for hit in compact_hits
                ),
                "top_hits": compact_hits,
            }
        )

    total = len(results)
    payload = {
        "collection": args.collection,
        "embedding_model": args.model,
        "embedding_dim": args.gitee_dimensions,
        "sample_count": total,
        "query_variant": "alternating_subtitle_lines",
        "top1_same_episode_rate": round(sum(item["top1_same_episode"] for item in results) / total, 4),
        "topk_same_episode_rate": round(sum(item["topk_same_episode"] for item in results) / total, 4),
        "cross_language_hit_count": sum(item["cross_language_hit_count"] for item in results),
        "language_counts": dict(Counter(item["language_code"] for item in results)),
        "book_count": len({item["source_book_id"] for item in results}),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "collection",
                    "embedding_model",
                    "embedding_dim",
                    "sample_count",
                    "top1_same_episode_rate",
                    "topk_same_episode_rate",
                    "cross_language_hit_count",
                    "language_counts",
                    "book_count",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
