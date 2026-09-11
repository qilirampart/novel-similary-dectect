CREATE TABLE IF NOT EXISTS drama_subtitle_window_embedding_sync_state (
    window_uid TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (window_uid, collection_name, embedding_model, embedding_dim),
    FOREIGN KEY (window_uid) REFERENCES drama_subtitle_windows (window_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drama_subtitle_embedding_sync_collection
    ON drama_subtitle_window_embedding_sync_state (collection_name, embedding_model, embedding_dim);
