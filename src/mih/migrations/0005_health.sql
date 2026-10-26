-- Stream health (spec 17): probe and playback observations, the lifecycle state they
-- drive, and a journal of check runs.

-- Lifecycle bookkeeping. A "streak" is the run of counted failures since the last
-- success; windows are fixed time buckets, so three quick retries count as one window.
ALTER TABLE streams ADD COLUMN status_changed_at TEXT;
ALTER TABLE streams ADD COLUMN last_checked_at TEXT;
ALTER TABLE streams ADD COLUMN next_check_at TEXT;
ALTER TABLE streams ADD COLUMN last_ok_at TEXT;
ALTER TABLE streams ADD COLUMN first_fail_at TEXT;
ALTER TABLE streams ADD COLUMN streak_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE streams ADD COLUMN streak_windows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE streams ADD COLUMN last_fail_window INTEGER;
-- Playback on a real device outranks any probe (spec 17, 14.1).
ALTER TABLE streams ADD COLUMN last_device_ok_at TEXT;
ALTER TABLE streams ADD COLUMN last_device_fail_at TEXT;
CREATE INDEX idx_streams_next_check ON streams(next_check_at);

CREATE TABLE health_runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    vantage         TEXT NOT NULL,
    -- done | vantage_offline (the prober itself had no network; nothing was judged)
    status          TEXT NOT NULL,
    stats           TEXT
);

CREATE TABLE stream_observations (
    id              TEXT PRIMARY KEY,
    stream_id       TEXT NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    run_id          TEXT REFERENCES health_runs(id) ON DELETE SET NULL,
    checked_at      TEXT NOT NULL,
    -- server: this install's prober; home: a prober in the home network;
    -- device: a real playback attempt reported by a TV or player.
    vantage         TEXT NOT NULL CHECK (vantage IN ('server', 'home', 'device')),
    kind            TEXT NOT NULL CHECK (kind IN ('probe', 'playback')),
    success         INTEGER NOT NULL,
    -- 0 when the result says more about the path than the stream (prober offline,
    -- provider outage, rate limit, a recent device result that disagrees).
    counted         INTEGER NOT NULL,
    note            TEXT,
    error_kind      TEXT,
    http_status     INTEGER,
    stage           TEXT,
    startup_ms      INTEGER,
    resolution      TEXT,
    codec           TEXT,
    bitrate         INTEGER
);
CREATE INDEX idx_observations_stream ON stream_observations(stream_id, checked_at);

CREATE TABLE stream_health_events (
    id              TEXT PRIMARY KEY,
    stream_id       TEXT NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    cause           TEXT NOT NULL,
    observation_id  TEXT REFERENCES stream_observations(id) ON DELETE SET NULL
);
CREATE INDEX idx_health_events_stream ON stream_health_events(stream_id, at);
