import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { validateHarness } from "../src/validate.js";

const page = readFileSync(new URL("../src/dashboard.html", import.meta.url), "utf8");
const code = page.slice(page.indexOf("// hours:start"), page.indexOf("// hours:end"));
const { harnessStatus } = new Function(`${code}; return { harnessStatus };`)();

const NOW = Date.parse("2026-11-04T00:00:00Z");
const laptop = (over) => ({ developer: "A", machine: "m", version: "0.10.0", last_seen: "2026-11-03T00:00:00Z", ...over });
const report = (over) => ({ developer: "A", machine: "m", repo: "o/r", plugin: true, plugin_version: "2.4.0", hooks: true, ...over });
const status = (machines, harness, tools = new Set(["claude-code"])) => harnessStatus("A", "o/r", tools, machines, harness, NOW);

test("the plugin decides the colour, and hooks are only noted", () => {
  assert.deepEqual(status([laptop()], [report()]), { state: "green", why: "" });
  assert.deepEqual(status([laptop()], [report({ hooks: false })]), { state: "green", why: "git hooks off" });
  assert.deepEqual(status([laptop()], [report({ plugin: false })]), { state: "red", why: "plugin off" });
  assert.deepEqual(status([laptop()], [report({ plugin: false, hooks: false })]), { state: "red", why: "plugin off" });
});

test("no usable report is grey, never red", () => {
  assert.deepEqual(status([], []), { state: "grey", why: "laptop not reporting" });
  assert.deepEqual(status([laptop({ last_seen: "2026-10-01T00:00:00Z" })], [report()]), { state: "grey", why: "laptop not reporting" });
  assert.deepEqual(status([laptop({ version: "0.9.1" })], []), { state: "grey", why: "needs re-install" });
  assert.deepEqual(status([laptop()], [report({ repo: "o/other" })]), { state: "grey", why: "repo not found on laptop" });
  assert.deepEqual(status([laptop()], [report({ hooks: false })], new Set(["codex"])), { state: "grey", why: "Codex only" });
});

test("commits with no AI use are still judged, and the best fresh laptop counts", () => {
  assert.equal(status([laptop()], [report({ plugin: false })], new Set()).state, "red");
  assert.deepEqual(status([laptop(), laptop({ machine: "n" })], [report({ plugin: false }), report({ machine: "n" })]), { state: "green", why: "" });
  assert.equal(status([laptop(), laptop({ machine: "n", last_seen: "2026-10-01T00:00:00Z" })], [report({ plugin: false }), report({ machine: "n" })]).state, "red");
});

test("someone else's report doesn't count", () => {
  assert.equal(status([laptop()], [report({ developer: "B" })]).state, "grey");
});

test("harness uploads are checked field by field", () => {
  const repos = [{ repo: "O/R", plugin: true, plugin_version: "2.4.0", hooks: false, secret: "x" }];
  assert.deepEqual(validateHarness({ machine: "m", repos }), { machine: "m", repos: [{ repo: "o/r", plugin: true, plugin_version: "2.4.0", hooks: false }] });
  assert.deepEqual(validateHarness({ machine: "m", repos: [] }), { machine: "m", repos: [] });
  assert.ok(validateHarness({ repos: [] }).error);
  assert.ok(validateHarness({ machine: "m", repos: [{ ...repos[0], repo: "no-slash" }] }).error);
  assert.ok(validateHarness({ machine: "m", repos: [{ ...repos[0], plugin: "yes" }] }).error);
  assert.ok(validateHarness({ machine: "m", repos: [{ ...repos[0], hooks: 1 }] }).error);
  assert.ok(validateHarness({ machine: "m", repos: [{ ...repos[0], plugin_version: "a b" }] }).error);
});
