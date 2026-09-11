CREATE TABLE IF NOT EXISTS drama_books (
    book_id TEXT PRIMARY KEY,
    book_name TEXT NOT NULL,
    source_title TEXT,
    source_tag TEXT,
    episode_count INTEGER NOT NULL DEFAULT 0,
    first10_episode_count INTEGER NOT NULL DEFAULT 0,
    has_any_real_subtitles INTEGER NOT NULL DEFAULT 0,
    first10_has_real_subtitles INTEGER NOT NULL DEFAULT 0,
    total_subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    total_real_subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    first10_subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    first10_real_subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    source_payload_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drama_books_first10_real
    ON drama_books (first10_has_real_subtitles, first10_real_subtitle_line_count DESC, book_id);

CREATE TABLE IF NOT EXISTS drama_episodes (
    episode_uid TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    episode_order INTEGER NOT NULL,
    chapter_id INTEGER,
    chapter_name TEXT NOT NULL DEFAULT '',
    summary TEXT,
    subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    real_subtitle_line_count INTEGER NOT NULL DEFAULT 0,
    first_subtitle_start TEXT,
    last_subtitle_end TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (book_id, episode_order),
    FOREIGN KEY (book_id) REFERENCES drama_books (book_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_episodes_book_order
    ON drama_episodes (book_id, episode_order);

CREATE TABLE IF NOT EXISTS drama_subtitle_lines (
    line_uid TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    episode_uid TEXT NOT NULL,
    episode_order INTEGER NOT NULL,
    chapter_id INTEGER,
    line_order INTEGER NOT NULL,
    source_subtitle_id INTEGER,
    start_time TEXT,
    end_time TEXT,
    speaker TEXT,
    text TEXT NOT NULL,
    text_normalized TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    language_code TEXT NOT NULL DEFAULT 'unknown',
    language_confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (episode_uid, line_order),
    FOREIGN KEY (book_id) REFERENCES drama_books (book_id) ON DELETE CASCADE,
    FOREIGN KEY (episode_uid) REFERENCES drama_episodes (episode_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_lines_episode_order
    ON drama_subtitle_lines (episode_uid, line_order);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_lines_book_episode
    ON drama_subtitle_lines (book_id, episode_order, line_order);
