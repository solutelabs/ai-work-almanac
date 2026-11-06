import assert from "node:assert/strict";
import test from "node:test";
import { buildDigest } from "../src/digest.js";

const now = Date.parse("2026-11-09T04:00:00Z"), seen = "2026-11-09T03:00:00Z";
const base = { now, dashboardUrl: "https://x/dashboard", weekStart: "2026-11-02", commits: [], usageDays: [], loggedDays: [], leaveDays: [], machines: [] };
const repo = "your-org/example-app";
const machine = (developer, over = {}) => ({ developer, version: "0.9.1", last_seen: seen, ...over });

test("says so when there's nothing to report", () => {
  const text = buildDigest(base);
  assert.match(text, /• No gaps/);
  assert.match(text, /• Everyone is reporting/);
  assert.match(text, /<https:\/\/x\/dashboard\|Open the dashboard>/);
});

test("finds time logged and activity that disagree in both directions, in IST", () => {
  const text = buildDigest({ ...base, machines: [machine("Asha")],
    usageDays: [{ person: "Asha", day: "2026-11-03", repo }, { person: "Asha", day: "2026-11-04", repo }, { person: "Asha", day: "2026-10-01", repo }],
    commits: [{ person: "Asha", repo, authored_at: "2026-11-04T20:00:00Z" }], // 5 Nov in IST
    loggedDays: [{ person: "Asha", day: "2026-11-03" }, { person: "Asha", day: "2026-11-06" }] });
  assert.match(text, /• Asha: 2 days active, nothing logged; 1 day logged, no activity/);
});

test("doesn't flag someone whose logged days match their activity", () => {
  const text = buildDigest({ ...base, machines: [machine("Asha")], usageDays: [{ person: "Asha", day: "2026-11-03", repo }], loggedDays: [{ person: "Asha", day: "2026-11-03" }] });
  assert.match(text, /• No gaps/);
});

test("leaves out the listed people, the shared account, people who left, and this project", () => {
  const people = ["Example Manager", "sharedcoder (shared account)"];
  const text = buildDigest({ ...base, leftOutRepos: ["Your-Org/ai-work-almanac"],
    usageDays: [...people.map((person) => ({ person, day: "2026-11-03", repo })), { person: "Asha", day: "2026-11-03", repo: "your-org/ai-work-almanac" }],
    commits: [{ person: "Asha", repo: "your-org/ai-work-almanac", authored_at: "2026-11-03T05:00:00Z" }],
    loggedDays: people.map((person) => ({ person, day: "2026-11-04" })),
    machines: [machine("Asha"), ...people.map((p) => machine(p, { version: "0.1.0", last_seen: "2026-10-01T00:00:00Z" }))] });
  for (const person of [...people, "ai-work-almanac"]) assert.ok(!text.includes(person), person);
  assert.match(text, /• No gaps/);
  assert.match(text, /• Everyone is reporting/);
});

test("doesn't call a full leave day unlogged, but does count a half day", () => {
  const usageDays = [{ person: "Asha", day: "2026-11-03", repo }, { person: "Asha", day: "2026-11-04", repo }];
  const text = buildDigest({ ...base, machines: [machine("Asha")], usageDays, leaveDays: [{ person: "Asha", day: "2026-11-03", portion: 1 }, { person: "Asha", day: "2026-11-04", portion: 0.5 }] });
  assert.match(text, /• Asha: 1 day active, nothing logged/);
});

test("reports quiet laptops, old versions and people with no laptop, comparing versions numerically", () => {
  const text = buildDigest({ ...base,
    machines: [machine("Asha", { version: "0.10.0" }), machine("Ben"), machine("Cara", { version: "0.10.0", last_seen: "2026-11-01T00:00:00Z" })],
    commits: [{ person: "Dev", repo, authored_at: "2026-11-03T05:00:00Z" }], loggedDays: [{ person: "Dev", day: "2026-11-03" }, { person: "Dev", day: "2026-11-04" }] });
  assert.match(text, /• Ben: old version, partial data/);
  assert.match(text, /• Cara: no report for 8 days/);
  assert.match(text, /• Dev: no laptop reporting/);
  assert.doesNotMatch(text, /Asha/);
  assert.doesNotMatch(text, /Dev: .*logged, no activity/); // only commits are known for Dev, so this proves nothing
});

test("escapes names, so Slack can't be tricked into a mention", () => {
  const text = buildDigest({ ...base, machines: [machine("<!channel>", { last_seen: "2026-11-01T00:00:00Z" })] });
  assert.doesNotMatch(text, /<!channel>/);
  assert.match(text, /&lt;!channel&gt;/);
});
