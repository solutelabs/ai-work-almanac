-- Which 5-minute blocks of the day had activity, as hex of a 288-bit number. Text rather than a count,
-- so the dashboard can combine parallel sessions without counting the same minutes twice.
-- NULL means a collector older than 0.7.0, which is not the same as no activity.
ALTER TABLE tracker.usage
  ADD COLUMN active_slots TEXT,
  ADD COLUMN prompted_slots TEXT,
  ADD COLUMN prompts BIGINT;
