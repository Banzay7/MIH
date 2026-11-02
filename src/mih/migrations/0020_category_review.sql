-- Channels the automatic rules put into a category on thin evidence, for the owner to
-- confirm or move in the web UI (today: «adult»). Rewritten after every catalog build;
-- a channel with categories set by hand never has a row here.
CREATE TABLE category_review (
    channel_id  TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    reasons     TEXT NOT NULL
);
