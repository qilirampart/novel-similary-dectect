CREATE TABLE IF NOT EXISTS datasets (
    dataset_key TEXT PRIMARY KEY,
    dataset_label TEXT NOT NULL,
    source_scope TEXT,
    source_file_name TEXT,
    source_file_sha256 TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id TEXT PRIMARY KEY,
    dataset_key TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    source_file_sha256 TEXT,
    source_row_count INTEGER,
    manifest_path TEXT,
    batch_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
);

CREATE INDEX IF NOT EXISTS idx_ingest_batches_dataset
    ON ingest_batches (dataset_key);

CREATE TABLE IF NOT EXISTS raw_manifest_rows (
    batch_id TEXT NOT NULL,
    row_num INTEGER NOT NULL,
    dataset_key TEXT NOT NULL,
    book_ext_id TEXT NOT NULL,
    book_name TEXT NOT NULL,
    chapter_ext_id INTEGER NOT NULL,
    chapter_name TEXT NOT NULL,
    chapter_order INTEGER NOT NULL,
    url_host TEXT,
    object_key TEXT NOT NULL,
    signed_url TEXT NOT NULL,
    signed_expires_at TEXT,
    raw_rel_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, row_num),
    FOREIGN KEY (batch_id) REFERENCES ingest_batches (batch_id),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
);

CREATE INDEX IF NOT EXISTS idx_raw_manifest_rows_dataset_chapter
    ON raw_manifest_rows (dataset_key, chapter_ext_id);

CREATE INDEX IF NOT EXISTS idx_raw_manifest_rows_dataset_book
    ON raw_manifest_rows (dataset_key, book_ext_id, chapter_order);

CREATE TABLE IF NOT EXISTS books (
    book_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT NOT NULL,
    book_ext_id TEXT NOT NULL,
    book_name TEXT NOT NULL,
    chapter_count INTEGER,
    total_char_count INTEGER,
    avg_chapter_char_count REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dataset_key, book_ext_id),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
);

CREATE INDEX IF NOT EXISTS idx_books_dataset_name
    ON books (dataset_key, book_name);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT NOT NULL,
    book_uid INTEGER NOT NULL,
    chapter_ext_id INTEGER NOT NULL,
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
    raw_file_size INTEGER,
    content_sha256 TEXT,
    char_count_raw INTEGER,
    char_count_clean INTEGER,
    is_empty INTEGER DEFAULT 0,
    is_too_short INTEGER DEFAULT 0,
    has_abnormal_repetition INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dataset_key, chapter_ext_id),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key),
    FOREIGN KEY (book_uid) REFERENCES books (book_uid)
);

CREATE INDEX IF NOT EXISTS idx_chapters_dataset_status
    ON chapters (dataset_key, download_status);

CREATE INDEX IF NOT EXISTS idx_chapters_book_order
    ON chapters (book_uid, chapter_order);

CREATE INDEX IF NOT EXISTS idx_chapters_dataset_book_ext
    ON chapters (dataset_key, book_uid, chapter_ext_id);

CREATE TABLE IF NOT EXISTS chapter_contents (
    chapter_uid INTEGER PRIMARY KEY,
    content_raw TEXT,
    content_clean TEXT,
    content_retrieval TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chapter_uid) REFERENCES chapters (chapter_uid)
);

CREATE TABLE IF NOT EXISTS chapter_exact_dedup_groups (
    group_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    canonical_chapter_uid INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dataset_key, content_sha256),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key),
    FOREIGN KEY (canonical_chapter_uid) REFERENCES chapters (chapter_uid)
);

CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_groups_dataset
    ON chapter_exact_dedup_groups (dataset_key, member_count);

CREATE TABLE IF NOT EXISTS chapter_exact_dedup_members (
    chapter_uid INTEGER PRIMARY KEY,
    group_uid INTEGER NOT NULL,
    dataset_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    is_canonical INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (group_uid) REFERENCES chapter_exact_dedup_groups (group_uid),
    FOREIGN KEY (chapter_uid) REFERENCES chapters (chapter_uid),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
);

CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_members_group
    ON chapter_exact_dedup_members (group_uid, is_canonical);

CREATE INDEX IF NOT EXISTS idx_chapter_exact_dedup_members_dataset
    ON chapter_exact_dedup_members (dataset_key, is_canonical, chapter_uid);

CREATE TABLE IF NOT EXISTS terminal_failures (
    dataset_key TEXT NOT NULL,
    chapter_ext_id INTEGER NOT NULL,
    book_ext_id TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    error_message TEXT,
    raw_rel_path TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_key, chapter_ext_id),
    FOREIGN KEY (dataset_key) REFERENCES datasets (dataset_key)
);

CREATE INDEX IF NOT EXISTS idx_terminal_failures_dataset_book
    ON terminal_failures (dataset_key, book_ext_id);

CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_uid INTEGER NOT NULL,
    chunk_order INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (chapter_uid, chunk_order),
    FOREIGN KEY (chapter_uid) REFERENCES chapters (chapter_uid)
);

CREATE INDEX IF NOT EXISTS idx_semantic_chunks_chapter
    ON semantic_chunks (chapter_uid, chunk_order);

CREATE TABLE IF NOT EXISTS evidence_windows (
    window_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_uid INTEGER NOT NULL,
    window_order INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (chapter_uid, window_order),
    FOREIGN KEY (chapter_uid) REFERENCES chapters (chapter_uid)
);

CREATE INDEX IF NOT EXISTS idx_evidence_windows_chapter
    ON evidence_windows (chapter_uid, window_order);
