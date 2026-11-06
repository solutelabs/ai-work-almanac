-- Whether each laptop has the team harness set up, per repo its owner worked in. Replaced whole on each
-- change, and only as fresh as the laptop's last_seen in tracker.machines. No settings or hook contents.
CREATE TABLE tracker.harness (
  developer_id BIGINT NOT NULL REFERENCES tracker.developers(id),
  machine TEXT NOT NULL,
  repo TEXT NOT NULL,
  plugin BOOLEAN NOT NULL,        -- the Claude plugin is on for this repo
  plugin_version TEXT,
  hooks BOOLEAN NOT NULL,         -- git would run the harness secret scanner in this clone
  PRIMARY KEY (developer_id, machine, repo)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.harness TO tracker_app;
