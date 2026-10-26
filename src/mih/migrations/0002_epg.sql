-- EPG (spec 19): XMLTV sources and snapshots, guide channels mapped to feeds,
-- programmes (the work) and airings (one showing, UTC).

-- tvg-id seen on a feed's streams. Channel aliases say which brand an id belongs to;
-- this says which version of it (region, timeshift), which EPG mapping needs.
CREATE TABLE feed_aliases (
    feed_id         TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL,
    PRIMARY KEY (feed_id, kind, value)
);
CREATE INDEX idx_feed_aliases_lookup ON feed_aliases(kind, value);

INSERT OR IGNORE INTO feed_aliases (feed_id, kind, value)
SELECT s.feed_id, 'tvg-id', lower(json_extract(r.normalized_payload, '$.attrs."tvg-id"'))
FROM source_records r JOIN streams s ON s.id = r.stream_id
WHERE coalesce(json_extract(r.normalized_payload, '$.attrs."tvg-id"'), '') != '';

CREATE TABLE epg_sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    -- May carry a subscriber token, like sources.url; never returned by the API.
    url             TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL,
    etag            TEXT,
    last_modified   TEXT
);

CREATE TABLE epg_snapshots (
    id              TEXT PRIMARY KEY,
    epg_source_id   TEXT NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
    fetched_at      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    channel_count   INTEGER NOT NULL,
    programme_count INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('applied', 'unchanged', 'rejected')),
    reason          TEXT,
    blob_path       TEXT,
    stats           TEXT
);
CREATE INDEX idx_epg_snapshots_source ON epg_snapshots(epg_source_id, fetched_at);

-- A <channel> of a guide: an observation, not a catalog channel.
CREATE TABLE epg_channels (
    id              TEXT PRIMARY KEY,
    epg_source_id   TEXT NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
    xmltv_id        TEXT NOT NULL,
    display_names   TEXT NOT NULL,
    icon_url        TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (epg_source_id, xmltv_id)
);

-- Which feed a guide channel describes. feed_id is NULL while a person has to choose
-- (needs_review) or after they said "none of these" (rejected). approved and rejected
-- rows are never overwritten by an import.
CREATE TABLE epg_mappings (
    epg_channel_id  TEXT PRIMARY KEY REFERENCES epg_channels(id) ON DELETE CASCADE,
    feed_id         TEXT REFERENCES feeds(id) ON DELETE SET NULL,
    rule            TEXT NOT NULL,
    score           REAL NOT NULL,
    evidence        TEXT,
    decision        TEXT NOT NULL CHECK (decision IN ('auto', 'needs_review', 'approved',
                                                      'rejected')),
    decided_by      TEXT NOT NULL DEFAULT 'system',
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_epg_mappings_feed ON epg_mappings(feed_id);
CREATE INDEX idx_epg_mappings_decision ON epg_mappings(decision);

-- Identified by content, so a repeat with the same title and description is the same
-- programme with another airing (spec 15, item 2).
CREATE TABLE programmes (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    sub_title       TEXT,
    description     TEXT,
    -- All localized variants as JSON {"titles": [[lang, text], ...], "descs": ...}.
    localized       TEXT NOT NULL,
    categories      TEXT NOT NULL,
    episode_num     TEXT,
    lang            TEXT,
    icon_url        TEXT,
    rating          TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE airings (
    id              TEXT PRIMARY KEY,
    epg_channel_id  TEXT NOT NULL REFERENCES epg_channels(id) ON DELETE CASCADE,
    programme_id    TEXT NOT NULL REFERENCES programmes(id),
    start_utc       TEXT NOT NULL,
    stop_utc        TEXT NOT NULL,
    -- The time exactly as the guide wrote it, offset included.
    start_raw       TEXT NOT NULL,
    -- scheduled, or cancelled when a later snapshot of the same window dropped it.
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'cancelled')),
    -- Bumped each time the guide changes this slot (title, stop time, un-cancel).
    version         INTEGER NOT NULL DEFAULT 1,
    snapshot_id     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (epg_channel_id, start_utc)
);
CREATE INDEX idx_airings_time ON airings(start_utc, stop_utc);
CREATE INDEX idx_airings_programme ON airings(programme_id);
