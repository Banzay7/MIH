-- Topic alerts already sent (mih.alerts): one push per showing, even when the same
-- programme airs at the same time on several channels. Rows older than
-- MIH_EPG_RETENTION_DAYS are deleted by the alerts job.
CREATE TABLE alerts_sent (
    key          TEXT PRIMARY KEY,      -- title_key + "|" + start time (UTC)
    title_key    TEXT NOT NULL,         -- folded title: back-to-back episodes push once
    title        TEXT NOT NULL,
    start_utc    TEXT NOT NULL,
    channel_ids  TEXT NOT NULL,         -- JSON list, best first
    reasons      TEXT NOT NULL,         -- JSON [{"profile_id", "kind", "value"}]
    sent_at      TEXT NOT NULL,
    pushed       TEXT                   -- sent | failed | off | summary
);
CREATE INDEX alerts_sent_at ON alerts_sent (sent_at);
CREATE INDEX alerts_sent_title ON alerts_sent (title_key, start_utc);
