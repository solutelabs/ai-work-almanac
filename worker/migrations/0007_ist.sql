-- Whether a row's day and blocks are in IST. Older collectors counted in UTC days. A log file's UTC
-- rows are replaced when its IST rows arrive, and this tells the two apart.
ALTER TABLE tracker.usage ADD COLUMN ist BOOLEAN NOT NULL DEFAULT FALSE;
