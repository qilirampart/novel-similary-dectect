from __future__ import annotations

from pathlib import Path

from scripts.v2_common import connect_db
from service.drama_subtitle_evidence_context import get_drama_subtitle_evidence_context
from service.drama_subtitle_retrieval import (
    build_trigram_match_query,
    normalize_query,
    search_drama_subtitle_lexical_candidates,
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
                language_code TEXT NOT NULL DEFAULT 'unknown',
                language_confidence REAL NOT NULL DEFAULT 0
            );
            CREATE VIRTUAL TABLE drama_subtitle_windows_fts
            USING fts5(window_uid UNINDEXED, window_text, tokenize = 'trigram');
            CREATE VIRTUAL TABLE drama_subtitle_windows_word_fts
            USING fts5(window_uid UNINDEXED, window_text, tokenize = 'unicode61');
            CREATE VIRTUAL TABLE drama_subtitle_windows_fts_lang_v2
            USING fts5(window_uid UNINDEXED, language_code, window_text, tokenize = 'trigram');
            CREATE VIRTUAL TABLE drama_subtitle_windows_word_fts_lang_v2
            USING fts5(window_uid UNINDEXED, language_code, window_text, tokenize = 'unicode61');
            CREATE TABLE drama_subtitle_lexical_index_metadata (
                index_name TEXT PRIMARY KEY,
                source_window_count INTEGER NOT NULL,
                indexed_window_count INTEGER NOT NULL,
                build_status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        rows = [
            (
                "book-a:1:1-4",
                "book-a",
                "命中剧",
                "book-a:1",
                1,
                1,
                4,
                "00:00:01,000",
                "00:00:08,000",
                "大哥这是横店吗你们是不是在拍戏",
                "大哥这是横店吗你们是不是在拍戏",
                4,
                16,
                "zh",
                1.0,
            ),
            (
                "book-a:1:3-6",
                "book-a",
                "命中剧",
                "book-a:1",
                1,
                3,
                6,
                "00:00:05,000",
                "00:00:12,000",
                "横店吗你们是不是在拍戏接着往下走",
                "横店吗你们是不是在拍戏接着往下走",
                4,
                18,
                "zh",
                1.0,
            ),
            (
                "book-b:2:1-4",
                "book-b",
                "干扰剧",
                "book-b:2",
                2,
                1,
                4,
                "00:00:01,000",
                "00:00:08,000",
                "你们是不是在拍戏呀",
                "你们是不是在拍戏呀",
                4,
                10,
                "zh",
                1.0,
            ),
            (
                "book-en-match:1:1-4",
                "book-en-match",
                "English Exact Match",
                "book-en-match:1",
                1,
                1,
                4,
                "00:00:01,000",
                "00:00:08,000",
                "Isn't Sonia Adrienne's secretary? I am going to see Adrian anyway.",
                "Isn't Sonia Adrienne's secretary? I am going to see Adrian anyway.",
                4,
                64,
                "en",
                1.0,
            ),
            (
                "book-en-noise:1:1-4",
                "book-en-noise",
                "English Noise",
                "book-en-noise:1",
                1,
                1,
                4,
                "00:00:01,000",
                "00:00:08,000",
                "The secretary said Adrian would arrive tomorrow, so everyone waited.",
                "The secretary said Adrian would arrive tomorrow, so everyone waited.",
                4,
                65,
                "en",
                1.0,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO drama_subtitle_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_windows_fts(window_uid, window_text) VALUES (?, ?)",
            [(row[0], row[9]) for row in rows],
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_windows_word_fts(window_uid, window_text) VALUES (?, ?)",
            [(row[0], row[9]) for row in rows],
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_windows_fts_lang_v2(window_uid, language_code, window_text) VALUES (?, ?, ?)",
            [(row[0], f"lang_{row[13]}", row[9]) for row in rows],
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_windows_word_fts_lang_v2(window_uid, language_code, window_text) VALUES (?, ?, ?)",
            [(row[0], f"lang_{row[13]}", row[9]) for row in rows],
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_lexical_index_metadata VALUES (?, ?, ?, ?, ?)",
            [
                ("drama_subtitle_windows_fts_lang_v2", len(rows), len(rows), "ready", "2026-09-04"),
                ("drama_subtitle_windows_word_fts_lang_v2", len(rows), len(rows), "ready", "2026-09-04"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_lexical_search_aggregates_overlapping_windows_to_one_episode(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_subtitle.sqlite3"
    _build_subtitle_db(db_path)

    result = search_drama_subtitle_lexical_candidates(
        db_path=str(db_path),
        query_text="大哥这是横店吗你们是不是在拍戏",
        candidate_limit=5,
        window_limit=20,
    )

    assert result["mode"] == "fts5_trigram"
    assert result["lexical_index_variant"] == "language_fts_v2"
    assert result["candidates"][0]["best_lexical_score"] < 0
    assert result["candidate_count"] == 2
    metrics = result["candidates"][0]["match_metrics"]
    assert metrics["shared_trigram_count"] > 0
    assert metrics["query_coverage_rate"] == 1.0
    assert metrics["shared_trigrams"]
    assert result["candidates"][0]["book_id"] == "book-a"
    assert result["candidates"][0]["episode_order"] == 1
    assert result["candidates"][0]["retrieved_window_count"] == 2
    assert result["candidates"][0]["evidence"]["window_text"] == "大哥这是横店吗你们是不是在拍戏"


def test_subtitle_marker_normalization_is_shared_across_entry_points():
    assert normalize_query("Hello [music]  world [applause]") == "Hello world"


def test_long_cjk_query_samples_trigrams_across_the_full_text():
    query, _ = build_trigram_match_query("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥", max_grams=4)

    assert query is not None
    assert '"甲乙丙"' in query
    assert '"酉戌亥"' in query
    assert len(query.split(" OR ")) == 4


def test_episode_coverage_unions_all_retrieved_windows(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_subtitle.sqlite3"
    _build_subtitle_db(db_path)

    result = search_drama_subtitle_lexical_candidates(
        db_path=str(db_path),
        query_text="大哥这是横店吗你们是不是在拍戏接着往下走",
        candidate_limit=5,
        window_limit=20,
    )

    metrics = result["candidates"][0]["match_metrics"]
    assert metrics["query_coverage_rate"] < 1.0
    assert metrics["retrieved_window_query_coverage_rate"] == 1.0
    assert metrics["retrieved_window_shared_match_count"] > metrics["shared_trigram_count"]


def test_short_query_uses_like_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_subtitle.sqlite3"
    _build_subtitle_db(db_path)

    result = search_drama_subtitle_lexical_candidates(
        db_path=str(db_path),
        query_text="横店",
        candidate_limit=5,
        window_limit=20,
    )

    assert result["mode"] == "like_fallback"
    assert result["candidates"][0]["book_id"] == "book-a"


def test_explicit_language_filter_does_not_return_other_languages(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_subtitle.sqlite3"
    _build_subtitle_db(db_path)

    result = search_drama_subtitle_lexical_candidates(
        db_path=str(db_path),
        query_text="大哥这是横店吗你们是不是在拍戏",
        language_code="en",
        candidate_limit=5,
        window_limit=20,
    )

    assert result["query_language_code"] == "zh"
    assert result["language_filter"] == "en"
    assert result["candidates"] == []
    assert result["lexical_index_variant"] == "language_fts_v2"


def test_english_search_uses_contiguous_word_phrases(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_subtitle.sqlite3"
    _build_subtitle_db(db_path)

    result = search_drama_subtitle_lexical_candidates(
        db_path=str(db_path),
        query_text="Isn't Sonia Adrienne's secretary? I am going to see Adrian anyway.",
        candidate_limit=5,
        window_limit=20,
    )

    assert result["mode"] == "fts5_word_phrase"
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["book_id"] == "book-en-match"
    metrics = result["candidates"][0]["match_metrics"]
    assert metrics["match_unit"] == "word_4gram"
    assert metrics["match_unit_label"] == "共享四词短语"
    assert metrics["query_coverage_rate"] == 1.0
    assert "sonia adrienne s secretary" in metrics["shared_trigrams"]


def test_evidence_context_expands_forward_from_the_retrieved_window(tmp_path: Path) -> None:
    db_path = tmp_path / "drama_context.sqlite3"
    conn = connect_db(db_path)
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
                language_code TEXT NOT NULL DEFAULT 'unknown'
            );
            CREATE TABLE drama_subtitle_lines (
                episode_uid TEXT NOT NULL,
                line_order INTEGER NOT NULL,
                start_time TEXT,
                end_time TEXT,
                text_normalized TEXT NOT NULL,
                UNIQUE (episode_uid, line_order)
            );
            """
        )
        conn.execute(
            "INSERT INTO drama_subtitle_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("book-a:1:10-12", "book-a", "命中剧", "book-a:1", 1, 10, 12, "00:00:10,000", "00:00:12,000", "zh"),
        )
        conn.executemany(
            "INSERT INTO drama_subtitle_lines VALUES (?, ?, ?, ?, ?)",
            [
                ("book-a:1", line, f"00:00:{line:02d},000", f"00:00:{line:02d},900", f"字幕第{line}行")
                for line in range(1, 24)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    result = get_drama_subtitle_evidence_context(
        db_path=db_path,
        window_uid="book-a:1:10-12",
        context_chars=600,
        before_lines=2,
    )

    assert result["hit"]["line_start"] == 10
    assert result["context"]["line_start"] == 8
    assert result["context"]["line_end"] == 23
    assert result["context"]["before_line_count"] == 2
    assert result["context"]["after_line_count"] == 11
    assert "字幕第10行" in result["context"]["text"]
    assert "字幕第23行" in result["context"]["text"]
