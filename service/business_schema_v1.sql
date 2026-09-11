CREATE TABLE IF NOT EXISTS app_users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'operator',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_users_active
    ON app_users (is_active, username);

CREATE TABLE IF NOT EXISTS app_user_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES app_users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_app_user_sessions_user
    ON app_user_sessions (user_id, expires_at DESC);

CREATE INDEX IF NOT EXISTS idx_app_user_sessions_expires
    ON app_user_sessions (expires_at);

CREATE TABLE IF NOT EXISTS compare_tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL DEFAULT 'batch_compare',
    status TEXT NOT NULL,
    owner_user_id INTEGER,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    detection_mode TEXT NOT NULL,
    created_by TEXT,
    source_file_name TEXT NOT NULL,
    source_file_ext TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    source_file_sha256 TEXT NOT NULL,
    source_file_size INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    accepted_input_count INTEGER,
    completed_input_count INTEGER NOT NULL DEFAULT 0,
    failed_input_count INTEGER NOT NULL DEFAULT 0,
    worker_name TEXT,
    worker_lease_token TEXT,
    status_message TEXT,
    error_message TEXT,
    summary_export_path TEXT,
    review_export_path TEXT,
    result_json_path TEXT,
    created_at TEXT NOT NULL,
    queue_entered_at TEXT,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    deleted_at TEXT,
    finished_at TEXT,
    last_heartbeat_at TEXT,
    FOREIGN KEY (owner_user_id) REFERENCES app_users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_compare_tasks_status_created
    ON compare_tasks (is_deleted, status, created_at);

CREATE INDEX IF NOT EXISTS idx_compare_tasks_created
    ON compare_tasks (is_deleted, created_at DESC);

CREATE TABLE IF NOT EXISTS compare_task_items (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    item_order INTEGER NOT NULL,
    source_ref TEXT,
    source_short_drama TEXT,
    source_novel_name TEXT,
    source_excel_row TEXT,
    source_episode TEXT,
    source_author TEXT,
    source_platform TEXT,
    source_display_title TEXT,
    source_description TEXT,
    query_text TEXT NOT NULL,
    query_text_preview TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    semantic_status TEXT,
    top1_book_name TEXT,
    top1_chapter_name TEXT,
    top1_review_label TEXT,
    top1_confidence_label TEXT,
    top1_fine_score REAL,
    result_payload_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, item_order),
    FOREIGN KEY (task_id) REFERENCES compare_tasks (task_id)
);

CREATE INDEX IF NOT EXISTS idx_compare_task_items_task_status
    ON compare_task_items (task_id, status, item_order);

CREATE INDEX IF NOT EXISTS idx_compare_task_items_task_order
    ON compare_task_items (task_id, item_order);

CREATE TABLE IF NOT EXISTS compare_task_item_payloads (
    result_id INTEGER PRIMARY KEY,
    query_text TEXT NOT NULL,
    result_payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (result_id) REFERENCES compare_task_items (result_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compare_task_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL UNIQUE,
    review_status TEXT NOT NULL,
    reviewer_name TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (result_id) REFERENCES compare_task_items (result_id)
);

CREATE INDEX IF NOT EXISTS idx_compare_task_reviews_status
    ON compare_task_reviews (review_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS drama_subtitle_tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    owner_user_id INTEGER,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    source_file_name TEXT NOT NULL,
    source_file_ext TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    source_file_sha256 TEXT NOT NULL,
    source_file_size INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    accepted_input_count INTEGER NOT NULL DEFAULT 0,
    completed_input_count INTEGER NOT NULL DEFAULT 0,
    failed_input_count INTEGER NOT NULL DEFAULT 0,
    worker_name TEXT,
    worker_lease_token TEXT,
    status_message TEXT,
    error_message TEXT,
    summary_export_path TEXT,
    result_json_path TEXT,
    created_at TEXT NOT NULL,
    queue_entered_at TEXT,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    deleted_at TEXT,
    finished_at TEXT,
    last_heartbeat_at TEXT,
    FOREIGN KEY (owner_user_id) REFERENCES app_users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_tasks_owner_created
    ON drama_subtitle_tasks (owner_user_id, is_deleted, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_tasks_status_created
    ON drama_subtitle_tasks (is_deleted, status, created_at);

CREATE TABLE IF NOT EXISTS drama_subtitle_task_items (
    task_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    item_order INTEGER NOT NULL,
    source_ref TEXT,
    source_short_drama TEXT,
    source_episode TEXT,
    source_author TEXT,
    source_platform TEXT,
    source_display_title TEXT,
    source_description TEXT,
    source_excel_row TEXT,
    source_video_id TEXT,
    source_channel TEXT,
    source_upload_date TEXT,
    source_caption_language TEXT,
    source_caption_source TEXT,
    source_segment_order INTEGER,
    source_time_start TEXT,
    source_time_end TEXT,
    source_text_original TEXT,
    query_text TEXT NOT NULL,
    query_text_preview TEXT NOT NULL,
    query_language_code TEXT,
    query_language_confidence REAL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    matched_book_id TEXT,
    matched_book_name TEXT,
    matched_episode_order INTEGER,
    matched_language_code TEXT,
    lexical_score REAL,
    evidence_window_uid TEXT,
    evidence_time_start TEXT,
    evidence_time_end TEXT,
    result_payload_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, item_order),
    FOREIGN KEY (task_id) REFERENCES drama_subtitle_tasks (task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_task_items_task_status
    ON drama_subtitle_task_items (task_id, status, item_order);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_task_items_task_score
    ON drama_subtitle_task_items (task_id, lexical_score DESC, item_order);

CREATE TABLE IF NOT EXISTS drama_subtitle_task_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_item_id INTEGER NOT NULL UNIQUE,
    review_status TEXT NOT NULL,
    reviewer_name TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_item_id) REFERENCES drama_subtitle_task_items (task_item_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_task_reviews_status
    ON drama_subtitle_task_reviews (review_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS drama_subtitle_translation_cache (
    cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    source_text_sha256 TEXT NOT NULL,
    source_language_code TEXT NOT NULL,
    target_language_code TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    source_char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        owner_user_id,
        source_text_sha256,
        source_language_code,
        target_language_code,
        provider_key
    ),
    FOREIGN KEY (owner_user_id) REFERENCES app_users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_translation_cache_lookup
    ON drama_subtitle_translation_cache (
        owner_user_id,
        source_text_sha256,
        source_language_code,
        target_language_code,
        provider_key
    );
