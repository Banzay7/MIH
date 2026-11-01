-- When each step of a command happened (accepted, ha_called, ha_finished, started or
-- failed), in UTC with milliseconds: tells a slow server from a slow Home Assistant or TV.
ALTER TABLE playback_commands ADD COLUMN stages TEXT;
