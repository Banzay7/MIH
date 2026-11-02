-- How long a channel took to open on the TV, one row per launch that was timed.
-- outcome 'opened': the device said it plays after open_ms; 'slow': it was still
-- loading when the wait ran out (open_ms is then a lower bound). source: who timed it
-- ('ha' = the Home Assistant script returned, 'agent' = the agent reported, 'device' =
-- a player's own report).
CREATE TABLE open_times (
    id          INTEGER PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    stream_id   TEXT,
    command_id  TEXT,
    source      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    open_ms     INTEGER NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX idx_open_times_channel ON open_times(channel_id, at);
CREATE INDEX idx_playback_created ON playback_commands(created_at);
