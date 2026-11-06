// The Monday summary for managers: where logged time and activity disagree, and who isn't sending data.
// Names and counts only. No prompt text, secrets, commit details or anything from excluded repos.
export const DIGEST_CRON = "0 4 * * 1"; // Monday 09:30 IST

const DAY_MS = 86400000, IST_MS = 5.5 * 3600000, QUIET_DAYS = 2;
// People the digest never names: those who don't log time, have left, or aren't one person (a shared bot account).
// Use the names people.json gives them. Activity in leftOutRepos (this tracker's own, UPLOAD_REPO) doesn't count.
const LEFT_OUT = new Set(["Example Manager", "sharedcoder (shared account)"]);

const safe = (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const istDay = (at) => new Date(Date.parse(at) + IST_MS).toISOString().slice(0, 10);
const byName = (a, b) => a.localeCompare(b);
const days = (n) => `${n} ${n === 1 ? "day" : "days"}`;

export function buildDigest({ now, dashboardUrl, weekStart, commits, usageDays, loggedDays, leaveDays, machines, leftOutRepos = [] }) {
  const leftOut = new Set(leftOutRepos.map((repo) => repo.toLowerCase()));
  const age = (at) => Math.floor((now - Date.parse(at)) / DAY_MS);
  const counts = (person) => !LEFT_OUT.has(person);
  const active = new Map(), logged = new Map();
  const add = (map, person, day) => { if (day >= weekStart && counts(person)) map.set(person, (map.get(person) ?? new Set()).add(day)); };
  usageDays.filter((u) => !leftOut.has(u.repo)).forEach((u) => add(active, u.person, u.day));
  commits.filter((c) => !leftOut.has(c.repo)).forEach((c) => add(active, c.person, istDay(c.authored_at)));
  loggedDays.forEach((l) => add(logged, l.person, l.day));
  const fullLeave = new Set(leaveDays.filter((l) => l.portion === 1).map((l) => `${l.person}\n${l.day}`));
  const onLaptop = new Set(machines.map((m) => m.developer));

  const gaps = [];
  for (const person of [...new Set([...active.keys(), ...logged.keys()])].sort(byName)) {
    const worked = active.get(person) ?? new Set(), timed = logged.get(person) ?? new Set();
    const unlogged = [...worked].filter((day) => !timed.has(day) && !fullLeave.has(`${person}\n${day}`)).length;
    // Without a laptop reporting, activity is only commits, so a logged day with none proves nothing.
    const quiet = onLaptop.has(person) ? [...timed].filter((day) => !worked.has(day)).length : 0;
    const parts = [unlogged && `${days(unlogged)} active, nothing logged`, quiet && `${days(quiet)} logged, no activity`].filter(Boolean);
    if (parts.length) gaps.push(`• ${safe(person)}: ${parts.join("; ")}`);
  }

  const newest = machines.reduce((best, m) => ((m.version ?? "").localeCompare(best, undefined, { numeric: true }) > 0 ? m.version : best), "");
  const syncing = [];
  for (const m of machines.filter((m) => counts(m.developer)).sort((a, b) => byName(a.developer, b.developer))) {
    if (age(m.last_seen) > QUIET_DAYS) syncing.push(`• ${safe(m.developer)}: no report for ${days(age(m.last_seen))}`);
    else if (m.version !== newest) syncing.push(`• ${safe(m.developer)}: old version, partial data`);
  }
  for (const person of [...active.keys(), ...logged.keys()].filter((p, i, all) => all.indexOf(p) === i && !onLaptop.has(p)).sort(byName)) {
    syncing.push(`• ${safe(person)}: no laptop reporting`);
  }

  return [
    `*Project tracker, week from ${weekStart}*`,
    `*Time logged vs activity*\n${gaps.length ? gaps.join("\n") : "• No gaps"}`,
    `*Not syncing*\n${syncing.length ? syncing.join("\n") : "• Everyone is reporting"}`,
    `Meetings and non-AI work can explain a gap. <${dashboardUrl}|Open the dashboard>`,
  ].join("\n\n");
}
