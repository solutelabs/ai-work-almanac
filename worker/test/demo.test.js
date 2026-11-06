import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";
import { build, COMMON_WORDS, demoPage, findLeaks, realTerms } from "../dev/demo/build.mjs";
import { REPO_RENAMES } from "../src/repo_renames.js";

const script = readFileSync(new URL("../dev/demo/data.js", import.meta.url), "utf8");
const { demoData } = new Function(`${script}; return { demoData };`)();
const NOW = Date.parse("2026-11-04T06:00:00Z");

const people = JSON.parse(readFileSync(new URL("../../people.json", import.meta.url), "utf8"));
const projects = JSON.parse(readFileSync(new URL("../../solidtime_projects.json", import.meta.url), "utf8"));
const dashboard = readFileSync(new URL("../src/dashboard.html", import.meta.url), "utf8");
const firstName = Object.keys(people).find((name) => name.includes(" ")).split(" ")[0];
const lastName = Object.keys(people).find((name) => name.includes(" ")).split(" ").pop();
const wrangler = readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8");
const wranglerValues = ["GITHUB_CLIENT_ID", "ACCESS_TEAM", "ACCESS_AUD", "PROMPT_READERS"].flatMap((key) => (wrangler.match(new RegExp(`"${key}":\\s*"([^"]+)"`))?.[1] ?? "").split(",")).filter(Boolean);
// Every real name the files hold, worked out here on its own so the build's list can be checked against it.
// Messages never carry the names themselves.
const segments = (project) => project.split(/ [-&] /);
const everyName = [...new Set([
  ...Object.keys(people), ...Object.keys(people).map((name) => name.replace(/\s+/g, "")), ...Object.values(people).flat(),
  ...Object.keys(projects), ...Object.keys(projects).flatMap(segments), ...Object.values(projects).flat().map(String),
  ...Object.entries(REPO_RENAMES).flat(), ...wranglerValues,
])].filter((term) => term.length > 2 && !COMMON_WORDS.has(term.toLowerCase()));
const everyWord = [...new Set(Object.keys(people).flatMap((name) => name.split(/\s+/)))].filter((word) => word.length > 2 && !COMMON_WORDS.has(word.toLowerCase()));
const renameName = Object.keys(REPO_RENAMES)[0];
const segment = Object.keys(projects).flatMap(segments).find((part) => !COMMON_WORDS.has(part.toLowerCase()) && Object.keys(projects).some((project) => project !== part && segments(project).includes(part)));
const secondPerson = Object.keys(people)[1].split(" ")[0];
const wranglerValue = wranglerValues.find((value) => value.length > 2);
const email = Object.values(people).flat().find((id) => id.includes("@"));
const login = Object.values(people).flat().find((id) => !id.includes("@"));
const owner = Object.values(projects).flat().map(String).map((repo) => repo.split("/")[0]).find((name) => name.toLowerCase() !== "your-org");

function buildWith(extra, inScript = false) {
  const out = mkdtempSync(join(tmpdir(), "demo-"));
  try {
    return inScript ? build(pathToFileURL(`${out}/`), dashboard, `${script}\n/* ${extra} */`) : build(pathToFileURL(`${out}/`), dashboard.replace("</body>", `<!-- ${extra} --></body>`));
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
}

test("the leak check knows the real names, and leaves similar words alone", () => {
  const terms = realTerms();
  for (const term of [firstName, lastName, email, login, owner].filter(Boolean)) assert.ok(terms.includes(term), "a real name is missing from the leak check");
  for (const term of everyName) assert.ok(terms.includes(term), "a real name is missing from the leak check");
  for (const term of everyName) assert.ok(findLeaks(`see ${term} here`, terms).length, "a real name isn't caught in text");
  for (const word of everyWord) for (const form of [`${word}s`, `${word}2`, word.toUpperCase()]) assert.ok(findLeaks(`see ${form} here`, terms).length, "a changed form of a real name isn't caught");
  for (const word of ["assessment", "poc", "case", "study", "wordpress", "shopify", "sanity"]) assert.ok(!COMMON_WORDS.has(word), "a word that can sit in a real project name is allowed");
  assert.deepEqual(findLeaks("Ask YOUR-ORG/app.", ["your-org"]), ["your-org"]);
  assert.deepEqual(findLeaks("a.b@example.com", ["a.b@example.com"]), ["a.b@example.com"]);
  assert.deepEqual(findLeaks("the scar healed", ["car"]), []);
  assert.deepEqual(findLeaks("your-orgTracker", []), ["your-org"]);
});

test("the demo builds with nothing real in it", () => {
  const out = mkdtempSync(join(tmpdir(), "demo-"));
  try {
    const page = readFileSync(build(pathToFileURL(`${out}/`)), "utf8");
    assert.deepEqual(findLeaks(page, realTerms()), []);
    assert.ok(statSync(join(out, "fonts", "archivo.woff2")).size > 0);
    assert.ok(page.includes("Harness use"));
    assert.ok(page.includes("demoData"));
    assert.ok(page.indexOf("<meta charset") + 20 < 1024, "the charset tag is past the first 1024 bytes");
    assert.ok(page.indexOf("<script>") > page.indexOf("<meta charset"));
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test("the build stops if the page gains a real name", () => {
  for (const planted of [firstName, lastName, email, login, owner, secondPerson, renameName, segment, wranglerValue, Object.keys(people)[1].replace(" ", ""), `${firstName}s`, `${firstName}7`, Object.keys(projects)[0]].filter(Boolean)) assert.throws(() => buildWith(planted), /would name/);
  // The page's own text isn't the only way in: the data script is part of what ships.
  for (const planted of [firstName, secondPerson, segment, renameName]) assert.throws(() => buildWith(planted, true), /would name/);
  assert.throws(() => buildWith("Acme your-org2"), /would name/);
});

test("the build stops if the page describes prompt capture or secret detection", () => {
  for (const words of ["Each view is logged", "a type and a fingerprint", "part=prompts", "Possible secrets", "secret scanner", "Captured text", "redacted", "Slack", "Cloudflare", "Vercel", "Supabase", "Hyperdrive"]) assert.throws(() => buildWith(words), /still describes/);
});

test("the build stops if the dashboard no longer matches what it removes", () => {
  assert.throws(() => demoPage(dashboard.replace("let secretView = null;", ""), ""), /no longer matches/);
  assert.throws(() => demoPage(dashboard.replace("Most client repos land here.", "Most repos land here."), ""), /no longer matches/);
});

test("the same day gives the same numbers", () => {
  // Today fills in as the day goes on, so only earlier days are compared.
  const today = "2026-11-04";
  const earlier = (rows, dayOf) => rows.filter((row) => dayOf(row) < today);
  const work = (data) => ({ usage: earlier(data.usage, (u) => u.day), timeLog: earlier(data.timeLog, (t) => t.day), harness: data.harness, commits: earlier(data.commits, (c) => c.authored_at.slice(0, 10)), pullRequests: earlier(data.pullRequests, (pr) => pr.created_at.slice(0, 10)) });
  assert.deepEqual(work(demoData(30, NOW)), work(demoData(30, NOW + 3600000)));
});

test("a past day keeps its numbers as later days arrive", () => {
  const usage = (now) => demoData(0, now).usage.filter((u) => u.day === "2026-10-20");
  assert.ok(usage(NOW).length);
  assert.deepEqual(usage(NOW), usage(NOW + 5 * 86400000));
});

test("nothing is dated in the future, at any time of day", () => {
  for (const hour of ["00:30", "04:59", "12:00", "23:50"]) {
    const now = Date.parse(`2026-11-04T${hour}:00Z`);
    const data = demoData(0, now);
    assert.ok(data.commits.every((c) => Date.parse(c.authored_at) <= now));
    assert.ok(data.pullRequests.every((pr) => Date.parse(pr.created_at) <= now && (!pr.merged_at || Date.parse(pr.merged_at) <= now)));
    const nowIst = new Date(now + 5.5 * 3600000).toISOString().slice(0, 10);
    assert.ok(!data.timeLog.some((t) => t.day === nowIst));
    const slot = Math.floor(((now + 5.5 * 3600000) % 86400000) / 300000);
    for (const u of data.usage.filter((row) => row.day === nowIst)) assert.ok(BigInt(`0x${u.active}`) < 1n << BigInt(slot), "today shows activity that hasn't happened");
  }
});

test("leave falls on fixed working days chosen from the date", () => {
  const leave = (now) => demoData(0, now).leave;
  const first = leave(NOW);
  assert.ok(first.length >= 4);
  for (const l of first) assert.ok([2, 3].includes(new Date(`${l.day}T00:00:00Z`).getUTCDay()));
  // Viewing it on any later day keeps every date that was already there.
  for (let day = 1; day < 40; day += 1) {
    const later = leave(NOW + day * 86400000).map((l) => l.day);
    for (const l of first) if (l.day > "2026-10-01") assert.ok(later.includes(l.day), "a leave date moved"); // older ones leave the 90 days
    for (const l of later) assert.ok(![0, 6].includes(new Date(`${l}T00:00:00Z`).getUTCDay()));
  }
  assert.ok(!demoData(0, NOW).usage.some((u) => u.developer === "Leo Martins" && first.some((l) => l.day === u.day)), "worked on leave");
});

test("an open PR keeps one number, outside the merged range", () => {
  const open = (now) => demoData(0, now).pullRequests.filter((pr) => !pr.head_ref.startsWith("feat/change-")).map((pr) => pr.number);
  assert.ok(open(NOW).length);
  assert.deepEqual(open(NOW), open(NOW + 9 * 86400000));
  const merged = demoData(0, NOW).pullRequests.filter((pr) => pr.merged_at).map((pr) => pr.number);
  assert.ok(open(NOW).every((n) => n > Math.max(...merged)));
});

test("today's numbers grow with the part of the session that has happened", () => {
  const today = (hour) => demoData(0, Date.parse(`2026-11-04T${hour}:00Z`)).usage.filter((u) => u.day === "2026-11-04");
  const total = (hour) => today(hour).reduce((sum, u) => sum + u.output + u.prompts, 0);
  const totals = ["04:30", "06:00", "08:00", "23:00"].map(total);
  assert.ok(totals[0] < totals[1] && totals[1] < totals[2] && totals[2] <= totals[3]);
  const full = today("23:00");
  assert.ok(today("06:00").every((u) => full.find((f) => f.session === u.session).output >= u.output));
});

test("a PR keeps its number as days pass", () => {
  const numbers = (now) => new Map(demoData(0, now).pullRequests.filter((pr) => pr.merged_at).map((pr) => [`${pr.repo}|${pr.created_at}|${pr.person}`, pr.number]));
  const later = numbers(NOW + 5 * 86400000);
  let shared = 0;
  for (const [key, number] of numbers(NOW)) if (later.has(key)) { shared += 1; assert.equal(later.get(key), number); }
  assert.ok(shared > 20);
});

test("the period filter keeps to its window, and open PRs always show", () => {
  const week = demoData(7, NOW);
  assert.ok(week.usage.length && week.usage.every((u) => u.day >= week.cutoff));
  assert.ok(week.commits.every((c) => c.authored_at.slice(0, 10) >= week.cutoff));
  assert.ok(week.pullRequests.some((pr) => !pr.merged_at));
  assert.ok(demoData(0, NOW).usage.length > demoData(30, NOW).usage.length);
});

test("it reaches the states the dashboard is there to show", () => {
  const data = demoData(30, NOW);
  assert.ok(data.harness.some((h) => h.plugin && h.hooks) && data.harness.some((h) => h.plugin && !h.hooks) && data.harness.some((h) => !h.plugin));
  assert.ok(data.commits.some((c) => c.branch === "feat/checkout-rewrite"));
  assert.ok(data.commits.some((c) => c.branch === null));
  assert.ok(data.usage.some((u) => u.tool === "codex" && u.added === null));
  assert.ok(data.leave.length > 0);
  assert.equal(new Set(data.commits.map((c) => c.oid)).size, data.commits.length);
  assert.ok(data.commits.some((c) => c.person === "Ola Nowak") && !data.machines.some((m) => m.developer === "Ola Nowak"));
});

test("the demo drops Greptile from the page and refuses it if it comes back", () => {
  assert.ok(!/greptile/i.test(demoPage(dashboard, script)));
  assert.throws(() => buildWith("Greptile"), /Greptile/);
});
