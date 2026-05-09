CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id TEXT PRIMARY KEY,
    source_file_name TEXT NOT NULL,
    source_file_sha256 TEXT,
    source_row_count INTEGER,
    created_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS raw_manifest_rows (
    batch_id TEXT NOT NULL,
    row_num INTEGER NOT NULL,
    book_ext_id TEXT NOT NULL,
    book_name TEXT NOT NULL,
    chapter_id INTEGER NOT NULL,
    chapter_name TEXT NOT NULL,
    chapter_order INTEGER NOT NULL,
    url_host TEXT,
    object_key TEXT NOT NULL,
    signed_url TEXT NOT NULL,
    signed_expires_at TEXT,
    raw_rel_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, row_num)
);

CREATE INDEX IF NOT EXISTS idx_raw_manifest_rows_chapter
    ON raw_manifest_rows (chapter_id);

CREATE TABLE IF NOT EXISTS books (
    book_ext_id TEXT PRIMARY KEY,
    book_name TEXT NOT NULL,
    chapter_count INTEGER,
    total_char_count INTEGER,
    avg_chapter_char_count REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_id INTEGER PRIMARY KEY,
    book_ext_id TEXT NOT NULL,
    chapter_name TEXT NOT NULL,
    chapter_order INTEGER NOT NULL,
    url_host TEXT,
    object_key TEXT NOT NULL,
    signed_url TEXT NOT NULL,
    signed_expires_at TEXT,
    downloaded_at TEXT,
    download_status TEXT,
    download_error TEXT,
    raw_file_path TEXT,
    content_sha256 TEXT,
    char_count_raw INTEGER,
    char_count_clean INTEGER,
    is_empty INTEGER DEFAULT 0,
    is_too_short INTEGER DEFAULT 0,
    has_abnormal_repetition INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (book_ext_id) REFERENCES books (book_ext_id)
);

CREATE INDEX IF NOT EXISTS idx_chapters_book_order
    ON chapters (book_ext_id, chapter_order);

CREATE INDEX IF NOT EXISTS idx_chapters_download_status
    ON chapters (download_status);

CREATE TABLE IF NOT EXISTS chapter_contents (
    chapter_id INTEGER PRIMARY KEY,
    content_raw TEXT,
    content_clean TEXT,
    content_retrieval TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chapter_id) REFERENCES chapters (chapter_id)
);

CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_id TEXT PRIMARY KEY,
    chapter_id INTEGER NOT NULL,
    chunk_order INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (chapter_id) REFERENCES chapters (chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_chunks_chapter
    ON semantic_chunks (chapter_id, chunk_order);

CREATE TABLE IF NOT EXISTS evidence_windows (
    window_id TEXT PRIMARY KEY,
    chapter_id INTEGER NOT NULL,
    window_order INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (chapter_id) REFERENCES chapters (chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_windows_chapter
    ON evidence_windows (chapter_id, window_order);
