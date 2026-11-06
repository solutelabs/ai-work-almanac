import assert from "node:assert/strict";
import test from "node:test";
import { fairShare, readPrompts, readSecretContext, summarizeFindings } from "../src/findings.js";

const finding = (over) => ({ person: "A", type: "github-token", fingerprint: "f1", repo: "o/r", machine: "m", tool: "claude-code", at: "2026-11-02T10:00:00Z", ...over });

test("groups repeats of one secret, keeps the latest day, and leaves out older findings", () => {
  const rows = summarizeFindings([
    finding({}),
    finding({ at: "2026-11-03T10:00:00Z" }),
    finding({ fingerprint: "f2" }),
    finding({ at: "2026-10-01T10:00:00Z", fingerprint: "old" }),
  ], "2026-11-01");
  assert.equal(rows.length, 2);
  const first = rows.find((r) => r.fingerprint === "f1");
  assert.equal(first.times, 2);
  assert.equal(first.last, "2026-11-03");
});

test("sends only what the dashboard shows", () => {
  const [row] = summarizeFindings([finding({})], "2026-11-01");
  assert.deepEqual(Object.keys(row).sort(), ["fingerprint", "last", "person", "repo", "times", "type"]);
});

test("reads one person's typed prompts for one IST day, in order and without repeats", async () => {
  const record = (over) => ({ session: "s", at: "2026-11-02T04:00:00Z", role: "prompt", agent: false, tool: "claude-code", repo: "o/r", text: "hello", truncated: false, ...over });
  const lines = (...records) => records.map((r) => JSON.stringify(r)).join("\n") + "\n";
  const objects = {
    "prompts/2026-11-01/7/a.jsonl": lines(record({ at: "2026-11-01T19:00:00Z", text: "just after midnight IST" }), record({ at: "2026-11-01T18:00:00Z", text: "the day before in IST" })),
    "prompts/2026-11-02/7/b.jsonl": lines(record({ text: "second" }), record({ text: "second" }), record({ role: "response", text: "an answer" }), record({ agent: true, text: "a subagent" }), record({ at: "2026-11-02T18:30:00Z", text: "the next day in IST" })),
    "prompts/2026-11-02/8/c.jsonl": lines(record({ text: "someone else" })),
  };
  const list = async (prefix) => Object.keys(objects).filter((key) => key.startsWith(prefix));
  const bucket = { get: async (key) => ({ text: async () => objects[key] }) };
  const { prompts, partial } = await readPrompts(bucket, list, [7], "2026-11-02");
  assert.deepEqual(prompts.map((p) => p.text), ["just after midnight IST", "second"]);
  assert.equal(partial, false);
  assert.deepEqual(Object.keys(prompts[0]).sort(), ["at", "repo", "text", "tool", "truncated"]);
});

test("shares the read limit, so one person's backlog can't hide others", () => {
  const keys = ["findings/2026-11-03/1/a.json", "findings/2026-11-02/1/b.json", "findings/2026-11-01/1/c.json", "findings/2026-10-01/2/d.json"];
  assert.deepEqual(fairShare(keys, 2).sort(), ["findings/2026-10-01/2/d.json", "findings/2026-11-03/1/a.json"]);
  assert.equal(fairShare(keys, 10).length, 4);
});

test("finds the text around a person's secrets of one type, without any other records", async () => {
  const record = (over) => ({ session: "s", at: "2026-11-02T04:00:00Z", role: "tool", agent: false, tool: "claude-code", repo: "o/r", text: "x", findings: [], ...over });
  const objects = {
    "prompts/2026-11-02/7/a.jsonl": [
      record({ text: `${"a".repeat(300)} password=[REDACTED:assigned-secret] ${"b".repeat(300)}`, findings: [{ type: "assigned-secret", fingerprint: "0123456789abcdef" }] }),
      record({ text: "token [REDACTED:github-token]", findings: [{ type: "github-token", fingerprint: "ffffffffffffffff" }] }),
      record({ text: "nothing here" }),
    ].map((r) => JSON.stringify(r)).join("\n"),
  };
  const keys = ["findings/2026-11-02/7/a.json", "findings/2026-11-02/8/other.json", "findings/2026-01-01/7/old.json"];
  const bucket = { get: async (key) => (objects[key] ? { text: async () => objects[key] } : null) };
  const { rows, partial } = await readSecretContext(bucket, async () => keys, [7], "assigned-secret", "2026-11-01");
  assert.equal(rows.length, 1);
  assert.equal(partial, false);
  assert.equal(rows[0].secret, "01234567");
  assert.equal(rows[0].role, "tool");
  assert.ok(rows[0].text.includes("password=[REDACTED:assigned-secret]"));
  assert.ok(rows[0].text.startsWith("…") && rows[0].text.endsWith("…"));
  assert.ok(rows[0].text.length < 500);
});
