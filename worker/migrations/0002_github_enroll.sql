-- Developers sign in with GitHub. One person can have several laptops, so tokens get their own table.
CREATE TABLE tracker.tokens (
  hash TEXT PRIMARY KEY,
  developer_id BIGINT NOT NULL REFERENCES tracker.developers(id),
  created_at TEXT NOT NULL
);
INSERT INTO tracker.tokens (hash, developer_id, created_at)
  SELECT token_hash, id, created_at FROM tracker.developers;

ALTER TABLE tracker.developers
  DROP COLUMN token_hash,
  ADD COLUMN github_login TEXT UNIQUE,
  -- The login and verified emails this person commits as, for matching them to git history.
  ADD COLUMN identity_keys TEXT[] NOT NULL DEFAULT '{}';
UPDATE tracker.developers SET identity_keys = ARRAY[email];

-- Git identity to display name, replaced on every git upload.
CREATE TABLE tracker.identities (
  key TEXT PRIMARY KEY,
  person TEXT NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.tokens, tracker.identities TO tracker_app;
