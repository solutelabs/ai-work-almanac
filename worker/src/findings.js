// Secret findings for the dashboard. They hold a type and a salted fingerprint, never the secret.
export function summarizeFindings(findings, cutoff) {
  const rows = new Map();
  for (const f of findings) {
    const day = f.at.slice(0, 10);
    if (day < cutoff) continue;
    const id = [f.person, f.type, f.repo, f.fingerprint].join("\n");
    const row = rows.get(id) ?? rows.set(id, { person: f.person, type: f.type, repo: f.repo, fingerprint: f.fingerprint, times: 0, last: "" }).get(id);
    row.times += 1;
    if (day > row.last) row.last = day;
  }
  return [...rows.values()];
}

const IST_MS = 5.5 * 3600000;
// Each file is one read, and Workers cap the reads in a request.
const MAX_PROMPT_FILES = 200;

// A person's typed prompts on one IST day. Text only appears here after the secret formats were removed on the laptop.
// shortcut: files are named by their first record's day, so only three days back are searched. A session left open longer can miss prompts.
export async function readPrompts(bucket, list, developerIds, day) {
  const start = Date.parse(`${day}T00:00:00Z`) - IST_MS;
  const keys = [];
  for (const id of developerIds) {
    for (let back = -1; back <= 3; back++) {
      const fileDay = new Date(Date.parse(`${day}T00:00:00Z`) - back * 86400000).toISOString().slice(0, 10);
      keys.push(...await list(`prompts/${fileDay}/${id}/`));
    }
  }
  const files = await Promise.all(keys.slice(0, MAX_PROMPT_FILES).map(async (key) => (await bucket.get(key))?.text() ?? ""));
  const seen = new Set(), prompts = [];
  for (const line of files.join("\n").split("\n")) {
    if (!line) continue;
    const r = JSON.parse(line), at = Date.parse(r.at);
    if (r.role !== "prompt" || r.agent || !(at >= start && at < start + 86400000)) continue;
    const id = `${r.session}\n${r.at}\n${r.text}`; // a batch re-sent after a failure can repeat records
    if (seen.has(id)) continue;
    seen.add(id);
    prompts.push({ at: r.at, tool: r.tool, repo: r.repo, text: r.text, truncated: r.truncated });
  }
  return { prompts: prompts.sort((a, b) => Date.parse(a.at) - Date.parse(b.at)), partial: keys.length > MAX_PROMPT_FILES };
}

// Shares the read limit between people, so one person's large backlog can't hide everyone else. Newest first for each.
export function fairShare(keys, max) {
  const byPerson = new Map();
  for (const key of [...keys].sort().reverse()) {
    const person = key.split("/")[2];
    byPerson.set(person, [...(byPerson.get(person) ?? []), key]);
  }
  const queues = [...byPerson.values()], picked = [];
  for (let round = 0; picked.length < max && queues.some((q) => round < q.length); round++) {
    for (const queue of queues) if (round < queue.length && picked.length < max) picked.push(queue[round]);
  }
  return picked;
}

const CONTEXT_CHARS = 200, MAX_CONTEXT_FILES = 60, MAX_CONTEXT_ROWS = 100;

// Where one person's possible secrets of one type appeared, with the surrounding text. The value itself was
// replaced on the laptop, so only the [REDACTED] marker can appear here.
export async function readSecretContext(bucket, list, developerIds, type, cutoff) {
  const keys = [];
  for (const id of developerIds) keys.push(...(await list("findings/")).filter((key) => key.split("/")[2] === String(id) && key.split("/")[1] >= cutoff));
  keys.sort().reverse();
  const files = await Promise.all(keys.slice(0, MAX_CONTEXT_FILES).map(async (key) => (await bucket.get(key.replace("findings/", "prompts/").replace(/\.json$/, ".jsonl")))?.text() ?? ""));
  const marker = `[REDACTED:${type}]`, rows = [];
  for (const line of files.join("\n").split("\n")) {
    if (!line) continue;
    const r = JSON.parse(line), found = (r.findings ?? []).find((f) => f.type === type);
    if (!found || r.at.slice(0, 10) < cutoff) continue;
    const at = r.text.indexOf(marker), start = Math.max(0, at - CONTEXT_CHARS), end = at < 0 ? CONTEXT_CHARS : at + marker.length + CONTEXT_CHARS;
    rows.push({ at: r.at, role: r.role, agent: r.agent === true, tool: r.tool, repo: r.repo, secret: found.fingerprint.slice(0, 8), text: `${start ? "…" : ""}${r.text.slice(start, end)}${end < r.text.length ? "…" : ""}` });
  }
  rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return { rows: rows.slice(0, MAX_CONTEXT_ROWS), partial: keys.length > MAX_CONTEXT_FILES || rows.length > MAX_CONTEXT_ROWS };
}
