-- The "На ТВ" button through the household's agent: a command waits as 'accepted'
-- until the agent takes it (taken_at) and reports whether the stream started.
ALTER TABLE playback_commands ADD COLUMN taken_at TEXT;
CREATE INDEX idx_playback_pending ON playback_commands(state, created_at);
