-- Comma-separated hashes of working folders, w for a git worktree and m for others. NULL means a
-- collector older than 0.7.0. A separate migration because 0004 had already been applied without it.
ALTER TABLE tracker.usage ADD COLUMN folders TEXT;
