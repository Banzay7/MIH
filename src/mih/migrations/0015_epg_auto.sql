-- Programmes of a guide channel in the last applied snapshot of its guide. A guide
-- channel without programmes gives no channel a guide. NULL: not counted yet (imported
-- before this column existed), taken as having programmes.
ALTER TABLE epg_channels ADD COLUMN programme_count INTEGER;

-- 1: added by the guide autopilot (mih.epg.auto). Its channels are only matched to the
-- catalog; channels it lists that no playlist carries are not added to the catalog.
ALTER TABLE epg_sources ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;

-- Public guides the autopilot tried, so a guide that gave nothing is not tried again
-- soon. verdict: trial (added, waiting for its first import), kept, useless (no working
-- channel got a guide only from it), broken (could not be fetched or read).
CREATE TABLE epg_auto (
    url             TEXT PRIMARY KEY,
    guide           TEXT NOT NULL,
    country         TEXT NOT NULL,
    epg_source_id   TEXT REFERENCES epg_sources(id) ON DELETE SET NULL,
    verdict         TEXT NOT NULL DEFAULT 'trial'
                    CHECK (verdict IN ('trial', 'kept', 'useless', 'broken')),
    gain            INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL,
    judged_at       TEXT
);
