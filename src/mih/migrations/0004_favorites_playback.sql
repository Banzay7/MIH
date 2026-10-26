-- Favourites (spec 17, profiles) and playback commands (spec 12 C4). One profile,
-- 'default', until family profiles arrive (E2); the key already carries profile_id.

-- target_kind: 'channel' or 'programme'. No foreign key on target_id: a favourite
-- outlives a playlist that briefly loses the channel (spec 5, principle 5).
CREATE TABLE favorites (
    profile_id   TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('channel', 'programme')),
    target_id    TEXT NOT NULL,
    position     INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (profile_id, target_kind, target_id)
);

-- state: accepted -> started | failed. stream_id is the stream MIH picked; its URL is
-- never stored here or returned.
CREATE TABLE playback_commands (
    id               TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL,
    target           TEXT NOT NULL,
    stream_id        TEXT,
    state            TEXT NOT NULL CHECK (state IN ('accepted', 'started', 'failed')),
    idempotency_key  TEXT UNIQUE,
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    error            TEXT
);
