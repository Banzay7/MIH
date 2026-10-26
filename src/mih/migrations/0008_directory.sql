-- Channel directory (iptv-org database): names, country, languages and categories of
-- channels worldwide, without streams. Catalog channels link to it by tvg-id or name.

CREATE TABLE directory_channels (
    -- iptv-org id, e.g. ChannelOne.ru
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    alt_names       TEXT,
    network         TEXT,
    country         TEXT,
    categories      TEXT,
    languages       TEXT,
    is_nsfw         INTEGER NOT NULL DEFAULT 0,
    closed          TEXT,
    replaced_by     TEXT,
    website         TEXT,
    logo_url        TEXT,
    -- iptv-org blocklist reason (dmca, nsfw): such channels are never created.
    blocked         TEXT,
    updated_at      TEXT NOT NULL
);

-- Folded name keys (split_name) of each directory channel's name and alt names.
CREATE TABLE directory_names (
    key             TEXT NOT NULL,
    directory_id    TEXT NOT NULL REFERENCES directory_channels(id) ON DELETE CASCADE,
    PRIMARY KEY (key, directory_id)
);

ALTER TABLE channels ADD COLUMN directory_id TEXT;
CREATE INDEX idx_channels_directory ON channels(directory_id);

-- iptv-org playlists tag feeds: "ChannelOne.ru@SD" and "ChannelOne.ru@HD" are one
-- channel. Channel-level tvg-id aliases keep only the part before '@'.
INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display, source_id)
SELECT channel_id, kind, substr(value, 1, instr(value, '@') - 1), display, source_id
FROM channel_aliases WHERE kind = 'tvg-id' AND instr(value, '@') > 1;
DELETE FROM channel_aliases WHERE kind = 'tvg-id' AND instr(value, '@') > 1;
