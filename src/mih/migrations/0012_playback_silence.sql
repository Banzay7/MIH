-- The agent reports only failures; silence means the stream plays. offered: the stream
-- ids the agent got for the command, failed: the ones it reported as not playing.
ALTER TABLE playback_commands ADD COLUMN offered TEXT;
ALTER TABLE playback_commands ADD COLUMN failed TEXT;
