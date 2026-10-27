-- Playlist discovery: the scout finds public playlists and samples a few streams, the
-- packer checks every new stream of a live playlist and imports the ones that play.

CREATE TABLE discovered_playlists (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    origin          TEXT NOT NULL,  -- seed | github | code | page
    found_via       TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'live', 'dead', 'known', 'duplicate',
                                      'rejected', 'packed', 'retired')),
    reason          TEXT,
    first_seen_at   TEXT NOT NULL,
    scouted_at      TEXT,
    next_scout_at   TEXT,
    misses          INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,
    entries         INTEGER,
    new_entries     INTEGER,
    sampled         INTEGER,
    sample_ok       INTEGER,
    checked         INTEGER NOT NULL DEFAULT 0,
    packed_ok       INTEGER NOT NULL DEFAULT 0,
    packed_at       TEXT,
    source_id       TEXT REFERENCES sources(id) ON DELETE SET NULL
);
CREATE INDEX idx_discovered_status ON discovered_playlists(status, next_scout_at);
CREATE INDEX idx_discovered_hash ON discovered_playlists(content_hash);
-- "Is this URL in the catalog already?" across all sources.
CREATE INDEX idx_streams_locator ON streams(locator_hash);

-- One probe result per stream URL, whichever playlist it came from: forks repeat the
-- same URLs, and a URL checked once is not checked again for a week.
CREATE TABLE discovery_urls (
    url_hash        TEXT PRIMARY KEY,
    ok              INTEGER NOT NULL,
    error_kind      TEXT,
    checked_at      TEXT NOT NULL
);

-- GitHub repositories found by search, scanned for .m3u files one by one.
CREATE TABLE discovered_repos (
    full_name       TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    found_at        TEXT NOT NULL,
    pushed_at       TEXT,
    scanned_at      TEXT,
    files           INTEGER
);

-- Cursors of the harvesters (search page, last scan of a page).
CREATE TABLE discovery_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
