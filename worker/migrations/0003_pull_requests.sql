-- Replaced on every git upload. Only what the dashboard needs: no titles or descriptions.
CREATE TABLE tracker.pull_requests (
  repo TEXT NOT NULL,
  number BIGINT NOT NULL,
  person TEXT NOT NULL,
  created_at TEXT NOT NULL,
  merged_at TEXT,        -- NULL means still open
  human_review BOOLEAN NOT NULL,  -- reviewed by a person other than the author
  any_review BOOLEAN NOT NULL,    -- reviewed by anyone else, including a bot
  PRIMARY KEY (repo, number)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.pull_requests TO tracker_app;
