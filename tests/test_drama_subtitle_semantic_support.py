from __future__ import annotations

import pytest

from scripts import embed_drama_subtitle_windows_v1 as subtitle_embedder
from scripts.embed_to_qdrant_v1 import SyncError
from service.semantic_retrieval import qdrant_search
from scripts.search_drama_subtitle_semantic_v1 import candidate_from_hit


def test_qdrant_search_forwards_language_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_http_json(method: str, url: str, payload: dict[str, object], timeout: int) -> dict[str, object]:
        captured.update({"method": method, "url": url, "payload": payload, "timeout": timeout})
        return {"result": []}

    monkeypatch.setattr("service.semantic_retrieval.http_json", fake_http_json)
    language_filter = {"must": [{"key": "language_code", "match": {"value": "en"}}]}

    assert qdrant_search(
        qdrant_url="http://127.0.0.1:6333",
        collection="drama_subtitle_window_embeddings_qwen3_4b_2560_v1",
        vector=[0.1, 0.2],
        limit=10,
        timeout_seconds=8,
        query_filter=language_filter,
    ) == []

    assert captured["method"] == "POST"
    assert captured["timeout"] == 8
    assert captured["payload"] == {
        "vector": [0.1, 0.2],
        "limit": 10,
        "with_payload": True,
        "filter": language_filter,
    }


def test_semantic_candidate_exposes_payload_language() -> None:
    candidate = candidate_from_hit(
        {
            "score": 0.87,
            "payload": {
                "window_uid": "book-1:1:1-16",
                "book_id": "book-1",
                "book_name": "English drama",
                "episode_uid": "book-1:1",
                "episode_order": 1,
                "language_code": "en",
            },
        }
    )

    assert candidate is not None
    assert candidate["language_code"] == "en"


def test_qdrant_upsert_retries_transient_connection_error(monkeypatch) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_upsert(_url: str, _collection: str, _points: list[dict[str, object]]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SyncError("Request failed for local Qdrant: simulated timeout")

    monkeypatch.setattr(subtitle_embedder, "upsert_points", fake_upsert)
    monkeypatch.setattr(subtitle_embedder.time, "sleep", delays.append)

    subtitle_embedder.upsert_points_with_retry(
        "http://127.0.0.1:6333",
        "test-collection",
        [{"id": "point-1"}],
        max_attempts=4,
        retry_backoff_seconds=0.25,
    )

    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_qdrant_upsert_does_not_retry_permanent_error(monkeypatch) -> None:
    attempts = 0

    def fake_upsert(_url: str, _collection: str, _points: list[dict[str, object]]) -> None:
        nonlocal attempts
        attempts += 1
        raise SyncError("HTTP 400 for local Qdrant: invalid vector")

    monkeypatch.setattr(subtitle_embedder, "upsert_points", fake_upsert)

    with pytest.raises(SyncError, match="HTTP 400"):
        subtitle_embedder.upsert_points_with_retry(
            "http://127.0.0.1:6333",
            "test-collection",
            [{"id": "point-1"}],
            max_attempts=4,
            retry_backoff_seconds=0,
        )

    assert attempts == 1
