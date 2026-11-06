-- Whether a commit has reached its repo's default branch. Laptops also report commits from other branches,
-- which stay out of commit and line totals because a squash merge would otherwise count the same work twice.
ALTER TABLE tracker.commits ADD COLUMN on_default BOOLEAN NOT NULL DEFAULT TRUE;
