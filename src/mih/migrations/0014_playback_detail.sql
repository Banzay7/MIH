-- What the device said went wrong, in its own words (a Home Assistant script's error).
ALTER TABLE playback_commands ADD COLUMN detail TEXT;
