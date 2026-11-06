import assert from "node:assert/strict";
import test from "node:test";
import { REPO_RENAMES, currentRepo } from "../src/repo_renames.js";
import { cutoffDay, cutoffInstant, validateCapture, validateCommits, validateLaptopCommits, validateLeave, validateTimeLog, validateDeveloper, validateGit, validateIngest } from "../src/validate.js";

const row = (over = {}) => ({
  tool: "claude-code", source: "abc123", session: "s-1", day: "2026-10-01", model: "claude-opus-5-5",
  repo: "Your-Org/Example-App", lines_added: 1, lines_removed: 0, input_tokens: 1, output_tokens: 1,
  cache_read_tokens: 0, cache_write_tokens: 0, ...over,
});
const stats = { files_parsed: 1, parse_errors: 0, bad_lines: 0, unrecognized_files: 0, skipped_unresolved: 0, skipped_other_org: 0 };
const body = (rows, over = {}) => ({ machine: "laptop", version: "0.1.0", os: "darwin", stats, rows, ...over });
const orgs = ["your-org"];

test("accepts a valid batch, lowercases the repo, and accepts an empty heartbeat", () => {
  assert.equal(validateIngest(body([row()])).rows[0].repo, "your-org/example-app");
  assert.deepEqual(validateIngest(body([])).rows, []);
});

test("keeps null line counts for tools that don't record them", () => {
  const out = validateIngest(body([row({ lines_added: null, lines_removed: null })]));
  assert.equal(out.rows[0].lines_added, null);
});

test("accepts activity, and collectors that don't send it", () => {
  const out = validateIngest(body([row({ active_slots: "ff", prompted_slots: "f", prompts: 2 })])).rows[0];
  assert.deepEqual([out.active_slots, out.prompted_slots, out.prompts], ["ff", "f", 2]);
  const old = validateIngest(body([row()])).rows[0];
  assert.deepEqual([old.active_slots, old.prompted_slots, old.prompts], [null, null, null]);
  const folders = "w0123456789abcdef,m0123456789abcdef";
  assert.equal(validateIngest(body([row({ folders })])).rows[0].folders, folders);
  assert.equal(validateIngest(body([row()])).rows[0].folders, null);
  assert.equal(validateIngest(body([row({ tool_version: "0.162.0-alpha.17.2" })])).rows[0].tool_version, "0.162.0-alpha.17.2");
  assert.equal(validateIngest(body([row()])).rows[0].tool_version, null);
  for (const over of [{ tool_version: "/Users/me" }, { tool_version: 2 }, { tool_version: "x".repeat(33) }, { folders: "/Users/me/app" }, { folders: "x0123456789abcdef" }, { folders: 5 }, { active_slots: "xyz" }, { prompted_slots: "f".repeat(73) }, { active_slots: 5 }, { prompts: -1 }]) {
    assert.ok(validateIngest(body([row(over)])).error, JSON.stringify(over));
  }
});

test("drops unknown fields so stray content can't be stored", () => {
  const out = validateIngest(body([row({ prompt: "secret" })], { extra: "secret" }));
  assert.ok(!JSON.stringify(out).includes("secret"));
});

test("accepts any org and the no-repo row", () => {
  assert.equal(validateIngest(body([row({ repo: "someone/personal" })])).rows[0].repo, "someone/personal");
  assert.equal(validateIngest(body([row({ repo: "-/no-repo" })])).rows[0].repo, "-/no-repo");
});

test("rejects bad values and oversized batches", () => {
  for (const over of [
    { repo: "no-slash" }, { day: "yesterday" }, { tool: "BAD TOOL" },
    { input_tokens: -1 }, { output_tokens: 1.5 }, { lines_added: "9" }, { model: "" }, { source: "a/b" },
  ]) {
    assert.ok(validateIngest(body([row(over)])).error, JSON.stringify(over));
  }
  assert.ok(validateIngest(body(Array(1001).fill(row()))).error);
  assert.ok(validateIngest(null).error);
  assert.ok(validateIngest(body([], { stats: {} })).error);
});

test("validates git rows", () => {
  const good = { month: "2026-10", repo: "your-org/example-app", person: "A", commits: 1, claude_commits: 0, lines_added: 1, lines_removed: 0, claude_lines_added: 0 };
  assert.equal(validateGit({ rows: [good] }, orgs).rows.length, 1);
  assert.ok(validateGit({ rows: [] }, orgs).error);
  assert.ok(validateGit({ rows: [{ ...good, month: "2026" }] }, orgs).error);
  assert.ok(validateGit({ rows: [{ ...good, repo: "x/y" }] }, orgs).error);
});

test("validates pull requests, and leaves them alone when an upload doesn't include them", () => {
  const good = { month: "2026-10", repo: "your-org/example-app", person: "A", commits: 1, claude_commits: 0, lines_added: 1, lines_removed: 0, claude_lines_added: 0 };
  const pr = { repo: "your-org/example-app", number: 7, person: "A", created_at: "2026-10-01T00:00:00Z", merged_at: null, human_review: false, any_review: false, title: "secret" };
  assert.equal(validateGit({ rows: [good] }, orgs).pullRequests, null);
  const out = validateGit({ rows: [good], pull_requests: [pr] }, orgs);
  assert.equal(out.pullRequests[0].merged_at, null);
  assert.ok(!JSON.stringify(out).includes("secret"));
  assert.equal(out.pullRequests[0].agent_review, null);
  for (const v of [true, false, null]) {
    assert.equal(validateGit({ rows: [good], pull_requests: [{ ...pr, agent_review: v }] }, orgs).pullRequests[0].agent_review, v);
  }
  for (const over of [{ repo: "x/y" }, { number: -1 }, { person: "" }, { merged_at: 5 }, { human_review: "yes" }, { any_review: 1 }, { agent_review: "yes" }, { agent_review: 0 }]) {
    assert.ok(validateGit({ rows: [good], pull_requests: [{ ...pr, ...over }] }, orgs).error, JSON.stringify(over));
  }
});

test("validates commits, and only stores known fields", () => {
  const good = { repo: "Your-Org/Example-App", oid: "a".repeat(40), person: "A", authored_at: "2026-10-01T10:00:00Z", ai: false, lines_added: 3, lines_removed: 1, message: "secret" };
  const out = validateCommits({ commits: [good] }, orgs);
  assert.equal(out.commits[0].repo, "your-org/example-app");
  assert.ok(!JSON.stringify(out).includes("secret"));
  assert.deepEqual(validateCommits({ commits: [] }, orgs).commits, []);
  for (const over of [{ repo: "x/y" }, { oid: "abc" }, { person: "" }, { authored_at: "2026-10-01T15:30:00+05:30" }, { ai: "no" }, { lines_added: -1 }, { lines_removed: "2" }]) {
    assert.ok(validateCommits({ commits: [{ ...good, ...over }] }, orgs).error, JSON.stringify(over));
  }
  assert.ok(validateCommits(null, orgs).error);
});

test("validates the time log, and rates or descriptions can't be stored", () => {
  const good = { day: "2026-10-02", person: "A", project: "Example App", seconds: 3600, repos: ["Your-Org/Example-App"], billable_rate: 99999, description: "secret" };
  const out = validateTimeLog({ from: "2026-10-01", rows: [good] });
  assert.deepEqual(out.rows, [{ day: "2026-10-02", person: "A", project: "Example App", seconds: 3600, repos: "your-org/example-app" }]);
  assert.ok(!JSON.stringify(out).includes("99999") && !JSON.stringify(out).includes("secret"));
  assert.deepEqual(validateTimeLog({ from: "2026-10-01", rows: [] }).rows, []);
  for (const over of [{ day: "2026-09-30" }, { day: "soon" }, { person: "" }, { project: "" }, { seconds: -1 }, { seconds: "9" }, { repos: "x/y" }, { repos: ["nope"] }]) {
    assert.ok(validateTimeLog({ from: "2026-10-01", rows: [{ ...good, ...over }] }).error, JSON.stringify(over));
  }
  assert.ok(validateTimeLog({ rows: [] }).error);
  assert.ok(validateTimeLog(null).error);
});

test("validates leave, and a type or reason can't be stored", () => {
  const body2 = (days) => ({ from: "2026-10-01", to: "2026-10-31", days });
  const out = validateLeave(body2([{ email: " A@Example.com ", day: "2026-10-02", type: "sick", reason: "secret" }]));
  assert.deepEqual(out.days, [{ email: "a@example.com", day: "2026-10-02", portion: 1 }]);
  assert.equal(validateLeave(body2([{ email: "a@b.co", day: "2026-10-02", portion: 0.5 }])).days[0].portion, 0.5);
  assert.equal(validateLeave(body2([{ email: "a@b.co", day: "2026-10-02", portion: 0.5 }, { email: "a@b.co", day: "2026-10-02", portion: 0.5 }])).days[0].portion, 1);
  assert.equal(validateLeave(body2([{ email: "a@b.co", day: "2026-10-02" }, { email: "A@b.co", day: "2026-10-02" }])).days.length, 1);
  assert.deepEqual(validateLeave(body2([])).days, []);
  for (const over of [{ email: "nope" }, { email: "" }, { day: "soon" }, { day: "2026-09-30" }, { day: "2026-11-01" }, { portion: 0.25 }, { portion: "1" }]) {
    assert.ok(validateLeave(body2([{ email: "a@b.co", day: "2026-10-02", ...over }])).error, JSON.stringify(over));
  }
  assert.ok(validateLeave({ from: "2026-10-31", to: "2026-10-01", days: [] }).error);
  assert.ok(validateLeave(null).error);
});

test("validates a laptop's commits from any host, and ignores any claim about who made them", () => {
  const good = { repo: "gitlab.com-Acme/App", oid: "a".repeat(40), authored_at: "2026-10-01T10:00:00Z", ai: false, on_default: false, lines_added: 3, lines_removed: 1, person: "Someone Else", message: "secret" };
  const out = validateLaptopCommits({ commits: [good] }).commits[0];
  assert.equal(out.repo, "gitlab.com-acme/app");
  assert.equal(out.on_default, false);
  assert.equal(out.person, undefined);
  assert.ok(!JSON.stringify(out).includes("secret"));
  for (const over of [{ repo: "no-slash" }, { oid: "abc" }, { authored_at: "soon" }, { ai: 1 }, { on_default: "no" }, { on_default: undefined }, { lines_added: -1 }, { lines_removed: "1" }]) {
    assert.ok(validateLaptopCommits({ commits: [{ ...good, ...over }] }).error, JSON.stringify(over));
  }
  assert.ok(validateLaptopCommits({ commits: [] }).error);
  assert.ok(validateLaptopCommits({ commits: Array(1001).fill(good) }).error);
  assert.ok(validateLaptopCommits(null).error);
});

test("validates developers", () => {
  assert.deepEqual(validateDeveloper({ name: " A ", email: "A@B.co" }), { name: "A", email: "a@b.co" });
  assert.ok(validateDeveloper({ name: "A", email: "nope" }).error);
  assert.ok(validateDeveloper({ email: "a@b.co" }).error);
});

test("the window follows IST days", () => {
  assert.equal(cutoffDay(1, new Date("2026-11-02T18:29:00Z")), "2026-11-02");
  assert.equal(cutoffDay(1, new Date("2026-11-02T18:30:00Z")), "2026-11-03");
  assert.equal(cutoffInstant("2026-11-03"), "2026-11-02T18:30:00Z");
  assert.equal(cutoffInstant("0000-00-00"), "0000-00-00");
});

test("only rows that say they're IST are marked IST", () => {
  assert.equal(validateIngest(body([row({ ist: true })])).rows[0].ist, true);
  assert.equal(validateIngest(body([row()])).rows[0].ist, false);
  assert.equal(validateIngest(body([row({ ist: "yes" })])).rows[0].ist, false);
});

test("cutoffDay includes today in the window, and 0 means all time", () => {
  const now = new Date("2026-11-02T12:00:00Z");
  assert.equal(cutoffDay(1, now), "2026-11-02");
  assert.equal(cutoffDay(30, now), "2026-10-04");
  assert.equal(cutoffDay(0, now), "0000-00-00");
});

test("old repo names resolve to the current repo, and others pass through", () => {
  assert.equal(currentRepo("your-org/example-app-legacy"), "your-org/example-app");
  assert.equal(currentRepo("your-org/example-app"), "your-org/example-app");
  for (const [from, to] of Object.entries(REPO_RENAMES)) {
    assert.equal(from, from.toLowerCase());
    assert.ok(!(to in REPO_RENAMES), `${to} must be a current name, not another old one`);
  }
});

test("validates captured text, across any repo, and only stores known fields", () => {
  const record = { tool: "claude-code", session: "s-1", at: "2026-11-05T10:00:00Z", role: "tool", agent: true, repo: "Your-Org/App", text: "hello", findings: [{ type: "aws-access-key", fingerprint: "abcdef0123456789", raw: "secret" }], extra: "secret" };
  const out = validateCapture({ machine: "laptop", records: [record] });
  assert.equal(out.records[0].repo, "your-org/app");
  assert.equal(out.records[0].agent, true);
  assert.equal(validateCapture({ machine: "laptop", records: [{ ...record, repo: "someone/personal" }] }).records[0].repo, "someone/personal");
  assert.equal(validateCapture({ machine: "laptop", records: [{ ...record, repo: "-/no-repo" }] }).records[0].repo, "-/no-repo");
  assert.ok(!JSON.stringify(out).includes("secret"));
  for (const over of [{ repo: "no-slash" }, { role: "system" }, { text: "" }, { text: "x".repeat(120001) }, { at: "soon" }, { findings: "no" }, { findings: [{ type: "BAD TYPE", fingerprint: "abcdef01" }] }, { session: "a/b" }]) {
    assert.ok(validateCapture({ machine: "laptop", records: [{ ...record, ...over }] }).error, JSON.stringify(over).slice(0, 60));
  }
  assert.ok(validateCapture({ machine: "laptop", records: [] }).error);
  assert.ok(validateCapture(null).error);
});

test("git uploads can say a commit is on another branch, and default to the default branch", () => {
  const good = { repo: "your-org/example-app", oid: "a".repeat(40), person: "Asha", authored_at: "2026-10-01T10:00:00Z", ai: false, lines_added: 1, lines_removed: 0 };
  assert.equal(validateCommits({ commits: [good] }, ["your-org"]).commits[0].on_default, true);
  assert.equal(validateCommits({ commits: [{ ...good, on_default: false }] }, ["your-org"]).commits[0].on_default, false);
  assert.ok(validateCommits({ commits: [{ ...good, on_default: "no" }] }, ["your-org"]).error);
});

test("git uploads can name a commit's branch and a PR's branch", () => {
  const good = { repo: "your-org/example-app", oid: "a".repeat(40), person: "Asha", authored_at: "2026-10-01T10:00:00Z", ai: false, lines_added: 1, lines_removed: 0 };
  assert.equal(validateCommits({ commits: [good] }, ["your-org"]).commits[0].branch, null);
  assert.equal(validateCommits({ commits: [{ ...good, branch: "feat/x" }] }, ["your-org"]).commits[0].branch, "feat/x");
  assert.ok(validateCommits({ commits: [{ ...good, branch: 5 }] }, ["your-org"]).error);
});
