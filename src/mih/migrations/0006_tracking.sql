-- Topics a profile follows. With MIH_EPG_SCOPE=tracked the guide import keeps, on
-- channels nobody marked as favourite, only programmes whose title, sub-title or
-- category matches one of these (folded, word prefix).
CREATE TABLE tracked_topics (
    profile_id   TEXT NOT NULL,
    topic        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (profile_id, topic)
);
