-- Which branch a commit was found on, so work that hasn't reached develop or main can be seen. NULL means a
-- laptop reported it, which can't say. A PR's branch name links open PRs to that work.
ALTER TABLE tracker.commits ADD COLUMN branch TEXT;
ALTER TABLE tracker.pull_requests ADD COLUMN head_ref TEXT;
