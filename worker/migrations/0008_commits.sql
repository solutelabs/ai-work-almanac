-- One row per commit on a default branch, for a view of all work with or without AI. Added to on each
-- upload rather than replaced, because GitHub only gives line counts for a recent window. No messages.
CREATE TABLE tracker.commits (
  repo TEXT NOT NULL,
  oid TEXT NOT NULL,
  person TEXT NOT NULL,
  authored_at TEXT NOT NULL,      -- UTC, like PR times
  ai BOOLEAN NOT NULL,            -- the commit names an AI tool
  lines_added BIGINT NOT NULL,    -- 0 for commits over 20,000 lines, which are treated as bulk imports
  lines_removed BIGINT NOT NULL,
  PRIMARY KEY (repo, oid)
);
CREATE INDEX commits_authored_at ON tracker.commits(authored_at);
GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.commits TO tracker_app;
