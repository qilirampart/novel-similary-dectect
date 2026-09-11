from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from service.drama_subtitle_decision import decide_drama_subtitle_match
from service.drama_subtitle_retrieval import normalize_query, search_drama_subtitle_lexical_candidates
from service.drama_subtitle_semantic_retrieval import (
    DramaSubtitleSemanticConfig,
    DramaSubtitleSemanticRetrievalError,
    search_drama_subtitle_semantic_candidates,
)


DEFAULT_STRONG_LEXICAL_COVERAGE = 0.6


def _lexical_coverage(candidate: dict[str, Any]) -> float:
    metrics = candidate.get("match_metrics")
    if not isinstance(metrics, dict):
        return 0.0
    try:
        return float(metrics.get("query_coverage_rate") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _merge_candidate(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    source: str,
) -> None:
    sources = list(target.get("retrieval_sources") or [])
    if source not in sources:
        sources.append(source)
    target["retrieval_sources"] = sources
    target[f"{source}_rank"] = incoming.get("rank")
    target[f"{source}_retrieved_window_count"] = incoming.get("retrieved_window_count", 0)

    if source == "semantic":
        target["semantic_score"] = incoming.get("semantic_score")
        # Preserve lexical evidence when available because it carries explainable highlights.
        if "lexical" not in sources[:-1]:
            target["evidence"] = incoming.get("evidence") or target.get("evidence")
    else:
        target["best_lexical_score"] = incoming.get("best_lexical_score")
        target["match_metrics"] = incoming.get("match_metrics")
        target["evidence"] = incoming.get("evidence") or target.get("evidence")


def _seed_candidate(candidate: dict[str, Any], *, source: str) -> dict[str, Any]:
    seeded = dict(candidate)
    seeded["retrieval_sources"] = [source]
    seeded[f"{source}_rank"] = candidate.get("rank")
    seeded[f"{source}_retrieved_window_count"] = candidate.get("retrieved_window_count", 0)
    seeded.setdefault("best_lexical_score", None)
    seeded.setdefault("semantic_score", None)
    if source == "lexical":
        seeded["lexical_rank"] = candidate.get("rank")
    return seeded


def _merge_candidates(
    *,
    lexical_candidates: list[dict[str, Any]],
    semantic_candidates: list[dict[str, Any]],
    candidate_limit: int,
    strong_lexical_coverage: float,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for candidate in lexical_candidates:
        key = (str(candidate.get("book_id") or ""), int(candidate.get("episode_order") or 0))
        if not key[0]:
            continue
        merged[key] = _seed_candidate(candidate, source="lexical")
    for candidate in semantic_candidates:
        key = (str(candidate.get("book_id") or ""), int(candidate.get("episode_order") or 0))
        if not key[0]:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = _seed_candidate(candidate, source="semantic")
        else:
            _merge_candidate(existing, candidate, source="semantic")

    def sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, float, str, int]:
        sources = list(candidate.get("retrieval_sources") or [])
        lexical_rank = int(candidate.get("lexical_rank") or 10**6)
        semantic_rank = int(candidate.get("semantic_rank") or 10**6)
        semantic_score = float(candidate.get("semantic_score") or 0.0)
        coverage = _lexical_coverage(candidate)
        # Exact lexical evidence is never displaced by a vector-only result.
        if "lexical" in sources and coverage >= strong_lexical_coverage:
            group = 0
        elif "lexical" in sources and "semantic" in sources:
            group = 1
        elif "semantic" in sources:
            group = 2
        else:
            group = 3
        return (
            group,
            lexical_rank if group in {0, 1, 3} else semantic_rank,
            semantic_rank,
            -semantic_score,
            str(candidate.get("book_id") or ""),
            int(candidate.get("episode_order") or 0),
        )

    candidates = sorted(merged.values(), key=sort_key)[:candidate_limit]
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates


def search_drama_subtitle_hybrid_candidates(
    *,
    db_path: str | Path,
    query_text: str,
    candidate_limit: int = 10,
    window_limit: int = 200,
    include_window_text: bool = True,
    language_code: str = "",
    semantic_enabled: bool = True,
    semantic_config: DramaSubtitleSemanticConfig = DramaSubtitleSemanticConfig(),
    semantic_window_limit: int = 100,
    strong_lexical_coverage: float = DEFAULT_STRONG_LEXICAL_COVERAGE,
    precomputed_lexical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if candidate_limit <= 0 or window_limit <= 0 or semantic_window_limit <= 0:
        raise ValueError("candidate and window limits must be > 0")
    if not 0.0 <= strong_lexical_coverage <= 1.0:
        raise ValueError("strong_lexical_coverage must be between 0 and 1")
    normalized_query = normalize_query(query_text)
    if not normalized_query:
        raise ValueError("query_text is empty after subtitle marker normalization")

    lexical_reused = isinstance(precomputed_lexical, dict)
    if lexical_reused:
        # Translation fallback has already run this exact lexical precheck.
        # Reuse it so a semantic follow-up does not scan the FTS index twice.
        lexical = dict(precomputed_lexical)
        lexical_duration = 0.0
    else:
        lexical_started_at = time.perf_counter()
        lexical = search_drama_subtitle_lexical_candidates(
            db_path=str(db_path),
            query_text=normalized_query,
            candidate_limit=max(candidate_limit * 2, 20),
            window_limit=window_limit,
            include_window_text=include_window_text,
            language_code=language_code,
        )
        lexical_duration = round(time.perf_counter() - lexical_started_at, 4)
    lexical_candidates = list(lexical.get("candidates") or [])

    semantic: dict[str, Any] | None = None
    semantic_error = ""
    semantic_duration = 0.0
    semantic_status = "disabled"
    if semantic_enabled:
        semantic_started_at = time.perf_counter()
        try:
            semantic = search_drama_subtitle_semantic_candidates(
                db_path=str(db_path),
                query_text=normalized_query,
                config=semantic_config,
                candidate_limit=max(candidate_limit * 2, 20),
                window_limit=semantic_window_limit,
                include_window_text=include_window_text,
                language_code=language_code,
            )
            semantic_status = "ok"
        except (DramaSubtitleSemanticRetrievalError, ValueError) as exc:
            semantic_error = str(exc)
            semantic_status = "fallback_lexical_only"
        finally:
            semantic_duration = round(time.perf_counter() - semantic_started_at, 4)

    semantic_candidates = list((semantic or {}).get("candidates") or [])
    candidates = _merge_candidates(
        lexical_candidates=lexical_candidates,
        semantic_candidates=semantic_candidates,
        candidate_limit=candidate_limit,
        strong_lexical_coverage=strong_lexical_coverage,
    )
    query_language_code = lexical.get("query_language_code") or "unknown"
    return {
        "mode": "hybrid_lexical_semantic" if semantic_status == "ok" else "lexical_only",
        "query_text": lexical.get("query_text") or normalized_query,
        "query_language_code": query_language_code,
        "query_language_confidence": lexical.get("query_language_confidence") or 0.0,
        "language_filter": lexical.get("language_filter") or "",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "lexical_status": "ok",
        "lexical_reused": lexical_reused,
        "lexical_candidate_count": len(lexical_candidates),
        "lexical_duration_seconds": lexical_duration,
        "semantic_status": semantic_status,
        "semantic_error": semantic_error,
        "semantic_candidate_count": len(semantic_candidates),
        "semantic_duration_seconds": semantic_duration,
        "semantic_embedding_duration_seconds": (semantic or {}).get("embedding_duration_seconds", 0.0),
        "semantic_qdrant_duration_seconds": (semantic or {}).get("qdrant_duration_seconds", 0.0),
        "semantic_qdrant_queue_wait_seconds": (semantic or {}).get("qdrant_queue_wait_seconds", 0.0),
        "semantic_evidence_fetch_duration_seconds": (semantic or {}).get("evidence_fetch_duration_seconds", 0.0),
        "semantic_evidence_window_count": (semantic or {}).get("evidence_window_count", 0),
        "semantic_collection": (semantic or {}).get("collection") or semantic_config.collection,
        "decision": decide_drama_subtitle_match(
            candidates=candidates,
            semantic_status=semantic_status,
            query_language_code=str(query_language_code),
            query_text=normalized_query,
        ),
    }
