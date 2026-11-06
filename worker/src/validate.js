// Everything a laptop or the git tracker sends is untrusted, so it is checked field by field and
// rebuilt: unknown fields are dropped rather than stored.
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const MONTH = /^\d{4}-\d{2}$/;
const REPO = /^[\w.-]+\/[\w.-]+$/;
const TOOL = /^[a-z0-9_-]{1,32}$/;
const ID = /^[\w.-]{1,64}$/;
const MAX_COUNT = 1e13;
const SLOTS = /^[0-9a-f]{1,72}$/; // 288 bits: the 5-minute blocks of a day
const TOOL_VERSION = /^[\w.+-]{1,32}$/;
const FOLDERS = /^[wm][0-9a-f]{16}(,[wm][0-9a-f]{16}){0,49}$/; // hashes only, so a path can't get stored
export const MAX_ROWS = 1000;
export const MAX_BODY_BYTES = 2_000_000;
const STAT_FIELDS = ["files_parsed", "parse_errors", "bad_lines", "unrecognized_files", "skipped_unresolved", "skipped_other_org"];
const TOKEN_FIELDS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"];

const isCount = (v) => Number.isSafeInteger(v) && v >= 0 && v <= MAX_COUNT;
const isText = (v, max) => typeof v === "string" && v.length > 0 && v.length <= max && !/[\x00-\x1f\x7f]/.test(v);
const orgOf = (repo) => repo.split("/")[0].toLowerCase();

export function validateIngest(body) {
  if (typeof body !== "object" || body === null) return { error: "body must be a JSON object" };
  const { machine, version, os, stats, rows } = body;
  if (!isText(machine, 100)) return { error: "invalid machine" };
  if (!isText(version, 32)) return { error: "invalid version" };
  if (!isText(os, 32)) return { error: "invalid os" };
  if (!Array.isArray(rows) || rows.length > MAX_ROWS) return { error: `rows must be an array of at most ${MAX_ROWS}` };
  const cleanStats = {};
  for (const field of STAT_FIELDS) {
    if (!isCount(stats?.[field])) return { error: `invalid stats.${field}` };
    cleanStats[field] = stats[field];
  }
  const clean = [];
  for (const [i, row] of rows.entries()) {
    const fail = (field) => ({ error: `row ${i}: invalid ${field}` });
    if (typeof row !== "object" || row === null) return fail("row");
    if (!TOOL.test(row.tool ?? "")) return fail("tool");
    if (!ID.test(row.source ?? "")) return fail("source");
    if (!ID.test(row.session ?? "")) return fail("session");
    if (!DAY.test(row.day ?? "")) return fail("day");
    if (!isText(row.model, 100)) return fail("model");
    if (!REPO.test(row.repo ?? "")) return fail("repo");
    for (const field of ["lines_added", "lines_removed"]) {
      if (row[field] !== null && !isCount(row[field])) return fail(field);
    }
    for (const field of TOKEN_FIELDS) if (!isCount(row[field])) return fail(field);
    // Absent from collectors older than 0.7.0, which keep working.
    for (const field of ["active_slots", "prompted_slots"]) {
      if (row[field] !== undefined && row[field] !== null && (typeof row[field] !== "string" || !SLOTS.test(row[field]))) return fail(field);
    }
    if (row.prompts !== undefined && row.prompts !== null && !isCount(row.prompts)) return fail("prompts");
    if (row.folders !== undefined && row.folders !== null && (typeof row.folders !== "string" || !FOLDERS.test(row.folders))) return fail("folders");
    if (row.tool_version !== undefined && row.tool_version !== null && (typeof row.tool_version !== "string" || !TOOL_VERSION.test(row.tool_version))) return fail("tool_version");
    clean.push({
      tool: row.tool, source: row.source, session: row.session, day: row.day, model: row.model, repo: row.repo.toLowerCase(),
      lines_added: row.lines_added, lines_removed: row.lines_removed,
      input_tokens: row.input_tokens, output_tokens: row.output_tokens,
      cache_read_tokens: row.cache_read_tokens, cache_write_tokens: row.cache_write_tokens,
      active_slots: row.active_slots ?? null, prompted_slots: row.prompted_slots ?? null, prompts: row.prompts ?? null, folders: row.folders ?? null, tool_version: row.tool_version ?? null, ist: row.ist === true,
    });
  }
  return { machine, version, os, stats: cleanStats, rows: clean };
}

const GIT_COUNTS = ["commits", "claude_commits", "lines_added", "lines_removed", "claude_lines_added"];

export function validateGit(body, allowedOrgs) {
  const rows = body?.rows;
  if (!Array.isArray(rows) || rows.length === 0 || rows.length > 20000) return { error: "rows must be a non-empty array of at most 20000" };
  const clean = [];
  for (const [i, row] of rows.entries()) {
    const fail = (field) => ({ error: `row ${i}: invalid ${field}` });
    if (typeof row !== "object" || row === null) return fail("row");
    if (!MONTH.test(row.month ?? "")) return fail("month");
    if (!REPO.test(row.repo ?? "") || !allowedOrgs.includes(orgOf(row.repo))) return fail("repo");
    if (!isText(row.person, 200)) return fail("person");
    for (const field of GIT_COUNTS) if (!isCount(row[field])) return fail(field);
    clean.push({
      month: row.month, repo: row.repo.toLowerCase(), person: row.person, commits: row.commits, claude_commits: row.claude_commits,
      lines_added: row.lines_added, lines_removed: row.lines_removed, claude_lines_added: row.claude_lines_added,
    });
  }
  const identities = body.identities ?? [];
  if (!Array.isArray(identities) || identities.length > 20000) return { error: "identities must be an array of at most 20000" };
  for (const [i, identity] of identities.entries()) {
    if (!isText(identity?.key, 200) || !isText(identity?.person, 200)) return { error: `identity ${i}: invalid` };
  }
  const pullRequests = body.pull_requests;
  if (pullRequests !== undefined && (!Array.isArray(pullRequests) || pullRequests.length > 100000)) return { error: "pull_requests must be an array of at most 100000" };
  const cleanPrs = [];
  for (const [i, pr] of (pullRequests ?? []).entries()) {
    const fail = (field) => ({ error: `pull request ${i}: invalid ${field}` });
    if (typeof pr !== "object" || pr === null) return fail("row");
    if (!REPO.test(pr.repo ?? "") || !allowedOrgs.includes(orgOf(pr.repo))) return fail("repo");
    if (!isCount(pr.number)) return fail("number");
    if (!isText(pr.person, 200)) return fail("person");
    if (!isText(pr.created_at, 40)) return fail("created_at");
    if (pr.merged_at !== null && !isText(pr.merged_at, 40)) return fail("merged_at");
    if (pr.head_ref !== undefined && pr.head_ref !== null && !isText(pr.head_ref, 255)) return fail("head_ref");
    if (typeof pr.human_review !== "boolean") return fail("human_review");
    if (typeof pr.any_review !== "boolean") return fail("any_review");
    if (pr.agent_review !== undefined && pr.agent_review !== null && typeof pr.agent_review !== "boolean") return fail("agent_review");
    cleanPrs.push({ repo: pr.repo.toLowerCase(), number: pr.number, person: pr.person, created_at: pr.created_at, merged_at: pr.merged_at, human_review: pr.human_review, any_review: pr.any_review, agent_review: pr.agent_review ?? null, head_ref: pr.head_ref ?? null });
  }
  return {
    rows: clean,
    identities: identities.map((i) => ({ key: i.key.toLowerCase(), person: i.person })),
    pullRequests: pullRequests === undefined ? null : cleanPrs,
  };
}

export function validateTimeLog(body) {
  if (!DAY.test(body?.from ?? "")) return { error: "invalid from" };
  if (!Array.isArray(body.rows) || body.rows.length > 20000) return { error: "rows must be an array of at most 20000" };
  const clean = [];
  for (const [i, r] of body.rows.entries()) {
    const fail = (field) => ({ error: `row ${i}: invalid ${field}` });
    if (typeof r !== "object" || r === null) return fail("row");
    if (!DAY.test(r.day ?? "") || r.day < body.from) return fail("day");
    if (!isText(r.person, 200)) return fail("person");
    if (!isText(r.project, 200)) return fail("project");
    if (!isCount(r.seconds)) return fail("seconds");
    if (!Array.isArray(r.repos) || r.repos.length > 50 || !r.repos.every((repo) => REPO.test(repo))) return fail("repos");
    clean.push({ day: r.day, person: r.person, project: r.project, seconds: r.seconds, repos: r.repos.join(",").toLowerCase() });
  }
  return { from: body.from, rows: clean };
}

// Only who and when: a leave type or reason is dropped here, however it's sent.
export function validateLeave(body) {
  if (!DAY.test(body?.from ?? "") || !DAY.test(body?.to ?? "") || body.from > body.to) return { error: "invalid from or to" };
  if (!Array.isArray(body.days) || body.days.length > 20000) return { error: "days must be an array of at most 20000" };
  const clean = new Map();
  for (const [i, d] of body.days.entries()) {
    const fail = (field) => ({ error: `day ${i}: invalid ${field}` });
    if (typeof d !== "object" || d === null) return fail("day");
    const email = typeof d.email === "string" ? d.email.trim().toLowerCase() : "";
    if (!isText(email, 200) || !/^[^@\s]+@[^@\s]+$/.test(email)) return fail("email");
    if (!DAY.test(d.day ?? "") || d.day < body.from || d.day > body.to) return fail("day");
    if (d.portion !== undefined && d.portion !== 0.5 && d.portion !== 1) return fail("portion");
    const portion = d.portion ?? 1;
    const key = `${email}|${d.day}`;
    clean.set(key, { email, day: d.day, portion: Math.min(1, (clean.get(key)?.portion ?? 0) + portion) }); // two half days make one full day
  }
  return { from: body.from, to: body.to, days: [...clean.values()] };
}

const OID = /^[0-9a-f]{40}$/;
const UTC_STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

// A laptop's own commits, from any org or git host. Who they belong to comes from its token, never the body.
export function validateLaptopCommits(body) {
  const commits = body?.commits;
  if (!Array.isArray(commits) || commits.length === 0 || commits.length > MAX_ROWS) return { error: `commits must be a non-empty array of at most ${MAX_ROWS}` };
  const clean = [];
  for (const [i, c] of commits.entries()) {
    const fail = (field) => ({ error: `commit ${i}: invalid ${field}` });
    if (typeof c !== "object" || c === null) return fail("commit");
    if (!REPO.test(c.repo ?? "")) return fail("repo");
    if (!OID.test(c.oid ?? "")) return fail("oid");
    if (!UTC_STAMP.test(c.authored_at ?? "")) return fail("authored_at");
    if (typeof c.ai !== "boolean") return fail("ai");
    if (typeof c.on_default !== "boolean") return fail("on_default");
    if (!isCount(c.lines_added)) return fail("lines_added");
    if (!isCount(c.lines_removed)) return fail("lines_removed");
    clean.push({ repo: c.repo.toLowerCase(), oid: c.oid, authored_at: c.authored_at, ai: c.ai, on_default: c.on_default, lines_added: c.lines_added, lines_removed: c.lines_removed, branch: null });
  }
  return { commits: clean };
}

// What a laptop says about the harness in the repos its owner worked in. Who it belongs to comes from its token.
export function validateHarness(body) {
  if (!isText(body?.machine, 100)) return { error: "invalid machine" };
  if (!Array.isArray(body.repos) || body.repos.length > MAX_ROWS) return { error: `repos must be an array of at most ${MAX_ROWS}` };
  const clean = [];
  for (const [i, r] of body.repos.entries()) {
    const fail = (field) => ({ error: `repo ${i}: invalid ${field}` });
    if (typeof r !== "object" || r === null) return fail("repo");
    if (!REPO.test(r.repo ?? "")) return fail("repo");
    if (typeof r.plugin !== "boolean") return fail("plugin");
    if (typeof r.hooks !== "boolean") return fail("hooks");
    if (r.plugin_version !== null && (typeof r.plugin_version !== "string" || !TOOL_VERSION.test(r.plugin_version))) return fail("plugin_version");
    clean.push({ repo: r.repo.toLowerCase(), plugin: r.plugin, plugin_version: r.plugin_version, hooks: r.hooks });
  }
  return { machine: body.machine, repos: clean };
}

export function validateCommits(body, allowedOrgs) {
  const commits = body?.commits;
  if (!Array.isArray(commits) || commits.length > 5000) return { error: "commits must be an array of at most 5000" };
  const clean = [];
  for (const [i, c] of commits.entries()) {
    const fail = (field) => ({ error: `commit ${i}: invalid ${field}` });
    if (typeof c !== "object" || c === null) return fail("commit");
    if (!REPO.test(c.repo ?? "") || !allowedOrgs.includes(orgOf(c.repo))) return fail("repo");
    if (!OID.test(c.oid ?? "")) return fail("oid");
    if (!isText(c.person, 200)) return fail("person");
    if (!UTC_STAMP.test(c.authored_at ?? "")) return fail("authored_at");
    if (typeof c.ai !== "boolean") return fail("ai");
    if (!isCount(c.lines_added)) return fail("lines_added");
    if (!isCount(c.lines_removed)) return fail("lines_removed");
    if (c.on_default !== undefined && typeof c.on_default !== "boolean") return fail("on_default");
    if (c.branch !== undefined && c.branch !== null && !isText(c.branch, 255)) return fail("branch");
    clean.push({ repo: c.repo.toLowerCase(), oid: c.oid, person: c.person, authored_at: c.authored_at, ai: c.ai, lines_added: c.lines_added, lines_removed: c.lines_removed, on_default: c.on_default ?? true, branch: c.branch ?? null });
  }
  return { commits: clean };
}

const ROLES = ["prompt", "response", "tool"];
const MAX_CAPTURE_TEXT = 120000;
const FINDING_TYPE = /^[a-z-]{1,40}$/;
const FINGERPRINT = /^[0-9a-f]{8,64}$/;

export function validateCapture(body) {
  if (typeof body !== "object" || body === null) return { error: "body must be a JSON object" };
  if (!isText(body.machine, 100)) return { error: "invalid machine" };
  if (!Array.isArray(body.records) || body.records.length === 0 || body.records.length > MAX_ROWS) return { error: `records must be a non-empty array of at most ${MAX_ROWS}` };
  const clean = [];
  for (const [i, r] of body.records.entries()) {
    const fail = (field) => ({ error: `record ${i}: invalid ${field}` });
    if (typeof r !== "object" || r === null) return fail("record");
    if (!TOOL.test(r.tool ?? "")) return fail("tool");
    if (!ID.test(r.session ?? "")) return fail("session");
    if (!isText(r.at, 40) || !DAY.test(r.at.slice(0, 10))) return fail("at");
    if (!ROLES.includes(r.role)) return fail("role");
    if (!REPO.test(r.repo ?? "")) return fail("repo");
    if (typeof r.text !== "string" || !r.text || r.text.length > MAX_CAPTURE_TEXT) return fail("text");
    if (!Array.isArray(r.findings) || r.findings.length > 500) return fail("findings");
    for (const f of r.findings) {
      if (!FINDING_TYPE.test(f?.type ?? "") || !FINGERPRINT.test(f?.fingerprint ?? "")) return fail("findings");
    }
    clean.push({
      tool: r.tool, session: r.session, at: r.at, role: r.role, agent: r.agent === true, repo: r.repo.toLowerCase(), text: r.text, truncated: r.truncated === true,
      findings: r.findings.map((f) => ({ type: f.type, fingerprint: f.fingerprint })),
    });
  }
  return { machine: body.machine, records: clean };
}

export function validateDeveloper(body) {
  const name = typeof body?.name === "string" ? body.name.trim() : "";
  const email = typeof body?.email === "string" ? body.email.trim().toLowerCase() : "";
  if (!isText(name, 200)) return { error: "invalid name" };
  if (!isText(email, 200) || !/^[^@\s]+@[^@\s]+$/.test(email)) return { error: "invalid email" };
  return { name, email };
}

const IST_MS = 5.5 * 3600000;

// Days on the dashboard are IST.
export function cutoffDay(days, now = new Date()) {
  if (!Number.isInteger(days) || days <= 0) return "0000-00-00";
  return new Date(now.getTime() + IST_MS - (days - 1) * 86400000).toISOString().slice(0, 10);
}

// The moment an IST day starts, in the form PR times are stored: GitHub's UTC timestamps.
export function cutoffInstant(day) {
  return day === "0000-00-00" ? day : new Date(`${day}T00:00:00+05:30`).toISOString().slice(0, 19) + "Z";
}
