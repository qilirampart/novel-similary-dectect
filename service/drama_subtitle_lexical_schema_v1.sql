CREATE VIRTUAL TABLE IF NOT EXISTS drama_subtitle_windows_fts
USING fts5(
    window_uid UNINDEXED,
    book_id UNINDEXED,
    book_name UNINDEXED,
    episode_uid UNINDEXED,
    window_text,
    tokenize = 'trigram'
);

-- English subtitle retrieval uses contiguous word phrases instead of character
-- trigrams, preventing incidental matches caused by the small Latin alphabet.
CREATE VIRTUAL TABLE IF NOT EXISTS drama_subtitle_windows_word_fts
USING fts5(
    window_uid UNINDEXED,
    book_id UNINDEXED,
    book_name UNINDEXED,
    episode_uid UNINDEXED,
    window_text,
    tokenize = 'unicode61'
);

-- V2 keeps the corpus language inside FTS. It lets translated fallback search
-- one language before BM25 ranking instead of ranking all language tracks.
CREATE VIRTUAL TABLE IF NOT EXISTS drama_subtitle_windows_fts_lang_v2
USING fts5(
    window_uid UNINDEXED,
    language_code,
    window_text,
    tokenize = 'trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS drama_subtitle_windows_word_fts_lang_v2
USING fts5(
    window_uid UNINDEXED,
    language_code,
    window_text,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS drama_subtitle_lexical_index_metadata (
    index_name TEXT PRIMARY KEY,
    source_window_count INTEGER NOT NULL,
    indexed_window_count INTEGER NOT NULL,
    build_status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
