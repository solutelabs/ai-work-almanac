-- NULL means not recorded yet (the evidence reader fills it later).
ALTER TABLE tracker.pull_requests ADD COLUMN agent_review BOOLEAN;
