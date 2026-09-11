from __future__ import annotations

from service.drama_subtitle_hybrid_retrieval import search_drama_subtitle_hybrid_candidates
from service.drama_subtitle_semantic_retrieval import DramaSubtitleSemanticRetrievalError


def _lexical_candidate(book_id: str, rank: int, coverage: float) -> dict[str, object]:
    return {
        "book_id": book_id,
        "book_name": book_id,
        "episode_uid": f"{book_id}:1",
        "episode_order": 1,
        "language_code": "zh",
        "rank": rank,
        "retrieved_window_count": 1,
        "best_lexical_score": -float(rank),
        "match_metrics": {"query_coverage_rate": coverage},
        "evidence": {"window_uid": f"{book_id}:lexical", "window_text": f"{book_id} lexical evidence"},
    }


def _semantic_candidate(book_id: str, rank: int, score: float) -> dict[str, object]:
    return {
        "book_id": book_id,
        "book_name": book_id,
        "episode_uid": f"{book_id}:1",
        "episode_order": 1,
        "language_code": "zh",
        "rank": rank,
        "retrieved_window_count": 1,
        "semantic_score": score,
        "evidence": {"window_uid": f"{book_id}:semantic", "window_text": f"{book_id} semantic evidence"},
    }


def test_hybrid_preserves_strong_lexical_and_merges_semantic_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_lexical_candidates",
        lambda **_: {
            "query_text": "测试台词",
            "query_language_code": "zh",
            "query_language_confidence": 1.0,
            "language_filter": "zh",
            "candidates": [
                _lexical_candidate("strong", 1, 1.0),
                _lexical_candidate("shared", 2, 0.2),
            ],
        },
    )
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_semantic_candidates",
        lambda **_: {
            "collection": "drama_subtitle_window_embeddings_qwen3_4b_2560_v1",
            "candidates": [
                _semantic_candidate("shared", 1, 0.91),
                _semantic_candidate("semantic-only", 2, 0.87),
            ],
        },
    )

    result = search_drama_subtitle_hybrid_candidates(
        db_path="unused.sqlite3",
        query_text="测试台词",
        candidate_limit=3,
    )

    assert result["mode"] == "hybrid_lexical_semantic"
    assert result["semantic_qdrant_queue_wait_seconds"] == 0.0
    assert [item["book_id"] for item in result["candidates"]] == ["strong", "shared", "semantic-only"]
    assert result["candidates"][0]["retrieval_sources"] == ["lexical"]
    shared = result["candidates"][1]
    assert shared["retrieval_sources"] == ["lexical", "semantic"]
    assert shared["semantic_score"] == 0.91
    assert shared["evidence"]["window_uid"] == "shared:lexical"
    assert result["candidates"][2]["retrieval_sources"] == ["semantic"]


def test_hybrid_falls_back_to_lexical_when_semantic_errors(monkeypatch) -> None:
    lexical = _lexical_candidate("strong", 1, 1.0)
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_lexical_candidates",
        lambda **_: {
            "query_text": "测试台词",
            "query_language_code": "zh",
            "query_language_confidence": 1.0,
            "language_filter": "zh",
            "candidates": [lexical],
        },
    )
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_semantic_candidates",
        lambda **_: (_ for _ in ()).throw(DramaSubtitleSemanticRetrievalError("Qdrant unavailable")),
    )

    result = search_drama_subtitle_hybrid_candidates(
        db_path="unused.sqlite3",
        query_text="测试台词",
    )

    assert result["mode"] == "lexical_only"
    assert result["semantic_status"] == "fallback_lexical_only"
    assert "Qdrant unavailable" in result["semantic_error"]
    assert result["candidates"][0]["retrieval_sources"] == ["lexical"]


def test_hybrid_reuses_precomputed_lexical_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_lexical_candidates",
        lambda **_: (_ for _ in ()).throw(AssertionError("lexical retrieval should be reused")),
    )
    monkeypatch.setattr(
        "service.drama_subtitle_hybrid_retrieval.search_drama_subtitle_semantic_candidates",
        lambda **_: {
            "collection": "drama_subtitle_window_embeddings_qwen3_4b_2560_v1",
            "candidates": [_semantic_candidate("semantic", 1, 0.91)],
        },
    )

    result = search_drama_subtitle_hybrid_candidates(
        db_path="unused.sqlite3",
        query_text="test subtitle",
        candidate_limit=2,
        precomputed_lexical={
            "query_text": "test subtitle",
            "query_language_code": "en",
            "query_language_confidence": 1.0,
            "language_filter": "en",
            "candidates": [_lexical_candidate("lexical", 1, 0.3)],
        },
    )

    assert result["lexical_reused"] is True
    assert result["lexical_duration_seconds"] == 0.0
    assert [item["book_id"] for item in result["candidates"]] == ["semantic", "lexical"]
