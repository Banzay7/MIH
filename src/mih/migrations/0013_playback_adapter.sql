-- Who carried out a playback command: 'agent' (MCP queue) or 'ha' (a Home Assistant
-- script, called directly). NULL on the commands made before.
ALTER TABLE playback_commands ADD COLUMN adapter TEXT;
