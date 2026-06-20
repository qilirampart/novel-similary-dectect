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
    status_message TEXT,
    error_message TEXT,
    summary_export_path TEXT,
    review_export_path TEXT,
    result_json_path TEXT,
    created_at TEXT NOT NULL,
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
