-- What the service did and what happened to it: the feed of the status screen and the
-- source of push notifications. Rows older than MIH_HISTORY_DAYS are deleted.
CREATE TABLE events (
    id      TEXT PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,              -- job.sources, job.health, service.started, ...
    level   TEXT NOT NULL,              -- routine | info | warning | error
    title   TEXT NOT NULL,
    body    TEXT NOT NULL DEFAULT '',
    data    TEXT,                       -- JSON
    pushed  TEXT                        -- sent | failed | off | repeat | NULL (below MIH_NOTIFY_LEVEL)
);
CREATE INDEX events_at ON events (at);

-- Background jobs: when each runs next and how its last run went.
CREATE TABLE jobs (
    name              TEXT PRIMARY KEY,  -- sources | epg | health | digest
    next_run_at       TEXT,              -- NULL: switched off, runs only on request
    running_since     TEXT,
    last_started_at   TEXT,
    last_finished_at  TEXT,
    last_status       TEXT,              -- ok | warning | error
    last_summary      TEXT
);
