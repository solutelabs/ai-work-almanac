// Made-up people and projects for the public demo, built fresh from today's date so it never goes stale.
// Each past day's numbers, PR numbers and leave come from its date, so reloading or coming back later doesn't change them.
// Two things follow the clock on purpose: today fills in as the day goes on, and Ana Souza's most recent days stay empty, because her laptop isn't reporting.
const DEMO_PEOPLE = [
  { name: "Maya Chen", aiDays: 0.95, repos: ["demo/storefront", "demo/mobile-app"], tool: "claude-code", busy: 0.95, scale: 3, commits: 5, ai: 0.8, logs: 0.97, version: "0.10.0", seenHours: 1, plugin: true, hooks: true },
  { name: "Leo Martins", aiDays: 0.7, repos: ["demo/storefront"], tool: "claude-code", busy: 0.85, scale: 1.5, commits: 3, ai: 0.6, logs: 0.9, version: "0.10.0", seenHours: 2, plugin: true, hooks: false, feature: "feat/checkout-rewrite" },
  { name: "Priya Nair", aiDays: 0.9, repos: ["demo/data-platform"], tool: "claude-code", busy: 0.9, scale: 2.2, commits: 4, ai: 0.75, logs: 0.95, version: "0.10.0", seenHours: 1, plugin: true, hooks: true },
  { name: "Tom Becker", aiDays: 0.35, repos: ["demo/admin-portal"], tool: "claude-code", busy: 0.7, scale: 0.8, commits: 2, ai: 0.3, logs: 0.9, version: "0.10.0", seenHours: 3, plugin: false, hooks: false, older: true },
  { name: "Sara Lindqvist", aiDays: 0.6, repos: ["demo/mobile-app"], tool: "codex", busy: 0.8, scale: 1.2, commits: 3, ai: 0.5, logs: 0.92, version: "0.10.0", seenHours: 2 },
  { name: "Yusuf Demir", aiDays: 0.6, repos: ["demo/data-platform"], tool: "claude-code", busy: 0.75, scale: 1, commits: 2, ai: 0.5, logs: 0, version: "0.9.1", seenHours: 5 },
  { name: "Ana Souza", aiDays: 0.5, repos: ["demo/marketing-site"], tool: "claude-code", busy: 0.6, scale: 0.6, commits: 2, ai: 0.4, logs: 0.8, version: "0.10.0", seenHours: 240, plugin: true, hooks: true, quietDays: 10 },
  { name: "Ken Ito", aiDays: 0.75, repos: ["gitlab.com-acme/claims-portal"], tool: "claude-code", busy: 0.8, scale: 1.1, commits: 3, ai: 0.55, logs: 0.6, version: "0.10.0", seenHours: 1, plugin: true, hooks: false },
  { name: "Ola Nowak", repos: ["demo/admin-portal"], busy: 0.65, commits: 2, ai: 0, logs: 0.9 },
];
const DEMO_PROJECTS = { "demo/storefront": "Storefront", "demo/mobile-app": "Mobile app", "demo/data-platform": "Data platform", "demo/admin-portal": "Admin portal", "demo/marketing-site": "Marketing site", "gitlab.com-acme/claims-portal": "Acme claims" };

function demoData(days, now = Date.now()) {
  const DAY = 86400000, HISTORY = 90;
  const today = new Date(now + 5.5 * 3600000).toISOString().slice(0, 10);
  const dayAt = (ago) => new Date(Date.parse(today) - ago * DAY).toISOString().slice(0, 10);
  const stampAt = (ms) => new Date(ms).toISOString().slice(0, 19) + "Z";
  const random = (key) => {
    let h = 2166136261;
    for (const c of key) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    return () => { h = Math.imul(h ^ (h >>> 15), 2246822507) ^ Math.imul(h ^ (h >>> 13), 3266489909); return ((h ^= h >>> 16) >>> 0) / 4294967296; };
  };
  const hex = (r, n) => Array.from({ length: n }, () => Math.floor(r() * 16).toString(16)).join("");
  const slots = (from, count, keep = () => true) => {
    let bits = 0n;
    for (let i = 0; i < count; i += 1) if (keep(i)) bits |= 1n << BigInt(from + i);
    return bits.toString(16);
  };
  const usage = [], commits = [], timeLog = [], pullRequests = [], leave = [], machines = [], harness = [];
  const dayNumber = (day) => Math.floor(Date.parse(day) / DAY) - 20000;
  const prFor = (p, repo, r, ago, open) => {
    // Merged PRs number from their date. The open one has a fixed number far above that range, so neither moves or collides.
    const number = open ? 90000 + DEMO_PEOPLE.indexOf(p) : dayNumber(dayAt(ago)) * 10 + DEMO_PEOPLE.indexOf(p);
    const created = Date.parse(`${dayAt(ago)}T07:00:00Z`);
    const reviewed = r() < 0.7, merged = created + (2 + r() * 40) * 3600000, anyReview = r() < 0.5;
    // A few merged PRs are recorded, so the line shows counts. The rest stay unrecorded (null).
    const agentReview = number % 5 === 0 ? true : number % 5 === 1 ? false : null;
    if (created > now) return;
    pullRequests.push({ repo, number, person: p.name, created_at: stampAt(created), merged_at: open || merged > now ? null : stampAt(merged), human_review: reviewed, any_review: reviewed || anyReview, agent_review: agentReview, head_ref: open ? p.feature : `feat/change-${number}` });
  };
  for (const p of DEMO_PEOPLE) {
    for (let ago = HISTORY - 1; ago >= 0; ago -= 1) {
      const day = dayAt(ago), r = random(`${p.name}|${day}`);
      const weekday = new Date(`${day}T00:00:00Z`).getUTCDay();
      // Leo is off on the Tuesday and Wednesday of every fourth calendar week, so the dates depend only on the calendar.
      const onLeave = p.name === "Leo Martins" && Math.floor(Date.parse(day) / (7 * DAY)) % 4 === 0 && [2, 3].includes(weekday);
      if (onLeave) leave.push({ day, person: p.name, portion: 1, synced_at: stampAt(now) });
      if (weekday === 0 || weekday === 6 || onLeave || r() > p.busy || ago < (p.quietDays ?? 0)) continue;
      const repo = p.repos[Math.floor(r() * p.repos.length)];
      if (p.tool && r() < p.aiDays) {
        const start = 108 + Math.floor(r() * 18), planned = 40 + Math.floor(r() * 50);
        // Today only shows what has happened by now. Every number above and below is drawn either way, so it stays stable.
        const nowSlot = Math.floor(((now + 5.5 * 3600000) % DAY) / 300000);
        const length = ago === 0 ? Math.min(planned, Math.max(0, nowSlot - start)) : planned;
        const model = p.tool === "codex" ? "gpt-5" : p.older ? "claude-sonnet-4-5" : r() < 0.6 ? "claude-opus-5-5" : "claude-sonnet-5-5";
        const scale = p.scale * (0.6 + r());
        const share = length / planned; // today's totals grow with the part of the session that has happened
        if (length > 0) usage.push({ day, developer: p.name, tool: p.tool, repo, model, session: hex(r, 12), added: p.tool === "codex" ? null : Math.round(180 * scale * share), removed: p.tool === "codex" ? null : Math.round(60 * scale * share), input: Math.round(40000 * scale * share), output: Math.round(90000 * scale * share), cache_read: Math.round(2600000 * scale * share), cache_write: Math.round(180000 * scale * share), active: slots(start, length), prompted: slots(start, length, (i) => i % 4 === 0), prompts: Math.round(length / 4 + r() * 8 * share), folders: null, tool_version: p.tool === "codex" ? "0.9.0" : "2.1.0", ist: true });
      }
      for (let n = Math.round(r() * p.commits); n > 0; n -= 1) {
        const stage = r(), branch = stage < 0.6 ? "main" : stage < 0.8 ? "develop" : p.feature ?? "main";
        const authored = `${day}T${String(5 + Math.floor(r() * 5)).padStart(2, "0")}:${String(Math.floor(r() * 60)).padStart(2, "0")}:00Z`;
        const commit = { repo, person: p.name, authored_at: authored, ai: r() < p.ai, lines_added: Math.round(20 + r() * 300), lines_removed: Math.round(r() * 80), on_default: branch === "main", branch: repo.startsWith("demo/") ? branch : null, oid: hex(r, 40) };
        if (Date.parse(authored) <= now) commits.push(commit); // after every draw, so a skipped commit can't change what follows
      }
      if (r() < p.logs && ago > 0) timeLog.push({ day, person: p.name, project: DEMO_PROJECTS[repo], seconds: Math.round((5 + r() * 3.5) * 3600), repos: repo });
      if (r() < 0.25) prFor(p, repo, r, ago, false);
    }
    if (p.feature) prFor(p, p.repos[0], random(`${p.name}|${dayAt(12)}|open`), 12, true);
    if (p.version) {
      machines.push({ developer: p.name, machine: `${p.name.split(" ")[0].toLowerCase()}-laptop`, os: "darwin", version: p.version, last_seen: stampAt(now - p.seenHours * 3600000), parse_errors: 0, bad_lines: 0, unrecognized_files: 0, skipped_unresolved: 0, usage_rows: 60, first_ist_day: dayAt(HISTORY - 1) });
      if (p.plugin !== undefined) for (const repo of p.repos) harness.push({ developer: p.name, machine: `${p.name.split(" ")[0].toLowerCase()}-laptop`, repo, plugin: p.plugin, plugin_version: p.plugin ? "2.4.0" : null, hooks: p.hooks });
    }
  }
  const cutoff = days > 0 ? dayAt(days - 1) : "0000-00-00";
  const since = (rows, of) => rows.filter((row) => of(row) >= cutoff);
  return {
    cutoff, orgs: ["demo"], git: [], machines, harness,
    usage: since(usage, (u) => u.day), timeLog: since(timeLog, (t) => t.day), leave: since(leave, (l) => l.day),
    commits: since(commits, (c) => c.authored_at.slice(0, 10)),
    pullRequests: pullRequests.filter((pr) => !pr.merged_at || pr.merged_at.slice(0, 10) >= cutoff),
  };
}
