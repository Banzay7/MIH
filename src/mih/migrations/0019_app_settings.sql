-- Settings the owner changes in the web UI: the 18+ mode and its PIN (salted hash).
CREATE TABLE app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
