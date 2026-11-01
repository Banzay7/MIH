-- Categories the owner set by hand in the web UI: they replace the automatic ones
-- (mih.categories) in the catalog, the lists and the search. The first one is the primary.
CREATE TABLE channel_category_overrides (
    channel_id  TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    categories  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- channel_tags also keeps each channel's primary category (kind 'primary'), so a channel
-- card shows it first without building the whole catalog.
CREATE TABLE channel_tags_new (
    channel_id  TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('category', 'language', 'primary')),
    value       TEXT NOT NULL,
    PRIMARY KEY (channel_id, kind, value)
) WITHOUT ROWID;
INSERT INTO channel_tags_new SELECT channel_id, kind, value FROM channel_tags;
DROP TABLE channel_tags;
ALTER TABLE channel_tags_new RENAME TO channel_tags;
CREATE INDEX channel_tags_value ON channel_tags (kind, value, channel_id);
