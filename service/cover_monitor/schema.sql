PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cover_schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cover_operators (
    operator_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    external_id TEXT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_cover_operators_workspace_active
    ON cover_operators (workspace_key, active, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cover_operators_workspace_name
    ON cover_operators (workspace_key, name);

CREATE TABLE IF NOT EXISTS cover_channels (
    channel_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    operator_pk INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_scan_at TEXT,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_key, platform, channel_id),
    FOREIGN KEY (operator_pk) REFERENCES cover_operators (operator_pk)
);

CREATE INDEX IF NOT EXISTS idx_cover_channels_workspace_active
    ON cover_channels (workspace_key, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS cover_channel_operator_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_pk INTEGER NOT NULL,
    operator_pk INTEGER,
    changed_by_user_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT,
    FOREIGN KEY (channel_pk) REFERENCES cover_channels (channel_pk) ON DELETE CASCADE,
    FOREIGN KEY (operator_pk) REFERENCES cover_operators (operator_pk)
);

CREATE TABLE IF NOT EXISTS cover_videos (
    video_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    channel_pk INTEGER NOT NULL,
    title TEXT NOT NULL,
    video_url TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    upload_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_key, platform, video_id),
    FOREIGN KEY (channel_pk) REFERENCES cover_channels (channel_pk)
);

CREATE INDEX IF NOT EXISTS idx_cover_videos_workspace_channel
    ON cover_videos (workspace_key, channel_pk, upload_date DESC, video_pk DESC);

CREATE TABLE IF NOT EXISTS cover_import_batches (
    import_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    import_kind TEXT NOT NULL CHECK (import_kind IN ('baseline', 'channels', 'videos')),
    status TEXT NOT NULL CHECK (status IN ('uploaded', 'previewed', 'confirmed', 'running', 'completed', 'failed')),
    source_file_name TEXT NOT NULL,
    source_file_sha256 TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    sheet_name TEXT,
    mapping_json TEXT NOT NULL DEFAULT '{}',
    stats_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    finished_at TEXT,
    UNIQUE (workspace_key, import_kind, source_file_sha256)
);

CREATE TABLE IF NOT EXISTS cover_import_rows (
    import_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    status TEXT NOT NULL,
    normalized_key TEXT,
    raw_json TEXT NOT NULL,
    normalized_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (import_id, source_sheet, source_row),
    FOREIGN KEY (import_id) REFERENCES cover_import_batches (import_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cover_import_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    conflict_type TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    existing_json TEXT NOT NULL,
    incoming_json TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_status IN ('unresolved', 'accepted_existing', 'accepted_incoming', 'ignored')),
    created_at TEXT NOT NULL,
    UNIQUE (import_id, source_sheet, source_row, conflict_type),
    FOREIGN KEY (import_id) REFERENCES cover_import_batches (import_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cover_import_conflicts_batch
    ON cover_import_conflicts (import_id, resolution_status, conflict_id);

CREATE TABLE IF NOT EXISTS cover_historical_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    video_pk INTEGER NOT NULL,
    import_row_id INTEGER NOT NULL,
    overall_risk TEXT NOT NULL CHECK (overall_risk IN ('safe', 'review', 'risk', 'unknown')),
    risk_tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    evidence TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    evidence_status TEXT NOT NULL DEFAULT 'evidence_missing'
        CHECK (evidence_status IN ('available', 'evidence_missing')),
    model_version TEXT NOT NULL DEFAULT 'legacy_unknown',
    observed_at TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (workspace_key, import_row_id),
    FOREIGN KEY (video_pk) REFERENCES cover_videos (video_pk),
    FOREIGN KEY (import_row_id) REFERENCES cover_import_rows (import_row_id)
);

CREATE INDEX IF NOT EXISTS idx_cover_historical_observations_video
    ON cover_historical_observations (workspace_key, video_pk, imported_at DESC);

CREATE TABLE IF NOT EXISTS cover_runs (
    run_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'pause_requested', 'paused', 'cancel_requested', 'cancelled', 'completed', 'partial_failed', 'failed')),
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'schedule')),
    intensity TEXT NOT NULL CHECK (intensity IN ('conservative', 'standard', 'strict')),
    params_json TEXT NOT NULL,
    scope_snapshot_json TEXT NOT NULL,
    model_snapshot_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    total_item_count INTEGER NOT NULL DEFAULT 0,
    completed_item_count INTEGER NOT NULL DEFAULT 0,
    failed_item_count INTEGER NOT NULL DEFAULT 0,
    worker_name TEXT,
    worker_lease_token TEXT,
    last_heartbeat_at TEXT,
    status_message TEXT,
    error_message TEXT,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cover_runs_workspace_created
    ON cover_runs (workspace_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cover_runs_claim
    ON cover_runs (status, created_at, run_id);

CREATE TABLE IF NOT EXISTS cover_run_channels (
    run_channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    channel_pk INTEGER NOT NULL,
    operator_snapshot_json TEXT NOT NULL,
    scan_status TEXT NOT NULL DEFAULT 'pending',
    checkpoint_json TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    completeness TEXT NOT NULL DEFAULT 'pending' CHECK (completeness IN ('pending', 'complete', 'partial', 'failed')),
    error_message TEXT,
    UNIQUE (run_id, channel_pk),
    FOREIGN KEY (run_id) REFERENCES cover_runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY (channel_pk) REFERENCES cover_channels (channel_pk)
);

CREATE TABLE IF NOT EXISTS cover_task_items (
    task_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    video_pk INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('new_video', 'historical_risk', 'retry_unknown', 'manual')),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    worker_lease_token TEXT,
    last_heartbeat_at TEXT,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (run_id, video_pk, reason),
    FOREIGN KEY (run_id) REFERENCES cover_runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY (video_pk) REFERENCES cover_videos (video_pk)
);

CREATE INDEX IF NOT EXISTS idx_cover_task_items_claim
    ON cover_task_items (status, next_retry_at, task_item_id);

CREATE INDEX IF NOT EXISTS idx_cover_task_items_run_claim
    ON cover_task_items (run_id, status, next_retry_at, task_item_id);

CREATE TABLE IF NOT EXISTS cover_assets (
    asset_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    video_pk INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    original_url TEXT NOT NULL,
    fetched_url TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_key, video_pk, content_sha256),
    FOREIGN KEY (video_pk) REFERENCES cover_videos (video_pk)
);

CREATE TABLE IF NOT EXISTS cover_detections (
    detection_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_item_id INTEGER NOT NULL,
    video_pk INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    overall_risk TEXT NOT NULL CHECK (overall_risk IN ('safe', 'review', 'risk', 'unknown')),
    risk_tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    intensity TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES cover_runs (run_id),
    FOREIGN KEY (task_item_id) REFERENCES cover_task_items (task_item_id),
    FOREIGN KEY (video_pk) REFERENCES cover_videos (video_pk),
    FOREIGN KEY (asset_id) REFERENCES cover_assets (asset_id)
);

CREATE INDEX IF NOT EXISTS idx_cover_detections_workspace_risk
    ON cover_detections (workspace_key, overall_risk, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cover_detections_task_item
    ON cover_detections (task_item_id);

CREATE TABLE IF NOT EXISTS cover_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_item_id INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    request_id TEXT,
    error_type TEXT,
    error_message TEXT,
    usage_json TEXT,
    duration_seconds REAL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (task_item_id, attempt_no, stage),
    FOREIGN KEY (task_item_id) REFERENCES cover_task_items (task_item_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cover_risk_cases (
    case_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    video_pk INTEGER NOT NULL,
    opened_detection_id TEXT NOT NULL,
    current_status TEXT NOT NULL CHECK (current_status IN ('open', 'needs_review', 'confirmed_rectified', 'false_positive', 'unavailable', 'closed')),
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (workspace_key, video_pk, opened_detection_id),
    FOREIGN KEY (video_pk) REFERENCES cover_videos (video_pk),
    FOREIGN KEY (opened_detection_id) REFERENCES cover_detections (detection_id)
);

CREATE TABLE IF NOT EXISTS cover_case_reviews (
    case_review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    detection_id TEXT,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    reviewed_by_user_id INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES cover_risk_cases (case_id) ON DELETE CASCADE,
    FOREIGN KEY (detection_id) REFERENCES cover_detections (detection_id)
);

CREATE TABLE IF NOT EXISTS cover_report_exports (
    export_id TEXT PRIMARY KEY,
    workspace_key TEXT NOT NULL,
    run_id TEXT,
    export_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    filter_snapshot_json TEXT NOT NULL,
    storage_key TEXT,
    error_message TEXT,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES cover_runs (run_id)
);
