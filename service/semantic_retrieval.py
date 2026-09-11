from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from urllib import error, request
import json
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_semantic_chunks_v2 import iter_chunks  # noqa: E402
from v2_common import connect_db  # noqa: E402
from scripts.gitee_embedding_client_v1 import (  # noqa: E402
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    gitee_embed_texts,
    resolve_gitee_token,
)


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_CHAPTER_COLLECTION = "novel_chapter_embeddings"
DEFAULT_CHUNK_COLLECTION = "novel_semantic_chunk_embeddings_qwen3_8b_1024_v1"
DEFAULT_REMOTE_EMBEDDING_BACKEND = "gitee_api"


class SemanticRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemanticRetrievalConfig:
    backend: str = "remote"
    remote_embedding_backend: str = DEFAULT_REMOTE_EMBEDDING_BACKEND
    ollama_url: str = DEFAULT_OLLAMA_URL
    qdrant_url: str = DEFAULT_QDRANT_URL
    model: str = DEFAULT_MODEL
    gitee_endpoint: str = DEFAULT_GITEE_API_ENDPOINT
    gitee_token: str = ""
    gitee_token_env: str = DEFAULT_GITEE_TOKEN_ENV
    gitee_dimensions: int = 1024
    chapter_collection: str = DEFAULT_CHAPTER_COLLECTION
    chunk_collection: str = DEFAULT_CHUNK_COLLECTION
    query_window_size: int = 800
    query_overlap_size: int = 200
    chapter_top_k: int = 0
    chunk_top_k: int = 12
    merged_top_k: int = 20
    score_threshold: float = 0.0
    local_index_dir: str = ""
    local_model_path: str = ""
    local_device: str = "cpu"
    local_encode_batch_size: int = 16
    embed_timeout_seconds: int = 30
    embed_max_attempts: int = 2
    embed_total_timeout_seconds: int = 15
    qdrant_timeout_seconds: int = 30


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except TimeoutError as exc:
        raise SemanticRetrievalError(f"Request timed out for {url}: {exc}") from exc
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SemanticRetrievalError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise SemanticRetrievalError(f"Request failed for {url}: {exc}") from exc
    except OSError as exc:
        message = str(exc).strip()
        if "timed out" in message.lower() or "timeout" in message.lower():
            raise SemanticRetrievalError(f"Request timed out for {url}: {message}") from exc
        raise SemanticRetrievalError(f"Request failed for {url}: {message}") from exc


def clip_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if limit <= 0 or len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def slice_query_semantic_chunks(
    query_text: str,
    window_size: int = 800,
    overlap_size: int = 200,
) -> list[dict[str, Any]]:
    if not query_text.strip():
        raise ValueError("query_text is empty")
    chunks = iter_chunks(query_text, window_size=window_size, overlap_size=overlap_size)
    return [
        {
            "chunk_order": int(chunk_order),
            "start_offset": int(start_offset),
            "end_offset": int(end_offset),
            "text": content,
            "char_count": len(content),
        }
        for chunk_order, start_offset, end_offset, content in chunks
    ]


def embed_texts(
    ollama_url: str,
    model: str,
    texts: list[str],
    timeout_seconds: int,
) -> list[list[float]]:
    if not texts:
        return []
    payload = {
        "model": model,
        "input": texts,
    }
    resp = http_json("POST", f"{ollama_url}/api/embed", payload, timeout=timeout_seconds)
    embeddings = resp.get("embeddings")
    if not isinstance(embeddings, list):
        raise SemanticRetrievalError(f"Unexpected Ollama response: {resp}")
    if len(embeddings) != len(texts):
        raise SemanticRetrievalError(
            f"Embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
        )
    return embeddings


def embed_texts_remote(config: SemanticRetrievalConfig, texts: list[str]) -> list[list[float]]:
    if config.remote_embedding_backend == "ollama":
        return embed_texts(
            ollama_url=config.ollama_url,
            model=config.model,
            texts=texts,
            timeout_seconds=config.embed_timeout_seconds,
        )
    if config.remote_embedding_backend == "gitee_api":
        try:
            return gitee_embed_texts(
                endpoint=config.gitee_endpoint,
                token=resolve_gitee_token(config.gitee_token, config.gitee_token_env),
                model=config.model,
                texts=texts,
                dimensions=config.gitee_dimensions,
                timeout=config.embed_timeout_seconds,
                max_attempts=config.embed_max_attempts,
                total_timeout_seconds=config.embed_total_timeout_seconds,
            )
        except GiteeEmbeddingError as exc:
            raise SemanticRetrievalError(str(exc)) from exc
    raise SemanticRetrievalError(
        f"Unsupported remote embedding backend: {config.remote_embedding_backend!r}"
    )


def qdrant_search(
    qdrant_url: str,
    collection: str,
    vector: list[float],
    limit: int,
    timeout_seconds: int,
    score_threshold: float = 0.0,
    query_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    if score_threshold > 0:
        payload["score_threshold"] = score_threshold
    if query_filter:
        payload["filter"] = query_filter
    resp = http_json(
        "POST",
        f"{qdrant_url}/collections/{collection}/points/search",
        payload,
        timeout=timeout_seconds,
    )
    results = resp.get("result")
    if not isinstance(results, list):
        raise SemanticRetrievalError(f"Unexpected Qdrant response: {resp}")
    return results


def _search_one_query_chunk(
    *,
    query_chunk: dict[str, Any],
    vector: list[float],
    effective: SemanticRetrievalConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    chapter_hits: list[dict[str, Any]] = []
    chunk_hits: list[dict[str, Any]] = []
    chapter_error = ""
    chunk_error = ""

    if effective.chapter_top_k > 0:
        try:
            hits = qdrant_search(
                qdrant_url=effective.qdrant_url,
                collection=effective.chapter_collection,
                vector=vector,
                limit=effective.chapter_top_k,
                timeout_seconds=effective.qdrant_timeout_seconds,
                score_threshold=effective.score_threshold,
            )
        except SemanticRetrievalError as exc:
            chapter_error = str(exc)
        else:
            for item in hits:
                row = dict(item)
                row["query_chunk_order"] = int(query_chunk["chunk_order"])
                chapter_hits.append(row)

    if effective.chunk_top_k > 0:
        try:
            hits = qdrant_search(
                qdrant_url=effective.qdrant_url,
                collection=effective.chunk_collection,
                vector=vector,
                limit=effective.chunk_top_k,
                timeout_seconds=effective.qdrant_timeout_seconds,
                score_threshold=effective.score_threshold,
            )
        except SemanticRetrievalError as exc:
            chunk_error = str(exc)
        else:
            for item in hits:
                row = dict(item)
                row["query_chunk_order"] = int(query_chunk["chunk_order"])
                chunk_hits.append(row)

    return chapter_hits, chunk_hits, chapter_error, chunk_error


def fetch_chapter_metadata(
    db_path: str,
    chapter_uids: list[int],
) -> dict[int, dict[str, Any]]:
    if not chapter_uids:
        return {}
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        placeholders = ",".join("?" for _ in chapter_uids)
        rows = conn.execute(
            f"""
            SELECT c.chapter_uid,
                   c.dataset_key,
                   b.book_ext_id,
                   b.book_name,
                   c.chapter_ext_id,
                   c.chapter_order,
                   c.chapter_name,
                   c.content_sha256
              FROM chapters c
              JOIN books b
                ON b.book_uid = c.book_uid
             WHERE c.chapter_uid IN ({placeholders})
            """,
            chapter_uids,
        ).fetchall()
    finally:
        conn.close()

    return {
        int(chapter_uid): {
            "chapter_uid": int(chapter_uid),
            "dataset_key": dataset_key,
            "book_ext_id": book_ext_id,
            "book_name": book_name,
            "chapter_ext_id": chapter_ext_id,
            "chapter_order": chapter_order,
            "chapter_name": chapter_name,
            "content_sha256": content_sha256,
        }
        for (
            chapter_uid,
            dataset_key,
            book_ext_id,
            book_name,
            chapter_ext_id,
            chapter_order,
            chapter_name,
            content_sha256,
        ) in rows
    }


def aggregate_semantic_hits(
    chapter_hits: list[dict[str, Any]],
    chunk_hits: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    aggregated: dict[int, dict[str, Any]] = {}

    def ensure_entry(chapter_uid: int) -> dict[str, Any]:
        if chapter_uid not in aggregated:
            aggregated[chapter_uid] = {
                "chapter_uid": chapter_uid,
                "chapter_score_max": 0.0,
                "chapter_hit_count": 0,
                "chunk_score_max": 0.0,
                "chunk_hit_count": 0,
                "distinct_query_chunk_orders": set(),
                "score_sum": 0.0,
                "best_chunk_preview": "",
                "best_chunk_start_offset": -1,
                "best_chunk_end_offset": -1,
            }
        return aggregated[chapter_uid]

    for hit in chapter_hits:
        payload = dict(hit.get("payload") or {})
        chapter_uid = int(payload.get("chapter_uid") or hit["id"])
        score = float(hit.get("score", 0.0))
        entry = ensure_entry(chapter_uid)
        entry["chapter_score_max"] = max(entry["chapter_score_max"], score)
        entry["chapter_hit_count"] += 1
        entry["score_sum"] += score

    for hit in chunk_hits:
        payload = dict(hit.get("payload") or {})
        chapter_uid = int(payload.get("chapter_uid") or hit["id"])
        score = float(hit.get("score", 0.0))
        query_chunk_order = int(hit.get("query_chunk_order", 0))
        entry = ensure_entry(chapter_uid)
        entry["chunk_score_max"] = max(entry["chunk_score_max"], score)
        entry["chunk_hit_count"] += 1
        entry["score_sum"] += score
        entry["distinct_query_chunk_orders"].add(query_chunk_order)
        if score >= entry["chunk_score_max"]:
            entry["best_chunk_preview"] = str(payload.get("text_preview", ""))
            entry["best_chunk_start_offset"] = int(payload.get("start_offset", -1))
            entry["best_chunk_end_offset"] = int(payload.get("end_offset", -1))

    return aggregated


def build_semantic_candidate_rows(
    db_path: str,
    aggregated_hits: dict[int, dict[str, Any]],
    merged_top_k: int,
    source_name: str,
) -> list[dict[str, Any]]:
    chapter_uids = sorted(aggregated_hits.keys())
    metadata_map = fetch_chapter_metadata(db_path=db_path, chapter_uids=chapter_uids)

    results: list[dict[str, Any]] = []
    for chapter_uid, entry in aggregated_hits.items():
        meta = metadata_map.get(chapter_uid)
        if not meta:
            continue

        chapter_score = float(entry["chapter_score_max"])
        chunk_score = float(entry["chunk_score_max"])
        hit_count = int(entry["chapter_hit_count"]) + int(entry["chunk_hit_count"])
        distinct_query_chunk_count = len(entry["distinct_query_chunk_orders"])

        final_score = chunk_score
        if chapter_score > 0:
            chapter_bridge = max(0.0, chapter_score - chunk_score)
            final_score = max(final_score, chapter_score * 0.93)
            if chunk_score > 0 and chapter_bridge > 0:
                # Chapter signal should only provide a small bridge over the chunk score,
                # otherwise same-book semantic neighbors get over-rewarded.
                final_score += min(0.015, chapter_bridge * 0.25)
        if distinct_query_chunk_count > 1:
            final_score += min(0.06, 0.02 * (distinct_query_chunk_count - 1))
        elif int(entry["chunk_hit_count"]) > 1:
            final_score += min(0.03, 0.01 * (int(entry["chunk_hit_count"]) - 1))
        final_score = min(final_score, 1.0)

        results.append(
            {
                "chapter_uid": chapter_uid,
                "dataset_key": meta["dataset_key"],
                "book_ext_id": meta["book_ext_id"],
                "book_name": meta["book_name"],
                "chapter_ext_id": meta["chapter_ext_id"],
                "chapter_name": meta["chapter_name"],
                "final_score": final_score,
                "coarse_score": chapter_score,
                "ngram_score": chunk_score,
                "seed_hit_count": hit_count,
                "seed_hit_weight": float(entry["score_sum"]),
                "source_dataset_key": meta["dataset_key"],
                "source_table_name": source_name,
                "recall_source": "semantic",
                "semantic_chapter_score_max": chapter_score,
                "semantic_chunk_score_max": chunk_score,
                "semantic_hit_count": hit_count,
                "semantic_distinct_query_chunk_count": distinct_query_chunk_count,
                "semantic_best_chunk_preview": str(entry["best_chunk_preview"]),
                "semantic_best_chunk_start_offset": int(entry["best_chunk_start_offset"]),
                "semantic_best_chunk_end_offset": int(entry["best_chunk_end_offset"]),
            }
        )

    results.sort(
        key=lambda item: (
            -float(item["final_score"]),
            -float(item["semantic_chunk_score_max"]),
            -float(item["semantic_chapter_score_max"]),
            -int(item["semantic_hit_count"]),
            int(item["chapter_uid"]),
        )
    )
    return results[:merged_top_k]


def merge_recall_candidates(
    lexical_results: list[dict[str, Any]],
    semantic_results: list[dict[str, Any]],
    merged_top_k: int,
) -> list[dict[str, Any]]:
    if merged_top_k <= 0:
        raise ValueError("merged_top_k must be > 0")

    lexical_map = {int(item["chapter_uid"]): dict(item) for item in lexical_results}
    semantic_map = {int(item["chapter_uid"]): dict(item) for item in semantic_results}

    def combine_row(chapter_uid: int) -> dict[str, Any]:
        lexical = lexical_map.get(chapter_uid)
        semantic = semantic_map.get(chapter_uid)
        base = dict(lexical or semantic or {})
        if lexical:
            base["lexical_recall_hit"] = True
            base["lexical_final_score"] = float(lexical["final_score"])
            base["lexical_source_table_name"] = lexical.get("source_table_name", "")
        else:
            base["lexical_recall_hit"] = False
            base["lexical_final_score"] = 0.0
            base["lexical_source_table_name"] = ""
        if semantic:
            base["semantic_recall_hit"] = True
            base["semantic_final_score"] = float(semantic["final_score"])
            base["semantic_chunk_score_max"] = float(semantic.get("semantic_chunk_score_max", 0.0))
            base["semantic_chapter_score_max"] = float(
                semantic.get("semantic_chapter_score_max", 0.0)
            )
            base["semantic_hit_count"] = int(semantic.get("semantic_hit_count", 0))
            base["semantic_distinct_query_chunk_count"] = int(
                semantic.get("semantic_distinct_query_chunk_count", 0)
            )
            base["semantic_best_chunk_preview"] = str(
                semantic.get("semantic_best_chunk_preview", "")
            )
            base["semantic_best_chunk_start_offset"] = int(
                semantic.get("semantic_best_chunk_start_offset", -1)
            )
            base["semantic_best_chunk_end_offset"] = int(
                semantic.get("semantic_best_chunk_end_offset", -1)
            )
            base["semantic_source_table_name"] = semantic.get("source_table_name", "")
        else:
            base["semantic_recall_hit"] = False
            base["semantic_final_score"] = 0.0
            base["semantic_chunk_score_max"] = 0.0
            base["semantic_chapter_score_max"] = 0.0
            base["semantic_hit_count"] = 0
            base["semantic_distinct_query_chunk_count"] = 0
            base["semantic_best_chunk_preview"] = ""
            base["semantic_best_chunk_start_offset"] = -1
            base["semantic_best_chunk_end_offset"] = -1
            base["semantic_source_table_name"] = ""

        recall_sources: list[str] = []
        if lexical:
            recall_sources.append("lexical")
        if semantic:
            recall_sources.append("semantic")
        base["recall_sources"] = recall_sources
        base["recall_source"] = "+".join(recall_sources)
        if lexical and semantic:
            base["source_table_name"] = (
                f"{lexical.get('source_table_name', '')}|{semantic.get('source_table_name', '')}"
            )
            base["final_score"] = max(
                float(lexical["final_score"]),
                float(semantic["final_score"]),
            )
        return base

    overlap_uids = [chapter_uid for chapter_uid in lexical_map if chapter_uid in semantic_map]
    overlap_uids.sort(
        key=lambda chapter_uid: (
            -max(
                float(lexical_map[chapter_uid]["final_score"]),
                float(semantic_map[chapter_uid]["final_score"]),
            ),
            -float(semantic_map[chapter_uid]["final_score"]),
            int(chapter_uid),
        )
    )

    merged: list[dict[str, Any]] = []
    seen: set[int] = set()

    for chapter_uid in overlap_uids:
        merged.append(combine_row(chapter_uid))
        seen.add(chapter_uid)
        if len(merged) >= merged_top_k:
            return merged[:merged_top_k]

    lexical_queue = [int(item["chapter_uid"]) for item in lexical_results if int(item["chapter_uid"]) not in seen]
    semantic_queue = [int(item["chapter_uid"]) for item in semantic_results if int(item["chapter_uid"]) not in seen]

    lexical_idx = 0
    semantic_idx = 0
    turn = "lexical"

    while len(merged) < merged_top_k and (lexical_idx < len(lexical_queue) or semantic_idx < len(semantic_queue)):
        if turn == "lexical" and lexical_idx < len(lexical_queue):
            chapter_uid = lexical_queue[lexical_idx]
            lexical_idx += 1
            if chapter_uid not in seen:
                merged.append(combine_row(chapter_uid))
                seen.add(chapter_uid)
            turn = "semantic"
            continue
        if turn == "semantic" and semantic_idx < len(semantic_queue):
            chapter_uid = semantic_queue[semantic_idx]
            semantic_idx += 1
            if chapter_uid not in seen:
                merged.append(combine_row(chapter_uid))
                seen.add(chapter_uid)
            turn = "lexical"
            continue
        turn = "semantic" if turn == "lexical" else "lexical"

    return merged[:merged_top_k]


def semantic_retrieve_candidates(
    db_path: str,
    query_text: str,
    config: SemanticRetrievalConfig | None = None,
) -> dict[str, Any]:
    if not query_text.strip():
        raise ValueError("query_text is empty")

    effective = config or SemanticRetrievalConfig()
    if effective.backend == "local":
        from service.local_semantic_retrieval import local_semantic_retrieve_candidates

        return local_semantic_retrieve_candidates(
            db_path=db_path,
            query_text=query_text,
            config=effective,
        )
    if effective.backend != "remote":
        raise SemanticRetrievalError(f"Unsupported semantic backend: {effective.backend!r}")
    query_chunks = slice_query_semantic_chunks(
        query_text=query_text,
        window_size=effective.query_window_size,
        overlap_size=effective.query_overlap_size,
    )
    query_vectors = embed_texts_remote(
        config=effective,
        texts=[str(item["text"]) for item in query_chunks],
    )

    chapter_hits: list[dict[str, Any]] = []
    chunk_hits: list[dict[str, Any]] = []
    chapter_search_enabled = effective.chapter_top_k > 0
    chunk_search_enabled = effective.chunk_top_k > 0
    chapter_error = ""
    chunk_error = ""
    search_jobs = [
        (query_chunk, vector)
        for query_chunk, vector in zip(query_chunks, query_vectors)
    ]
    if search_jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(search_jobs))) as executor:
            futures = [
                executor.submit(
                    _search_one_query_chunk,
                    query_chunk=query_chunk,
                    vector=vector,
                    effective=effective,
                )
                for query_chunk, vector in search_jobs
            ]
            for future in futures:
                chunk_chapter_hits, chunk_chunk_hits, chunk_chapter_error, chunk_chunk_error = future.result()
                chapter_hits.extend(chunk_chapter_hits)
                chunk_hits.extend(chunk_chunk_hits)
                if chunk_chapter_error and not chapter_error:
                    chapter_error = chunk_chapter_error
                if chunk_chunk_error and not chunk_error:
                    chunk_error = chunk_chunk_error

    if chapter_error and chunk_error:
        raise SemanticRetrievalError(
            "Semantic recall failed for both chapter and chunk collections. "
            f"chapter_error={chapter_error}; chunk_error={chunk_error}"
        )
    if chapter_error and not chunk_search_enabled:
        raise SemanticRetrievalError(chapter_error)
    if chunk_error and not chapter_search_enabled:
        raise SemanticRetrievalError(chunk_error)

    aggregated_hits = aggregate_semantic_hits(
        chapter_hits=chapter_hits,
        chunk_hits=chunk_hits,
    )
    results = build_semantic_candidate_rows(
        db_path=db_path,
        aggregated_hits=aggregated_hits,
        merged_top_k=effective.merged_top_k,
        source_name=f"{effective.chapter_collection}+{effective.chunk_collection}",
    )
    warnings: list[str] = []
    if chapter_error:
        warnings.append(
            "chapter recall disabled after query failure "
            f"for collection {effective.chapter_collection}: {chapter_error}"
        )
    if chunk_error:
        warnings.append(
            "chunk recall disabled after query failure "
            f"for collection {effective.chunk_collection}: {chunk_error}"
        )

    return {
        "status": "partial" if warnings else "ok",
        "query_chunk_count": len(query_chunks),
        "chapter_hit_count": len(chapter_hits),
        "chunk_hit_count": len(chunk_hits),
        "candidate_count": len(results),
        "error": " | ".join(warnings),
        "results": results,
    }
