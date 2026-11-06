-- Made-up people and repos for local testing, dated relative to today so they stay in the default period.
-- Together they reach each state the dashboard can show.
CREATE TEMP TABLE people (name TEXT PRIMARY KEY, machine TEXT, version TEXT, seen_hours INT, tool TEXT, repo TEXT, plugin BOOLEAN, hooks BOOLEAN);
INSERT INTO people VALUES
  ('Asha Rao',    'asha-mac',  '0.10.0', 1,   'claude-code', 'your-org/alpha-app',  TRUE,  TRUE),   -- green
  ('Ravi Patel',  'ravi-mac',  '0.10.0', 2,   'claude-code', 'your-org/alpha-app',  TRUE,  FALSE),  -- green, git hooks off
  ('Nina Shah',   'nina-mac',  '0.10.0', 1,   'claude-code', 'your-org/beta-web',   FALSE, FALSE),  -- red
  ('Meera Iyer',  'meera-mac', '0.10.0', 3,   'codex',       'your-org/beta-web',   NULL,  NULL),   -- grey, Codex only
  ('Dev Mehta',   'dev-mac',   '0.9.1',  4,   'claude-code', 'your-org/gamma-api',  NULL,  NULL),   -- grey, needs re-install
  ('Omar Khan',   'omar-mac',  '0.10.0', 500, 'claude-code', 'your-org/gamma-api',  TRUE,  TRUE);   -- grey, laptop not reporting

INSERT INTO tracker.developers (name, email, created_at, github_login, identity_keys)
  SELECT name, lower(split_part(name, ' ', 1)) || '@example.com', '2026-01-01T00:00:00Z', lower(split_part(name, ' ', 1)), ARRAY[lower(split_part(name, ' ', 1)) || '@example.com'] FROM people;

INSERT INTO tracker.machines (developer_id, machine, os, version, last_seen, files_parsed)
  SELECT d.id, p.machine, 'darwin', p.version, to_char(now() AT TIME ZONE 'UTC' - p.seen_hours * interval '1 hour', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), 10
  FROM people p JOIN tracker.developers d ON d.name = p.name;

INSERT INTO tracker.harness (developer_id, machine, repo, plugin, plugin_version, hooks)
  SELECT d.id, p.machine, p.repo, p.plugin, CASE WHEN p.plugin THEN '2.4.0' END, p.hooks
  FROM people p JOIN tracker.developers d ON d.name = p.name WHERE p.plugin IS NOT NULL;

CREATE TEMP TABLE work_days AS
  SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date - n AS day, n FROM generate_series(0, 27) n
  WHERE extract(isodow FROM (now() AT TIME ZONE 'Asia/Kolkata')::date - n) < 6;

-- Slots are bits, so 30 trailing zeros (120 bits) start at 10:00 and ffffff runs for two hours.
INSERT INTO tracker.usage (developer_id, machine, tool, source, session, day, model, repo, lines_added, lines_removed,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, active_slots, prompted_slots, prompts, folders, tool_version, ist)
  SELECT d.id, p.machine, p.tool, 'src' || w.n, 'sess-' || lower(split_part(p.name, ' ', 1)) || w.n, to_char(w.day, 'YYYY-MM-DD'),
    CASE p.tool WHEN 'codex' THEN 'gpt-5' ELSE 'claude-opus-5-5' END, p.repo,
    CASE WHEN p.tool = 'codex' THEN NULL ELSE 120 + w.n * 3 END, CASE WHEN p.tool = 'codex' THEN NULL ELSE 40 END,
    20000 + w.n * 500, 8000 + w.n * 100, 90000, 4000, 'ffffff' || repeat('0', 30), 'a1a1a1' || repeat('0', 30), 12 + w.n, NULL, '2.1.0', TRUE
  FROM people p JOIN tracker.developers d ON d.name = p.name CROSS JOIN work_days w;

-- Commits: on main, on develop, on a branch that hasn't reached a server, and laptop-reported.
INSERT INTO tracker.commits (repo, oid, person, authored_at, ai, lines_added, lines_removed, on_default, branch)
  SELECT p.repo, md5(p.name || w.n) || left(md5(p.name), 8), p.name, to_char(w.day, 'YYYY-MM-DD') || 'T06:00:00Z', w.n % 2 = 0, 50 + w.n, 10,
    CASE WHEN p.name = 'Asha Rao' THEN TRUE WHEN p.name = 'Ravi Patel' THEN w.n % 4 = 0 ELSE w.n % 2 = 0 END,
    CASE p.name WHEN 'Asha Rao' THEN 'main' WHEN 'Ravi Patel' THEN CASE WHEN w.n % 4 = 0 THEN 'develop' ELSE 'feat/stuck-work' END WHEN 'Nina Shah' THEN 'main' END
  FROM people p CROSS JOIN work_days w;

-- A merged PR with a review, a merged one without, and one open for over a week.
INSERT INTO tracker.pull_requests (repo, number, person, created_at, merged_at, human_review, any_review, agent_review, head_ref) VALUES
  ('your-org/alpha-app', 1, 'Asha Rao',   to_char(now() - interval '6 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(now() - interval '5 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), TRUE,  TRUE,  TRUE,  'feat/one'),
  ('your-org/beta-web',  2, 'Nina Shah',  to_char(now() - interval '4 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(now() - interval '3 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), FALSE, FALSE, FALSE, 'feat/two'),
  ('your-org/alpha-app', 3, 'Ravi Patel', to_char(now() - interval '12 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), NULL, FALSE, FALSE, NULL, 'feat/stuck-work');

-- Logged time for most days, so gaps against activity show up. Dev logs nothing at all.
INSERT INTO tracker.time_log (day, person, project, seconds, repos)
  SELECT to_char(w.day, 'YYYY-MM-DD'), p.name, split_part(p.repo, '/', 2), 6 * 3600, p.repo
  FROM people p CROSS JOIN work_days w WHERE p.name <> 'Dev Mehta' AND w.n % 5 <> 1;

INSERT INTO tracker.leave (day, person, portion, synced_at)
  SELECT to_char(day, 'YYYY-MM-DD'), 'Ravi Patel', 1, to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM work_days WHERE n BETWEEN 8 AND 9;
