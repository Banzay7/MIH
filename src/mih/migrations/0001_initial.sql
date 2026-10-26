-- Core catalog schema (spec 15). Only the tables the first import pipeline needs;
-- EPG, taxonomy, devices and profiles arrive in later migrations.

CREATE TABLE sources (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('m3u')),
    name            TEXT NOT NULL UNIQUE,
    -- URL may carry credentials, so it lives here and is never returned by the API.
    url             TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL,
    etag            TEXT,
    last_modified   TEXT
);

CREATE TABLE source_snapshots (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    fetched_at      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    item_count      INTEGER NOT NULL,
    -- applied: catalog updated; unchanged: same content as the active snapshot;
    -- rejected: failed a safety check, the previous catalog stays in place.
    status          TEXT NOT NULL CHECK (status IN ('applied', 'unchanged', 'rejected')),
    reason          TEXT,
    blob_path       TEXT,
    stats           TEXT
);
CREATE INDEX idx_snapshots_source ON source_snapshots(source_id, fetched_at);

CREATE TABLE source_records (
    id                  TEXT PRIMARY KEY,
    snapshot_id         TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,
    position            INTEGER NOT NULL,
    external_key        TEXT,
    raw_fingerprint     TEXT NOT NULL,
    normalized_payload  TEXT NOT NULL,
    stream_id           TEXT REFERENCES streams(id)
);
CREATE INDEX idx_records_snapshot ON source_records(snapshot_id);

CREATE TABLE channels (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    country         TEXT,
    logo_url        TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
);

-- kind: 'name' (normalized display name) or an external id scheme such as 'tvg-id'.
CREATE TABLE channel_aliases (
    channel_id      TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL,
    display         TEXT,
    source_id       TEXT REFERENCES sources(id) ON DELETE SET NULL,
    PRIMARY KEY (channel_id, kind, value)
);
CREATE INDEX idx_aliases_lookup ON channel_aliases(kind, value);

CREATE TABLE feeds (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    -- '' means "no region marker"; kept non-null so the unique key works.
    region          TEXT NOT NULL DEFAULT '',
    offset_minutes  INTEGER NOT NULL DEFAULT 0,
    audio_lang      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (channel_id, region, offset_minutes)
);

CREATE TABLE streams (
    id                  TEXT PRIMARY KEY,
    feed_id             TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    locator_hash        TEXT NOT NULL,
    format              TEXT,
    quality_label       TEXT,
    status              TEXT NOT NULL DEFAULT 'unverified'
                        CHECK (status IN ('unverified', 'new', 'active', 'suspect',
                                          'quarantine', 'archived')),
    availability_reason TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    UNIQUE (source_id, locator_hash)
);
CREATE INDEX idx_streams_feed ON streams(feed_id);

-- Secrets live apart from catalog rows (spec 15, item 5): URLs and request headers
-- can contain tokens and must never reach logs, exports or API responses.
CREATE TABLE stream_locators (
    stream_id       TEXT PRIMARY KEY REFERENCES streams(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    headers         TEXT
);

CREATE TABLE match_decisions (
    id              TEXT PRIMARY KEY,
    target_kind     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    record_id       TEXT REFERENCES source_records(id) ON DELETE CASCADE,
    rule            TEXT NOT NULL,
    score           REAL NOT NULL,
    evidence        TEXT,
    decision        TEXT NOT NULL CHECK (decision IN ('auto', 'needs_review', 'approved',
                                                      'rejected')),
    decided_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_match_review ON match_decisions(decision);
