#!/usr/bin/env node
// Builds the public demo: the real dashboard page, answered by made-up data in the browser, with no server.
// It removes prompt capture and secret detection, and refuses to write anything naming a real person, client or system.
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { REPO_RENAMES } from "../../src/repo_renames.js";

const url = (path) => new URL(path, import.meta.url);
const read = (path) => readFileSync(url(path), "utf8");
// The org's own names count as leaks wherever they appear, taken from ALLOWED_ORGS in wrangler.jsonc.
const ORG_NAMES = (read("../../wrangler.jsonc").match(/"ALLOWED_ORGS":\s*"([^"]+)"/)?.[1] ?? "").split(",").map((org) => org.trim().toLowerCase()).filter(Boolean);

// Each must match, so a change to the dashboard stops the build rather than quietly letting text through.
const SWAPS = [
  ["<title>Pulse</title>", "<title>Pulse demo: how a team works with AI</title>"],
  ["<h1>Pulse</h1>", "<h1>Pulse demo</h1>"],
  ['r.startsWith("your-org/")', 'r.startsWith("demo/")'],
  ["Your org's repos come from GitHub. Client repos come from laptops", "The team's own repos come from GitHub. Others come from laptops"],
  ["They come from GitHub for your org's repos and from laptops for client repos.", "They come from GitHub for the team's own repos and from laptops for others."],
  ["They can include generated files on your org's repos.", "They can include generated files on the team's own repos."],
  ["Most client repos land here.", "Most repos on other hosts land here."],
  ["Hours in solidtime.", "Hours in the time tracker."],
  ["hours logged in solidtime", "hours logged in the time tracker"],
  ["Solidtime projects that share", "Time tracker projects that share"],
  ["neither do Greptile's trial notices", "neither do trial notices from a review tool"],
  ["Greptile's trial notices don't count.", "Trial notices from a review tool don't count."],
  ["the harness secret scanner", "the harness's git checks"],
  ['url("/fonts/archivo-narrow.woff2")', 'url("fonts/archivo-narrow.woff2")'],
  ['url("/fonts/archivo.woff2")', 'url("fonts/archivo.woff2")'],
  ['data.canReadPrompts ? linkButton(number(d.prompts), () => openPrompts(person, d.day)) : number(d.prompts)', "number(d.prompts)"],
  ["    promptPanel(person),\n", ""],
  ["    if (!background) loadSecrets();\n", ""],
];

// Whole sections to remove, from the first text up to, not including, the second.
const CUTS = [
  [".prompt-view {", ".prompt-view {", "line"],
  [".prompt {", ".prompt {", "line"],
  [".prompt-text {", ".prompt-text {", "line"],
  ["let secrets = {", "let secrets = {", "line"],
  ["let secretView = null;", "function timelineBar(d)"],
  ['    ["Possible secrets",', '    ["Possible secrets",', "line"],
  ["  // Distinct secrets per person.", '  $("content").replaceChildren(el("div", { class: "below" },'],
  ['      fold("fold-secrets",', '      fold("fold-laptops",'],
  ["async function loadSecrets()", "async function load(background"],
];

// Anything here means prompt capture or secret detection is still described in the page.
const FORBIDDEN = /secret|captur|redact|slack|cloudflare|vercel|supabase|hyperdrive|part=|prompt-view|prompt-text|openPrompts|promptPanel|promptView|secretView|openSecretContext|loadSecrets|canReadPrompts|secret-context|fingerprint|captured|possible secret|see where|before upload|each view is logged|fold-secrets|\bsolidtime\b|\bclient|greptile/i;

function need(found, what) {
  if (!found) throw new Error(`The demo build no longer matches the dashboard: ${what}`);
}

export function demoPage(page, dataScript) {
  let html = page;
  for (const [from, to] of SWAPS) {
    need(html.includes(from), from);
    html = html.replaceAll(from, to);
  }
  for (const [start, end, line] of CUTS) {
    const from = html.indexOf(start);
    need(from >= 0, start);
    const to = line ? html.indexOf("\n", from) + 1 : html.indexOf(end, from);
    need(to > from, end);
    html = html.slice(0, from) + html.slice(to);
  }
  html = html.replace(/^[ \t]*\/\/.*\n/gm, "").replace(/([;{,)])[ \t]+\/\/ .*$/gm, "$1");
  need(html.includes("https://github.com/"), "GitHub links");
  html = html.replaceAll("https://github.com/", "#github.com/"); // made-up repos, and a real user owns github.com/demo
  // The page's own fetch calls stay as they are, and this answers them.
  const answer = `<script>${dataScript.replace(/^[ \t]*\/\/.*\n/gm, "")}
window.fetch = async (input) => {
  const days = new URL(input, "https://demo.invalid").searchParams.get("days");
  return new Response(JSON.stringify(demoData(Number(days))), { status: 200, headers: { "content-type": "application/json" } });
};
</script>`;
  const banner = '<p style="margin:0;padding:6px 32px;background:var(--ink);color:var(--paper);font-size:13px">Demo: every person, project and number here is made up.</p>';
  // After the charset tag, which browsers only honour in the first 1024 bytes.
  const charset = html.match(/<meta charset[^>]*>/);
  need(charset && charset.index < 1024, "meta charset in the first 1024 bytes");
  html = html.replace(charset[0], () => `${charset[0]}\n${answer}`);
  return html.replace(/<body[^>]*>/, (tag) => `${tag}\n${banner}`);
}

// Ordinary words inside real project names, which would otherwise stop the build on normal text.
export const COMMON_WORDS = new Set(["and", "the", "for", "app", "web", "api", "new", "main", "work", "team", "site", "website", "data", "project", "projects", "internal", "support", "time", "content", "engine", "brand", "docs", "mobile", "monorepo", "terraform", "development", "marketing", "sales", "accounts", "account", "ops", "dev", "admin", "test", "tests", "non", "billable", "leave", "misc", "general", "ai", "com", "github", "gmail", "automation", "setup", "enterprise", "ios", "local", "agent", "agents",
  // Words in this repo's placeholder values, such as your-org and example.com.
  "your", "org", "example"]);

function wordsOf(term) {
  return term.split(/[^\p{L}\p{N}]+/u).filter((word) => word.length > 2 && !COMMON_WORDS.has(word.toLowerCase()));
}

export function realTerms() {
  const people = JSON.parse(read("../../../people.json"));
  const projects = JSON.parse(read("../../../solidtime_projects.json"));
  const wrangler = read("../../wrangler.jsonc");
  const terms = new Set([...ORG_NAMES, "solidtime"]);
  const add = (term) => {
    terms.add(term);
    wordsOf(term).forEach((word) => terms.add(word));
  };
  for (const [name, ids] of Object.entries(people)) {
    add(name);
    add(name.replace(/\s+/g, "")); // joined, as in a handle
    name.split(/\s+/).forEach(add); // each word whole, accents and hyphens kept
    for (const id of ids) add(id);
  }
  for (const [project, repos] of Object.entries(projects)) {
    add(project);
    project.split(/ [-&] /).forEach(add); // a short form of a project name is as real as the whole
    for (const repo of [repos].flat()) add(String(repo));
  }
  for (const [from, to] of Object.entries(REPO_RENAMES)) {
    add(from);
    add(to);
  }
  for (const key of ["GITHUB_CLIENT_ID", "ACCESS_TEAM", "ACCESS_AUD", "PROMPT_READERS"]) {
    const value = wrangler.match(new RegExp(`"${key}":\\s*"([^"]+)"`))?.[1];
    if (value) value.split(",").forEach(add);
  }
  const hyperdrive = wrangler.match(/"binding":\s*"HYPERDRIVE",\s*"id":\s*"([^"]+)"/)?.[1];
  if (hyperdrive) terms.add(hyperdrive);
  return [...terms].filter((term) => term.length > 2 && !COMMON_WORDS.has(term.toLowerCase()));
}

export function findLeaks(html, terms) {
  const text = html.toLowerCase();
  const leaks = terms.filter((term) => new RegExp(`(^|[^a-z0-9])${term.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(s|\\d+)?($|[^a-z0-9])`).test(text));
  // The org's names count wherever they appear, even inside a longer word.
  for (const name of ORG_NAMES) if (text.includes(name) && !leaks.includes(name)) leaks.push(name);
  return leaks;
}

export function build(out = url("dist/"), page = read("../../src/dashboard.html"), dataScript = read("data.js")) {
  const html = demoPage(page, dataScript);
  const leaks = findLeaks(html, realTerms());
  if (leaks.length) throw new Error(`The demo would name ${leaks.length} real people, clients or systems. Fix these first: ${leaks.join(", ")}`);
  const banned = html.match(FORBIDDEN);
  if (banned) throw new Error(`The demo still describes prompt capture or secret detection: "${banned[0]}"`);
  mkdirSync(new URL("fonts/", out), { recursive: true });
  writeFileSync(new URL("index.html", out), html);
  for (const font of ["archivo.woff2", "archivo-narrow.woff2"]) copyFileSync(url(`../../src/fonts/${font}`), new URL(`fonts/${font}`, out));
  return new URL("index.html", out);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) console.log(`Built ${build().pathname}`);
