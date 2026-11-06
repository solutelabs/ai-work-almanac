import postgres from "postgres";
import collectorScript from "../../collector/collect.py";
import { adminAuthorized, bearer, hex, safeEqual, sha256 } from "./auth.js";
import { handleHealth } from "./health.js";
import { accessEmail, validAccessToken } from "./access.js";
import page from "./dashboard.html";
import archivo from "./fonts/archivo.woff2";
import archivoNarrow from "./fonts/archivo-narrow.woff2";
import { DIGEST_CRON, buildDigest } from "./digest.js";
import { fairShare, readPrompts, readSecretContext, summarizeFindings } from "./findings.js";
import { EnrollError, githubIdentity, settleAll, startGitUpload } from "./github.js";
import { REPO_RENAMES, currentRepo } from "./repo_renames.js";
import { MAX_BODY_BYTES, cutoffDay, cutoffInstant, validateCapture, validateCommits, validateHarness, validateLaptopCommits, validateLeave, validateTimeLog, validateDeveloper, validateGit, validateIngest } from "./validate.js";

const FONTS = { "/fonts/archivo.woff2": archivo, "/fonts/archivo-narrow.woff2": archivoNarrow };

const json = (value, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function dashboardAuthorized(request, env) {
  // Once Google sign-in is switched on, the shared password stops working.
  if (env.ACCESS_AUD) return validAccessToken(request.headers.get("cf-access-jwt-assertion"), env.ACCESS_TEAM, env.ACCESS_AUD);
  const encoded = request.headers.get("authorization")?.match(/^Basic (.+)$/)?.[1];
  if (!encoded) return false;
  let decoded;
  try {
    decoded = atob(encoded);
  } catch {
    return false;
  }
  return safeEqual(decoded.slice(decoded.indexOf(":") + 1), env.DASHBOARD_PASSWORD);
}

// The collector's notice names who reads prompts, so this list must match it. Empty until Google sign-in is on.
function promptReader(request, env) {
  if (!env.ACCESS_AUD) return null;
  const email = accessEmail(request.headers.get("cf-access-jwt-assertion"));
  return email && (env.PROMPT_READERS ?? "").toLowerCase().split(",").includes(email) ? email : null;
}

const challenge = () => new Response("Sign in to view the tracker.", {
  status: 401,
  headers: { "www-authenticate": 'Basic realm="Pulse", charset="UTF-8"', "cache-control": "no-store" },
});

async function readJson(request) {
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) return { tooLarge: true };
  try {
    return { body: JSON.parse(text) };
  } catch {
    return { body: undefined };
  }
}

const allowedOrgs = (env) => env.ALLOWED_ORGS.split(",").map((org) => org.trim().toLowerCase());

// Numbers come back as Numbers: BIGINT and SUM() would otherwise arrive as strings.
const numeric = { to: 20, from: [20, 1700], parse: Number, serialize: String };

// Lazy: this opens no connection until the first query. Hyperdrive holds the real database
// connection and its TLS; DATABASE_URL exists for running this code outside Cloudflare.
const connect = (env) => postgres(env.HYPERDRIVE?.connectionString ?? env.DATABASE_URL, {
  max: 1, prepare: false, fetch_types: false, connect_timeout: 10, types: { bigint: numeric },
});

const USAGE_COLUMNS = ["developer_id", "machine", "tool", "source", "session", "day", "model", "repo", "lines_added",
  "lines_removed", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "active_slots", "prompted_slots", "prompts", "folders", "tool_version", "ist"];
const GIT_COLUMNS = ["month", "repo", "person", "commits", "claude_commits", "lines_added", "lines_removed", "claude_lines_added", "synced_at"];
const PR_COLUMNS = ["repo", "number", "person", "created_at", "merged_at", "human_review", "any_review", "agent_review", "head_ref"];
const TIME_COLUMNS = ["day", "person", "project", "seconds", "repos"];
const COMMIT_COLUMNS = ["repo", "oid", "person", "authored_at", "ai", "lines_added", "lines_removed", "on_default", "branch"];
const NO_REPO = "-/no-repo";
const GIT_CHUNK = 5000; // 9 columns each keeps a statement under Postgres's 65,535 parameter limit

async function developerFor(request, sql) {
  const token = bearer(request);
  if (!token) return null;
  const [developer] = await sql`SELECT d.id FROM tracker.tokens t JOIN tracker.developers d ON d.id = t.developer_id
    WHERE t.hash = ${hex(await sha256(token))} AND d.revoked_at IS NULL`;
  return developer ?? null;
}

// Org repo names, so a collector can identify sessions from worktrees that have since been deleted.
async function orgRepos(request, sql) {
  if (!(await developerFor(request, sql))) return json({ error: "invalid or revoked token" }, 401);
  // Only the org's repos: other people's personal repo names aren't needed for matching.
  const rows = await sql`SELECT DISTINCT repo FROM tracker.git_activity`;
  // Old names are included so a deleted folder can still match; the dashboard shows the current name.
  return json({ repos: [...rows.map((r) => r.repo), ...Object.keys(REPO_RENAMES)] });
}

async function ingest(request, sql) {
  const developer = await developerFor(request, sql);
  if (!developer) return json({ error: "invalid or revoked token" }, 401);

  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateIngest(body);
  if (data.error) return json({ error: data.error }, 400);

  const s = data.stats;
  const machine = {
    developer_id: developer.id, machine: data.machine, os: data.os, version: data.version, last_seen: new Date().toISOString(), ...s,
  };
  const rows = data.rows.map((r) => ({ ...r, developer_id: developer.id, machine: data.machine }));
  await sql.begin(async (tx) => {
    await tx`INSERT INTO tracker.machines ${tx(machine)}
      ON CONFLICT (developer_id, machine) DO UPDATE SET os = excluded.os, version = excluded.version, last_seen = excluded.last_seen,
        files_parsed = excluded.files_parsed, parse_errors = excluded.parse_errors, bad_lines = excluded.bad_lines,
        unrecognized_files = excluded.unrecognized_files, skipped_unresolved = excluded.skipped_unresolved,
        skipped_other_org = excluded.skipped_other_org`;
    if (rows.length) {
      // A re-read can now identify a repo that was "No repo" before. Rows are keyed by repo, so
      // without this the same work would count under both.
      const identified = rows.filter((r) => r.repo !== NO_REPO).map((r) => `${r.source}|${r.day}|${r.model}`);
      await tx`DELETE FROM tracker.usage WHERE developer_id = ${developer.id} AND machine = ${data.machine} AND repo = ${NO_REPO}
        AND source || '|' || day || '|' || model = ANY(${identified})`;
      // A file's rows sent in IST replace any it had in UTC. Those are keyed by a different day, so
      // they'd otherwise count twice. Only UTC rows go, so one file spread over several batches is safe.
      const ist = [...new Set(rows.filter((r) => r.ist).map((r) => r.source))];
      await tx`DELETE FROM tracker.usage WHERE developer_id = ${developer.id} AND machine = ${data.machine} AND NOT ist AND source = ANY(${ist})`;
      await tx`INSERT INTO tracker.usage ${tx(rows, USAGE_COLUMNS)}
        ON CONFLICT (developer_id, machine, source, day, model, repo) DO UPDATE SET tool = excluded.tool, session = excluded.session,
          lines_added = excluded.lines_added, lines_removed = excluded.lines_removed, input_tokens = excluded.input_tokens,
          output_tokens = excluded.output_tokens, cache_read_tokens = excluded.cache_read_tokens, cache_write_tokens = excluded.cache_write_tokens,
          active_slots = excluded.active_slots, prompted_slots = excluded.prompted_slots, prompts = excluded.prompts, folders = excluded.folders, tool_version = excluded.tool_version, ist = excluded.ist`;
    }
  });
  return json({ ok: true, rows: rows.length });
}

// Text goes to its own private bucket, never the database behind the dashboard. Keys hash the content,
// so a laptop re-sending a batch after a failure overwrites the same object.
async function capture(request, env, sql) {
  const developer = await developerFor(request, sql);
  if (!developer) return json({ error: "invalid or revoked token" }, 401);
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateCapture(body);
  if (data.error) return json({ error: data.error }, 400);

  const [{ name }] = await sql`SELECT name FROM tracker.developers WHERE id = ${developer.id}`;
  const lines = data.records.map((r) => JSON.stringify({ person: name, machine: data.machine, ...r })).join("\n") + "\n";
  const hash = hex(await sha256(lines));
  const day = data.records[0].at.slice(0, 10);
  await env.CAPTURE.put(`prompts/${day}/${developer.id}/${hash}.jsonl`, lines);
  const findings = data.records.flatMap((r) => r.findings.map((f) => ({ ...f, person: name, machine: data.machine, repo: r.repo, tool: r.tool, at: r.at })));
  if (findings.length) await env.CAPTURE.put(`findings/${day}/${developer.id}/${hash}.json`, JSON.stringify(findings));
  return json({ ok: true, records: data.records.length, findings: findings.length });
}

// Every read of captured text leaves a record, so access can be audited.
async function logAccess(env, what) {
  const at = new Date().toISOString();
  console.log("capture access", what);
  await env.CAPTURE.put(`access-log/${at.slice(0, 10)}/${at}-${crypto.randomUUID()}.json`, JSON.stringify({ at, what }));
}

async function listAll(env, prefix) {
  const keys = [];
  let cursor;
  do {
    const page = await env.CAPTURE.list({ prefix, cursor });
    keys.push(...page.objects.map((o) => o.key));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return keys;
}

// Each file is one read, and Workers cap the reads in a request, so a very large backlog shows as partial.
const MAX_FINDING_FILES = 500;
async function dashboardFindings(url, env) {
  const requested = Number.parseInt(url.searchParams.get("days") ?? "90", 10);
  const cutoff = cutoffDay(Number.isInteger(requested) ? requested : 90);
  // shortcut: files are named by their first record's day, so a batch starting just before the period is missed. Only affects its first day.
  const keys = (await listAll(env, "findings/")).filter((key) => key.split("/")[1] >= cutoff);
  const shown = fairShare(keys, MAX_FINDING_FILES);
  const found = await Promise.all(shown.map(async (key) => (await env.CAPTURE.get(key))?.json() ?? []));
  await logAccess(env, `dashboard findings ${requested}`);
  return json({ findings: summarizeFindings(found.flat(), cutoff).map((f) => ({ ...f, repo: currentRepo(f.repo) })), partial: keys.length > shown.length });
}

async function dashboardPrompts(url, env, sql, reader) {
  if (!reader) return json({ error: "not allowed" }, 403);
  const person = url.searchParams.get("person") ?? "", day = url.searchParams.get("day") ?? "";
  if (!person || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return json({ error: "person and day are required" }, 400);
  const developers = await sql`SELECT id FROM tracker.developers WHERE name = ${person}`;
  await logAccess(env, `prompts of ${person} on ${day} read by ${reader}`);
  return json(await readPrompts(env.CAPTURE, (prefix) => listAll(env, prefix), developers.map((d) => d.id), day));
}

async function dashboardSecretContext(url, env, sql, reader) {
  if (!reader) return json({ error: "not allowed" }, 403);
  const person = url.searchParams.get("person") ?? "", type = url.searchParams.get("type") ?? "";
  if (!person || !/^[a-z-]{1,40}$/.test(type)) return json({ error: "person and type are required" }, 400);
  const requested = Number.parseInt(url.searchParams.get("days") ?? "90", 10);
  const developers = await sql`SELECT id FROM tracker.developers WHERE name = ${person}`;
  await logAccess(env, `${type} findings of ${person} read by ${reader}`);
  const found = await readSecretContext(env.CAPTURE, (prefix) => listAll(env, prefix), developers.map((d) => d.id), type, cutoffDay(Number.isInteger(requested) ? requested : 90));
  return json({ ...found, rows: found.rows.map((r) => ({ ...r, repo: currentRepo(r.repo) })) });
}

async function sendDigest(env, sql) {
  if (!env.SLACK_DIGEST_WEBHOOK) throw new Error("SLACK_DIGEST_WEBHOOK isn't set");
  const weekStart = cutoffDay(7);
  const commits = await sql`SELECT DISTINCT ON (oid) repo, person, authored_at FROM tracker.commits WHERE authored_at >= ${cutoffInstant(weekStart)} ORDER BY oid, on_default DESC`;
  const usageDays = await sql`SELECT DISTINCT d.name AS person, u.day, u.repo FROM tracker.usage u JOIN tracker.developers d ON d.id = u.developer_id WHERE u.day >= ${weekStart}`;
  const loggedDays = await sql`SELECT DISTINCT person, day FROM tracker.time_log WHERE day >= ${weekStart}`;
  const leaveDays = await sql`SELECT person, day, portion FROM tracker.leave WHERE day >= ${weekStart}`;
  const machines = await sql`SELECT d.name AS developer, m.version, m.last_seen FROM tracker.machines m JOIN tracker.developers d ON d.id = m.developer_id WHERE d.revoked_at IS NULL`;
  const named = (rows) => rows.map((r) => ({ ...r, repo: currentRepo(r.repo) }));
  const text = buildDigest({ now: Date.now(), dashboardUrl: env.DASHBOARD_URL, weekStart, commits: named(commits), usageDays: named(usageDays), loggedDays, leaveDays, machines, leftOutRepos: [env.UPLOAD_REPO ?? ""] });
  const response = await fetch(env.SLACK_DIGEST_WEBHOOK, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) });
  if (!response.ok) throw new Error(`Slack answered ${response.status}`);
  await logAccess(env, "weekly digest sent");
  return text;
}

async function readCapture(route, url, env) {
  if (route === "GET /api/admin/capture/list") {
    const day = url.searchParams.get("day") ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return json({ error: "day must be YYYY-MM-DD" }, 400);
    await logAccess(env, `list ${day}`);
    return json({ keys: await listAll(env, `prompts/${day}/`) });
  }
  if (route === "GET /api/admin/capture/object") {
    const key = url.searchParams.get("key") ?? "";
    if (!key.startsWith("prompts/")) return json({ error: "key must start with prompts/" }, 400);
    const object = await env.CAPTURE.get(key);
    if (!object) return json({ error: "not found" }, 404);
    await logAccess(env, `read ${key}`);
    return new Response(object.body, { headers: { "content-type": "application/x-ndjson", "cache-control": "no-store" } });
  }
  if (route === "GET /api/admin/capture/findings") {
    const days = Number.parseInt(url.searchParams.get("days") ?? "7", 10);
    const today = Date.now(), found = [];
    for (let i = 0; i < Math.min(Number.isInteger(days) && days > 0 ? days : 7, 90); i++) {
      const day = new Date(today - i * 86400000).toISOString().slice(0, 10);
      for (const key of await listAll(env, `findings/${day}/`)) found.push(...await (await env.CAPTURE.get(key)).json());
    }
    await logAccess(env, `findings ${days}`);
    return json({ findings: found });
  }
  return json({ error: "not found" }, 404);
}

async function replaceGit(request, env, sql) {
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateGit(body, allowedOrgs(env));
  if (data.error) return json({ error: data.error }, 400);
  const syncedAt = new Date().toISOString();
  const rows = data.rows.map((r) => ({ ...r, synced_at: syncedAt }));
  // One transaction, so a failed insert leaves the previous snapshot in place.
  await sql.begin(async (tx) => {
    await tx`DELETE FROM tracker.git_activity`;
    for (let i = 0; i < rows.length; i += GIT_CHUNK) {
      await tx`INSERT INTO tracker.git_activity ${tx(rows.slice(i, i + GIT_CHUNK), GIT_COLUMNS)}`;
    }
    if (data.pullRequests) {
      await tx`DELETE FROM tracker.pull_requests`;
      for (let i = 0; i < data.pullRequests.length; i += GIT_CHUNK) {
        await tx`INSERT INTO tracker.pull_requests ${tx(data.pullRequests.slice(i, i + GIT_CHUNK), PR_COLUMNS)}`;
      }
    }
    await tx`DELETE FROM tracker.identities`;
    if (data.identities.length) {
      await tx`INSERT INTO tracker.identities ${tx(data.identities, ["key", "person"])}`;
      // Laptop data and commits share one row only when the names match.
      await tx`UPDATE tracker.developers d SET name = i.person FROM tracker.identities i WHERE i.key = ANY(d.identity_keys)`;
    }
  });
  return json({ ok: true, rows: rows.length });
}

// Added to rather than replaced: GitHub only gives line counts for a recent window, so older commits
// aren't re-sent. shortcut: a commit dropped by a force-push stays counted; rebuild the table if that matters.
async function saveCommits(request, env, sql) {
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateCommits(body, allowedOrgs(env));
  if (data.error) return json({ error: data.error }, 400);
  if (data.commits.length) {
    await sql`INSERT INTO tracker.commits ${sql(data.commits, COMMIT_COLUMNS)}
      ON CONFLICT (repo, oid) DO UPDATE SET person = excluded.person, authored_at = excluded.authored_at, ai = excluded.ai,
        lines_added = excluded.lines_added, lines_removed = excluded.lines_removed, on_default = tracker.commits.on_default OR excluded.on_default, branch = COALESCE(excluded.branch, tracker.commits.branch)`;
  }
  return json({ ok: true, commits: data.commits.length });
}

// Replaced whole for each laptop, so a repo that's gone from it stops showing.
async function laptopHarness(request, sql) {
  const developer = await developerFor(request, sql);
  if (!developer) return json({ error: "invalid or revoked token" }, 401);
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateHarness(body);
  if (data.error) return json({ error: data.error }, 400);
  // Old and new names of a renamed repo land on one row, and a yes from either counts.
  const rows = new Map();
  for (const r of data.repos) {
    const repo = currentRepo(r.repo);
    const had = rows.get(repo);
    rows.set(repo, { developer_id: developer.id, machine: data.machine, repo, plugin: r.plugin || !!had?.plugin, plugin_version: r.plugin_version ?? had?.plugin_version ?? null, hooks: r.hooks || !!had?.hooks });
  }
  await sql.begin(async (tx) => {
    await tx`DELETE FROM tracker.harness WHERE developer_id = ${developer.id} AND machine = ${data.machine}`;
    if (rows.size) await tx`INSERT INTO tracker.harness ${tx([...rows.values()])}`;
  });
  return json({ ok: true, repos: rows.size });
}

// A laptop can add a commit or say it has reached the default branch, but can't change who made an
// existing one, so nobody can take credit for a colleague's work.
async function laptopCommits(request, sql) {
  const developer = await developerFor(request, sql);
  if (!developer) return json({ error: "invalid or revoked token" }, 401);
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateLaptopCommits(body);
  if (data.error) return json({ error: data.error }, 400);
  const [{ name }] = await sql`SELECT name FROM tracker.developers WHERE id = ${developer.id}`;
  // Keyed by the current repo name, so a renamed repo's commits don't count under both.
  const rows = new Map(data.commits.map((c) => [`${currentRepo(c.repo)}|${c.oid}`, { ...c, repo: currentRepo(c.repo), person: name }]));
  await sql`INSERT INTO tracker.commits ${sql([...rows.values()], COMMIT_COLUMNS)}
    ON CONFLICT (repo, oid) DO UPDATE SET on_default = tracker.commits.on_default OR excluded.on_default`;
  return json({ ok: true, commits: rows.size });
}

// Replaced from a date onward, so edited and deleted timesheet entries don't linger. One transaction,
// so a failed insert leaves the previous log in place.
async function replaceTimeLog(request, sql) {
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateTimeLog(body);
  if (data.error) return json({ error: data.error }, 400);
  await sql.begin(async (tx) => {
    await tx`DELETE FROM tracker.time_log WHERE day >= ${data.from}`;
    if (data.rows.length) await tx`INSERT INTO tracker.time_log ${tx(data.rows, TIME_COLUMNS)}`;
  });
  return json({ ok: true, rows: data.rows.length });
}

// Replaces a date range, so a cancelled leave disappears. Someone not on the dashboard is left out,
// and named back to the sender so a wrong email can be spotted.
async function replaceLeave(request, sql) {
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  const data = validateLeave(body);
  if (data.error) return json({ error: data.error }, 400);
  const syncedAt = new Date().toISOString();
  const skipped = await sql.begin(async (tx) => {
    const known = await tx`SELECT key, person FROM tracker.identities UNION SELECT unnest(identity_keys), name FROM tracker.developers WHERE revoked_at IS NULL`;
    const people = new Map(known.map((k) => [k.key.toLowerCase(), k.person]));
    const rows = new Map();
    for (const d of data.days) {
      const person = people.get(d.email);
      if (person) rows.set(`${person}|${d.day}`, { day: d.day, person, portion: d.portion, synced_at: syncedAt });
    }
    await tx`DELETE FROM tracker.leave WHERE day >= ${data.from} AND day <= ${data.to}`;
    if (rows.size) await tx`INSERT INTO tracker.leave ${tx([...rows.values()], ["day", "person", "portion", "synced_at"])}`;
    return [...new Set(data.days.filter((d) => !people.has(d.email)).map((d) => d.email))];
  });
  return json({ ok: true, days: data.days.length - data.days.filter((d) => skipped.includes(d.email)).length, skipped });
}

async function issueToken(tx, developerId) {
  const token = "slt_" + hex(crypto.getRandomValues(new Uint8Array(32)));
  await tx`INSERT INTO tracker.tokens (hash, developer_id, created_at) VALUES (${hex(await sha256(token))}, ${developerId}, ${new Date().toISOString()})`;
  return token;
}

async function createDeveloper(request, sql) {
  const { body } = await readJson(request);
  const data = validateDeveloper(body);
  if (data.error) return json({ error: data.error }, 400);
  // Re-registering an email replaces the person's tokens and clears a revocation.
  const token = await sql.begin(async (tx) => {
    const [developer] = await tx`INSERT INTO tracker.developers (name, email, created_at, identity_keys)
      VALUES (${data.name}, ${data.email}, ${new Date().toISOString()}, ${[data.email]})
      ON CONFLICT (email) DO UPDATE SET name = excluded.name, revoked_at = NULL RETURNING id`;
    await tx`DELETE FROM tracker.tokens WHERE developer_id = ${developer.id}`;
    return issueToken(tx, developer.id);
  });
  return json({ name: data.name, email: data.email, token });
}

// The GitHub sign-in is used once here and never stored. Enrolling adds a token rather than
// replacing one, so setting up a second laptop doesn't break the first.
async function enroll(request, env, sql) {
  const { body, tooLarge } = await readJson(request);
  if (tooLarge) return json({ error: "body too large" }, 413);
  let id;
  try {
    id = await githubIdentity(body?.github_token, allowedOrgs(env));
  } catch (error) {
    if (error instanceof EnrollError) return json({ error: error.message }, error.status);
    throw error;
  }
  const result = await sql.begin(async (tx) => {
    const [known] = await tx`SELECT person FROM tracker.identities WHERE key = ANY(${id.identityKeys}) LIMIT 1`;
    const name = known?.person ?? id.name;
    const [existing] = await tx`SELECT id, revoked_at, identity_keys FROM tracker.developers
      WHERE github_login = ${id.login} OR email = ANY(${id.emails}) ORDER BY github_login = ${id.login} DESC NULLS LAST LIMIT 1`;
    if (existing?.revoked_at) return { revoked: true };
    let developerId = existing?.id;
    if (existing) {
      const keys = [...new Set([...existing.identity_keys, ...id.identityKeys])];
      await tx`UPDATE tracker.developers SET name = ${name}, github_login = ${id.login}, identity_keys = ${keys} WHERE id = ${developerId}`;
    } else {
      [{ id: developerId }] = await tx`INSERT INTO tracker.developers (name, email, created_at, github_login, identity_keys)
        VALUES (${name}, ${id.email}, ${new Date().toISOString()}, ${id.login}, ${id.identityKeys}) RETURNING id`;
    }
    return { name, token: await issueToken(tx, developerId) };
  });
  if (result.revoked) return json({ error: "Your access was revoked. Ask whoever runs the tracker." }, 403);
  return json(result);
}

async function revokeDeveloper(request, sql) {
  const { body } = await readJson(request);
  const email = typeof body?.email === "string" ? body.email.trim().toLowerCase() : "";
  const result = await sql`UPDATE tracker.developers SET revoked_at = ${new Date().toISOString()} WHERE email = ${email}`;
  return result.count ? json({ ok: true }) : json({ error: "no developer with that email" }, 404);
}

async function dashboardData(url, sql, canReadPrompts, orgs) {
  const requested = Number.parseInt(url.searchParams.get("days") ?? "90", 10);
  const cutoff = cutoffDay(Number.isInteger(requested) ? requested : 90);
  const usage = await sql`SELECT u.day, d.name AS developer, u.tool, u.repo, u.model, u.session, SUM(u.lines_added) AS added,
      SUM(u.lines_removed) AS removed, SUM(u.input_tokens) AS input, SUM(u.output_tokens) AS output,
      SUM(u.cache_read_tokens) AS cache_read, SUM(u.cache_write_tokens) AS cache_write,
      STRING_AGG(u.active_slots, ',') AS active, STRING_AGG(u.prompted_slots, ',') AS prompted, SUM(u.prompts) AS prompts, STRING_AGG(u.folders, ',') AS folders, MAX(u.tool_version) AS tool_version, BOOL_OR(u.ist) AS ist
    FROM tracker.usage u JOIN tracker.developers d ON d.id = u.developer_id WHERE u.day >= ${cutoff}
    GROUP BY u.day, d.name, u.tool, u.repo, u.model, u.session`;
  const git = await sql`SELECT * FROM tracker.git_activity WHERE month >= ${cutoff.slice(0, 7)}`;
  // Open PRs count whatever the period: being stuck doesn't age out.
  const pullRequests = await sql`SELECT repo, number, person, created_at, merged_at, human_review, any_review, agent_review, head_ref FROM tracker.pull_requests
    WHERE merged_at IS NULL OR merged_at >= ${cutoffInstant(cutoff)}`;
  // One row per commit, because a repo mirrored to a second host has the same commit under both names.
  const commits = await sql`SELECT DISTINCT ON (oid) repo, person, authored_at, ai, lines_added, lines_removed, on_default, branch FROM tracker.commits
    WHERE authored_at >= ${cutoffInstant(cutoff)} ORDER BY oid, on_default DESC, (split_part(repo, '/', 1) IN ${sql(orgs)}) DESC, repo`;
  const timeLog = await sql`SELECT day, person, project, seconds, repos FROM tracker.time_log WHERE day >= ${cutoff}`;
  const leave = await sql`SELECT day, person, portion, synced_at FROM tracker.leave WHERE day >= ${cutoff}`;
  const machines = await sql`SELECT d.name AS developer, m.machine, m.os, m.version, m.last_seen, m.parse_errors, m.bad_lines,
      m.unrecognized_files, m.skipped_unresolved,
      (SELECT COUNT(*) FROM tracker.usage u WHERE u.developer_id = m.developer_id AND u.machine = m.machine) AS usage_rows,
      (SELECT MIN(u.day) FROM tracker.usage u WHERE u.developer_id = m.developer_id AND u.machine = m.machine AND u.ist AND u.active_slots IS NOT NULL) AS first_ist_day FROM tracker.machines m JOIN tracker.developers d ON d.id = m.developer_id WHERE d.revoked_at IS NULL`;
  const harness = await sql`SELECT d.name AS developer, h.machine, h.repo, h.plugin, h.plugin_version, h.hooks FROM tracker.harness h
    JOIN tracker.developers d ON d.id = h.developer_id WHERE d.revoked_at IS NULL`;
  return json({ canReadPrompts, cutoff, orgs, usage: usage.map((u) => ({ ...u, repo: currentRepo(u.repo) })), git, commits: commits.map((c) => ({ ...c, repo: currentRepo(c.repo) })), timeLog, leave, pullRequests, machines, harness });
}

export default {
  // GitHub's own schedule never fired for this repo, so Cloudflare starts the upload instead.
  async scheduled(controller, env, ctx) {
    if (controller.cron !== DIGEST_CRON) return startGitUpload(env.GIT_UPLOAD_TOKEN, env.UPLOAD_REPO);
    // The digest and the status post are independent: one failing must not stop the other.
    await settleAll([
      startGitUpload(env.GIT_UPLOAD_TOKEN, env.UPLOAD_REPO, fetch, "status.yml"),
      (async () => {
        const sql = connect(env);
        try {
          await sendDigest(env, sql);
        } finally {
          ctx.waitUntil(sql.end());
        }
      })(),
    ]);
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /collect.py") {
      return new Response(collectorScript, { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    if (request.method === "GET" && FONTS[url.pathname]) {
      return new Response(FONTS[url.pathname], { headers: { "content-type": "font/woff2", "cache-control": "public, max-age=31536000, immutable" } });
    }
    // Laptops share this hostname, so Access guards only /dashboard and /api/dashboard, and / can't hold the page.
    if (route === "GET /") return Response.redirect(new URL("/dashboard", url), 302);
    // Public by design: a client ID identifies the app and grants nothing.
    if (route === "GET /api/enroll/config") {
      return env.GITHUB_CLIENT_ID ? json({ client_id: env.GITHUB_CLIENT_ID }) : json({ error: "GitHub sign-in isn't set up yet" }, 503);
    }
    const sql = connect(env);
    try {
      if (route === "POST /api/enroll") return await enroll(request, env, sql);
      if (route === "POST /api/ingest") return await ingest(request, sql);
      if (route === "GET /api/repos") return await orgRepos(request, sql);
      if (route === "POST /api/capture") return await capture(request, env, sql);
      if (route === "POST /api/commits") return await laptopCommits(request, sql);
      if (route === "POST /api/harness") return await laptopHarness(request, sql);
      if (url.pathname.startsWith("/api/admin/")) {
        // No configured secret means no access, never open access.
        if (!(await adminAuthorized(request, env))) return json({ error: "unauthorized" }, 401);
        if (url.pathname.startsWith("/api/admin/capture/")) return await readCapture(route, url, env);
        if (route === "GET /api/admin/health") return await handleHealth(sql);
        if (route === "POST /api/admin/digest") {
          try {
            return json({ ok: true, text: await sendDigest(env, sql) });
          } catch (error) {
            console.error(route, error);
            // Only someone with the admin token sees this. Links are removed so the Slack webhook can't appear in it.
            return json({ error: "digest failed", detail: String(error?.message ?? error).replace(/https?:\S+/g, "[link]").slice(0, 300) }, 500);
          }
        }
        if (route === "POST /api/admin/developers") return await createDeveloper(request, sql);
        if (route === "POST /api/admin/developers/revoke") return await revokeDeveloper(request, sql);
        if (route === "POST /api/admin/git") return await replaceGit(request, env, sql);
        if (route === "POST /api/admin/leave") return await replaceLeave(request, sql);
        if (route === "POST /api/admin/time") return await replaceTimeLog(request, sql);
        if (route === "POST /api/admin/commits") return await saveCommits(request, env, sql);
        return json({ error: "not found" }, 404);
      }
      if (route === "GET /dashboard" || route === "GET /api/dashboard") {
        if (!(await dashboardAuthorized(request, env))) {
          return env.ACCESS_AUD ? new Response(`Sign in with Google at ${env.DASHBOARD_URL} to view the tracker.`, { status: 403, headers: { "cache-control": "no-store" } }) : challenge();
        }
        // On the same path, so the Access rule that guards the dashboard's data guards this too.
        if (route === "GET /api/dashboard" && url.searchParams.get("part") === "findings") return await dashboardFindings(url, env);
        if (route === "GET /api/dashboard" && url.searchParams.get("part") === "secret-context") return await dashboardSecretContext(url, env, sql, promptReader(request, env));
        if (route === "GET /api/dashboard" && url.searchParams.get("part") === "prompts") return await dashboardPrompts(url, env, sql, promptReader(request, env));
        if (route === "GET /api/dashboard") return await dashboardData(url, sql, Boolean(promptReader(request, env)), allowedOrgs(env));
        return new Response(page, {
          headers: {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src 'self'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
          },
        });
      }
      return new Response("Not found", { status: 404 });
    } catch (error) {
      console.error(route, error);
      return json({ error: "server error" }, 500);
    } finally {
      ctx.waitUntil(sql.end());
    }
  },
};
