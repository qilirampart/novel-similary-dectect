from __future__ import annotations

import pytest

from service.drama_subtitle_reranker import (
    DramaSubtitleRerankerConfig,
    DramaSubtitleRerankerError,
    rerank_drama_subtitle_candidates,
)


def _candidate(book_id: str, rank: int) -> dict[str, object]:
    return {
        "book_id": book_id,
        "episode_order": 1,
        "rank": rank,
        "evidence": {"window_text": f"evidence for {book_id}"},
    }


def test_reranker_adds_scores_and_sorts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_http_json(**kwargs: object) -> list[float]:
        captured.update(kwargs)
        return [0.2, 0.9, 0.5]

    monkeypatch.setattr("service.drama_subtitle_reranker.gitee_http_json", fake_http_json)
    result = rerank_drama_subtitle_candidates(
        query_text="query",
        candidates=[_candidate("a", 1), _candidate("b", 2), _candidate("c", 3)],
        config=DramaSubtitleRerankerConfig(gitee_token="test-token"),
    )

    assert [item["book_id"] for item in result] == ["b", "c", "a"]
    assert result[0]["reranker_score"] == 0.9
    assert result[0]["reranker_rank"] == 1
    assert captured["url"] == "https://ai.gitee.com/v1/sentence-similarity"
    assert captured["payload"]["inputs"]["sentences"] == [
        "evidence for a", "evidence for b", "evidence for c"
    ]


def test_reranker_rejects_score_count_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(
        "service.drama_subtitle_reranker.gitee_http_json",
        lambda **_: [0.2],
    )
    with pytest.raises(DramaSubtitleRerankerError, match="expected 2 scores"):
        rerank_drama_subtitle_candidates(
            query_text="query",
            candidates=[_candidate("a", 1), _candidate("b", 2)],
            config=DramaSubtitleRerankerConfig(gitee_token="test-token"),
        )
