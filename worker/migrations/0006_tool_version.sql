-- The Claude Code or Codex version last seen on a row. NULL means a collector older than 0.7.1.
ALTER TABLE tracker.usage ADD COLUMN tool_version TEXT;
