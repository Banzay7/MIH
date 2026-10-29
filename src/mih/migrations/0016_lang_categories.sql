-- The last summary of each job in the web UI's other languages: {"en": "...", "vi": "..."}.
ALTER TABLE jobs ADD COLUMN last_summary_i18n TEXT;

-- Each channel's categories (news, kids, adult...) and languages (ISO 639-1) as the catalog
-- worked them out the last time it was built (mih.export.build_catalog). Rewritten after
-- every job, so a better rule reaches every channel with no manual step. The search reads
-- it: the genre filter lists channels of that category, the language filter and list.
CREATE TABLE channel_tags (
    channel_id  TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('category', 'language')),
    value       TEXT NOT NULL,
    PRIMARY KEY (channel_id, kind, value)
) WITHOUT ROWID;
CREATE INDEX channel_tags_value ON channel_tags (kind, value, channel_id);
