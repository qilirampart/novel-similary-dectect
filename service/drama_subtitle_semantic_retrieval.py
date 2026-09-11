from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterator


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.embed_to_qdrant_v1 import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_QDRANT_URL,
    SyncError,
    embed_texts_by_backend,
)
from scripts.gitee_embedding_client_v1 import (
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    resolve_gitee_token,
)
from scripts.v2_common import connect_db
from service.drama_subtitle_language import SUPPORTED_LANGUAGE_CODES, classify_subtitle_text
from service.semantic_retrieval import (
    SemanticRetrievalError,
    qdrant_search,
    slice_query_semantic_chunks,
)


DEFAULT_DRAMA_SUBTITLE_COLLECTION = "drama_subtitle_window_embeddings_qwen3_4b_2560_v1"
DEFAULT_DRAMA_SUBTITLE_MODEL = "Qwen3-Embedding-4B"
DEFAULT_DRAMA_SUBTITLE_DIMENSIONS = 2560


class DramaSubtitleSemanticRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True)
class DramaSubtitleSemanticConfig:
    collection: str = DEFAULT_DRAMA_SUBTITLE_COLLECTION
    embedding_backend: str = "gitee_api"
    ollama_url: str = DEFAULT_OLLAMA_URL
    qdrant_url: str = DEFAULT_QDRANT_URL
    model: str = DEFAULT_DRAMA_SUBTITLE_MODEL
    gitee_endpoint: str = DEFAULT_GITEE_API_ENDPOINT
    gitee_token: str = ""
    gitee_token_env: str = DEFAULT_GITEE_TOKEN_ENV
    gitee_dimensions: int = DEFAULT_DRAMA_SUBTITLE_DIMENSIONS
    query_window_size: int = 800
    query_overlap_size: int = 200
    embed_timeout_seconds: int = 8
    embed_max_attempts: int = 2
    embed_total_timeout_seconds: int = 15
    qdrant_timeout_seconds: int = 30
    # A small cloud host can time out when translated branches all query Qdrant
    # together. Serialize a complete subtitle semantic request across processes.
    qdrant_single_flight_enabled: bool = True
    qdrant_queue_timeout_seconds: float = 12.0
    qdrant_lock_path: str = "/tmp/novel-similarity-drama-qdrant.lock"
    # Zero preserves the complete collection. A positive value restricts
    # semantic recall to the first N episodes without re-embedding points.
    max_episode_order: int = 0


@contextmanager
def _reserve_qdrant_slot(config: DramaSubtitleSemanticConfig) -> Iterator[float]:
    """Reserve the cloud Qdrant slot and report queue wait time.

    The lock is deliberately scoped to subtitle semantic retrieval. Translation
    and FTS work remain parallel; only the resource-constrained vector search is
    serialized. Windows development uses the no-lock path because `fcntl` is a
    POSIX primitive and the cloud runtime is Linux.
    """
    if not config.qdrant_single_flight_enabled or os.name != "posix":
        yield 0.0
        return

    import fcntl

    lock_path = Path(config.qdrant_lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.perf_counter()
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.perf_counter() - started_at >= config.qdrant_queue_timeout_seconds:
                    raise DramaSubtitleSemanticRetrievalError(
                        "Timed out waiting for the subtitle Qdrant request slot."
                    )
                time.sleep(0.05)
        try:
            yield round(time.perf_counter() - started_at, 4)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fetch_evidence_rows(
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


def _candidate_from_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    payload = hit.get("payload")
    if not isinstance(payload, dict):
        return None
    window_uid = str(payload.get("window_uid") or "")
    book_id = str(payload.get("book_id") or "")
    try:
        episode_order = int(payload.get("episode_order"))
    except (TypeError, ValueError):
        return None
    if not window_uid or not book_id:
        return None
    return {
        "window_uid": window_uid,
        "book_id": book_id,
        "book_name": str(payload.get("book_name") or ""),
        "episode_uid": str(payload.get("episode_uid") or ""),
        "episode_order": episode_order,
        "language_code": str(payload.get("language_code") or "unknown").lower(),
        "semantic_score": float(hit.get("score") or 0.0),
        "payload": payload,
    }


def _aggregate_hits(
    hits: list[dict[str, Any]],
    evidence_by_uid: dict[str, dict[str, Any]],
    *,
    candidate_limit: int,
    book_id_filter: str,
    language_code: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    seen_windows: dict[tuple[str, int], set[str]] = defaultdict(set)
    for hit in hits:
        parsed = _candidate_from_hit(hit)
        if parsed is None or parsed["language_code"] != language_code:
            continue
        if book_id_filter and parsed["book_id"] != book_id_filter:
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
                "best_lexical_score": None,
                "semantic_score": parsed["semantic_score"],
                "retrieval_sources": ["semantic"],
                "evidence": evidence_by_uid.get(
                    parsed["window_uid"],
                    {
                        "window_uid": parsed["window_uid"],
                        "window_text_preview": str(parsed["payload"].get("text_preview") or ""),
                    },
                ),
            }
            grouped[key] = candidate
        if parsed["window_uid"] not in seen_windows[key]:
            seen_windows[key].add(parsed["window_uid"])
            candidate["retrieved_window_count"] += 1
        if parsed["semantic_score"] > float(candidate["semantic_score"]):
            candidate["semantic_score"] = parsed["semantic_score"]
            candidate["evidence"] = evidence_by_uid.get(
                parsed["window_uid"],
                {
                    "window_uid": parsed["window_uid"],
                    "window_text_preview": str(parsed["payload"].get("text_preview") or ""),
                },
            )

    candidates = list(grouped.values())
    candidates.sort(
        key=lambda item: (
            -float(item["semantic_score"]),
            -int(item["retrieved_window_count"]),
            str(item["book_id"]),
            int(item["episode_order"]),
        )
    )
    for rank, candidate in enumerate(candidates[:candidate_limit], start=1):
        candidate["rank"] = rank
    return candidates[:candidate_limit]


def search_drama_subtitle_semantic_candidates(
    *,
    db_path: str | Path,
    query_text: str,
    config: DramaSubtitleSemanticConfig = DramaSubtitleSemanticConfig(),
    candidate_limit: int = 10,
    window_limit: int = 100,
    book_id: str = "",
    include_window_text: bool = True,
    language_code: str = "",
) -> dict[str, Any]:
    if candidate_limit <= 0 or window_limit <= 0:
        raise ValueError("candidate_limit and window_limit must be > 0")
    if config.gitee_dimensions <= 0 or config.qdrant_timeout_seconds <= 0:
        raise ValueError("semantic dimensions and Qdrant timeout must be > 0")
    normalized_query = str(query_text or "").strip()
    if not normalized_query:
        raise ValueError("query_text is empty")
    resolved_db_path = Path(db_path)
    if not resolved_db_path.exists():
        raise DramaSubtitleSemanticRetrievalError(
            f"subtitle database not found: {resolved_db_path}"
        )

    detected_language = classify_subtitle_text(normalized_query)
    requested_language = language_code.strip().lower()
    if requested_language and requested_language not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"unsupported language_code: {requested_language}")
    effective_language = requested_language or detected_language.language_code
    if effective_language not in SUPPORTED_LANGUAGE_CODES:
        raise DramaSubtitleSemanticRetrievalError(
            "semantic subtitle search requires a supported query language"
        )

    try:
        gitee_token = (
            resolve_gitee_token(config.gitee_token, config.gitee_token_env)
            if config.embedding_backend == "gitee_api"
            else ""
        )
        query_chunks = slice_query_semantic_chunks(
            query_text=normalized_query,
            window_size=config.query_window_size,
            overlap_size=config.query_overlap_size,
        )
        embedding_started_at = time.perf_counter()
        vectors = embed_texts_by_backend(
            backend=config.embedding_backend,
            ollama_url=config.ollama_url,
            model=config.model,
            texts=[str(chunk["text"]) for chunk in query_chunks],
            gitee_endpoint=config.gitee_endpoint,
            gitee_token=gitee_token,
            gitee_dimensions=config.gitee_dimensions,
            timeout_seconds=config.embed_timeout_seconds,
            max_attempts=config.embed_max_attempts,
            total_timeout_seconds=config.embed_total_timeout_seconds,
        )
        embedding_duration = time.perf_counter() - embedding_started_at
        if len(vectors) != len(query_chunks):
            raise DramaSubtitleSemanticRetrievalError(
                f"embedding count mismatch: {len(vectors)} vectors for {len(query_chunks)} query chunks"
            )
        raw_hits: list[dict[str, Any]] = []
        filter_conditions: list[dict[str, Any]] = [
            {"key": "language_code", "match": {"value": effective_language}}
        ]
        if config.max_episode_order > 0:
            filter_conditions.append(
                {"key": "episode_order", "range": {"lte": config.max_episode_order}}
            )
        language_filter = {"must": filter_conditions}
        qdrant_started_at = time.perf_counter()
        with _reserve_qdrant_slot(config) as qdrant_queue_wait_seconds:
            for query_chunk, vector in zip(query_chunks, vectors):
                for hit in qdrant_search(
                    qdrant_url=config.qdrant_url,
                    collection=config.collection,
                    vector=vector,
                    limit=window_limit,
                    timeout_seconds=config.qdrant_timeout_seconds,
                    query_filter=language_filter,
                ):
                    item = dict(hit)
                    item["query_chunk_order"] = int(query_chunk["chunk_order"])
                    raw_hits.append(item)
        qdrant_duration = time.perf_counter() - qdrant_started_at
    except (GiteeEmbeddingError, SyncError, SemanticRetrievalError) as exc:
        raise DramaSubtitleSemanticRetrievalError(str(exc)) from exc

    window_uids = sorted(
        {
            str((hit.get("payload") or {}).get("window_uid") or "")
            for hit in raw_hits
        }
        - {""}
    )
    evidence_fetch_started_at = time.perf_counter()
    evidence_by_uid = _fetch_evidence_rows(
        resolved_db_path,
        window_uids,
        include_window_text=include_window_text,
    )
    evidence_fetch_duration = time.perf_counter() - evidence_fetch_started_at
    candidates = _aggregate_hits(
        raw_hits,
        evidence_by_uid,
        candidate_limit=candidate_limit,
        book_id_filter=book_id.strip(),
        language_code=effective_language,
    )
    return {
        "mode": "semantic_qdrant",
        "query_text": normalized_query,
        "query_language_code": detected_language.language_code,
        "query_language_confidence": detected_language.confidence,
        "language_filter": effective_language,
        "max_episode_order": config.max_episode_order,
        "collection": config.collection,
        "query_chunk_count": len(query_chunks),
        "window_hit_count": len(raw_hits),
        "embedding_duration_seconds": round(embedding_duration, 4),
        "qdrant_duration_seconds": round(qdrant_duration, 4),
        "qdrant_queue_wait_seconds": qdrant_queue_wait_seconds,
        "evidence_fetch_duration_seconds": round(evidence_fetch_duration, 4),
        "evidence_window_count": len(window_uids),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
