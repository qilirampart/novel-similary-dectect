from __future__ import annotations

from pathlib import Path

from scripts.v2_common import connect_db
from service.drama_subtitle_semantic_retrieval import (
    DramaSubtitleSemanticConfig,
    search_drama_subtitle_semantic_candidates,
)


def _build_subtitle_db(path: Path) -> None:
    conn = connect_db(path)
    try:
        conn.executescript(
            """
            CREATE TABLE drama_subtitle_windows (
                window_uid TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                book_name TEXT NOT NULL,
                episode_uid TEXT NOT NULL,
                episode_order INTEGER NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                time_start TEXT,
                time_end TEXT,
                window_text TEXT NOT NULL,
                window_text_preview TEXT NOT NULL,
                line_count INTEGER NOT NULL,
                char_count INTEGER NOT NULL,
                language_code TEXT NOT NULL,
                language_confidence REAL NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO drama_subtitle_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "book-zh:1:1-4", "book-zh", "中文命中剧", "book-zh:1", 1,
                    1, 4, "00:00:01,000", "00:00:08,000", "第一段完整中文字幕", "第一段完整中文字幕",
                    4, 10, "zh", 1.0,
                ),
                (
                    "book-zh:1:5-8", "book-zh", "中文命中剧", "book-zh:1", 1,
                    5, 8, "00:00:09,000", "00:00:16,000", "第二段完整中文字幕", "第二段完整中文字幕",
                    4, 10, "zh", 1.0,
                ),
                (
                    "book-en:1:1-4", "book-en", "English Noise", "book-en:1", 1,
                    1, 4, "00:00:01,000", "00:00:08,000", "English evidence", "English evidence",
                    4, 16, "en", 1.0,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_semantic_search_filters_language_and_aggregates_evidence(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "subtitle.sqlite3"
    _build_subtitle_db(db_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "service.drama_subtitle_semantic_retrieval.embed_texts_by_backend",
        lambda **_: [[0.1, 0.2]],
    )

    def fake_qdrant_search(**kwargs: object) -> list[dict[str, object]]:
        captured["filter"] = kwargs["query_filter"]
        return [
            {
                "score": 0.81,
                "payload": {
                    "window_uid": "book-zh:1:5-8", "book_id": "book-zh", "book_name": "中文命中剧",
                    "episode_uid": "book-zh:1", "episode_order": 1, "language_code": "zh",
                },
            },
            {
                "score": 0.93,
                "payload": {
                    "window_uid": "book-zh:1:1-4", "book_id": "book-zh", "book_name": "中文命中剧",
                    "episode_uid": "book-zh:1", "episode_order": 1, "language_code": "zh",
                },
            },
            {
                "score": 0.99,
                "payload": {
                    "window_uid": "book-en:1:1-4", "book_id": "book-en", "book_name": "English Noise",
                    "episode_uid": "book-en:1", "episode_order": 1, "language_code": "en",
                },
            },
        ]

    monkeypatch.setattr(
        "service.drama_subtitle_semantic_retrieval.qdrant_search",
        fake_qdrant_search,
    )

    result = search_drama_subtitle_semantic_candidates(
        db_path=db_path,
        query_text="这是一段中文测试台词",
        config=DramaSubtitleSemanticConfig(embedding_backend="ollama", max_episode_order=7),
        candidate_limit=5,
        window_limit=20,
    )

    assert captured["filter"] == {"must": [
        {"key": "language_code", "match": {"value": "zh"}},
        {"key": "episode_order", "range": {"lte": 7}},
    ]}
    assert result["query_language_code"] == "zh"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["book_id"] == "book-zh"
    assert candidate["retrieved_window_count"] == 2
    assert candidate["semantic_score"] == 0.93
    assert candidate["retrieval_sources"] == ["semantic"]
    assert candidate["evidence"]["window_uid"] == "book-zh:1:1-4"
    assert candidate["evidence"]["window_text"] == "第一段完整中文字幕"
    assert result["qdrant_queue_wait_seconds"] == 0.0
