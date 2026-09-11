CREATE TABLE IF NOT EXISTS drama_subtitle_windows (
    window_uid TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    book_name TEXT NOT NULL,
    episode_uid TEXT NOT NULL,
    episode_order INTEGER NOT NULL,
    chapter_id INTEGER,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    time_start TEXT,
    time_end TEXT,
    window_text TEXT NOT NULL,
    window_text_preview TEXT NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    language_code TEXT NOT NULL DEFAULT 'unknown',
    language_confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (episode_uid, line_start, line_end),
    FOREIGN KEY (book_id) REFERENCES drama_books (book_id) ON DELETE CASCADE,
    FOREIGN KEY (episode_uid) REFERENCES drama_episodes (episode_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_windows_book_episode
    ON drama_subtitle_windows (book_id, episode_order, line_start);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_windows_episode
    ON drama_subtitle_windows (episode_uid, line_start, line_end);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_windows_chars
    ON drama_subtitle_windows (char_count DESC, line_count DESC);
