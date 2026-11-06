-- Days people on the dashboard were on leave, sent from your HR system. Who and when only,
-- never the type or reason. portion is 1 for a full day and 0.5 for a half day.
CREATE TABLE tracker.leave (
  day TEXT NOT NULL,
  person TEXT NOT NULL,
  portion REAL NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (day, person)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON tracker.leave TO tracker_app;
