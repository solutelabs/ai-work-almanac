-- Hours logged in solidtime per person, project and IST day, for people on the dashboard. No billable
-- rates, descriptions or billable flag. The last 35 days are replaced on each upload, because timesheets get edited.
CREATE TABLE tracker.time_log (
  day TEXT NOT NULL,
  person TEXT NOT NULL,
  project TEXT NOT NULL,
  seconds BIGINT NOT NULL,
  repos TEXT NOT NULL,  -- comma-separated, from solidtime_projects.json; empty when not mapped or not code work
  PRIMARY KEY (day, person, project)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.time_log TO tracker_app;
