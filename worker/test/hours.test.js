import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const page = readFileSync(new URL("../src/dashboard.html", import.meta.url), "utf8");
const code = page.slice(page.indexOf("// hours:start"), page.indexOf("// hours:end"));
const { timeBy, median, latestToolVersions, classifyCommits, logBy, leaveBy, projectGroups, promptHours, commitStage, offServerWork, dayTimeline, dayStretches, tokensPerHour, tokensPerHourCell, emptyGroup, addPr, claudeReviewLine, reportingWindow, hoursInWindow, tokensPerReportedHour } = new Function(`${code}; return { timeBy, median, latestToolVersions, classifyCommits, logBy, leaveBy, projectGroups, promptHours, commitStage, offServerWork, dayTimeline, dayStretches, tokensPerHour, tokensPerHourCell, emptyGroup, addPr, claudeReviewLine, reportingWindow, hoursInWindow, tokensPerReportedHour };`)();
const row = (over) => ({ developer: "A", day: "2026-11-01", repo: "r", active: null, prompted: null, prompts: null, ...over });
const blocks = (hours) => Math.round(hours * 12);
const hex = (...slots) => slots.reduce((all, slot) => all | (1n << BigInt(slot)), 0n).toString(16);

test("overlapping sessions count once for a person, and people add up", () => {
  const rows = [
    row({ active: `${hex(1, 2)},${hex(2, 3)}` }),
    row({ active: hex(3, 4), repo: "s" }),
    row({ active: hex(1), developer: "B" }),
  ];
  assert.equal(blocks(timeBy(rows, (u) => u.developer).get("A").running), 4);
  assert.equal(blocks(timeBy(rows, () => "team").get("team").running), 5);
  assert.equal(blocks(timeBy(rows, (u) => u.repo).get("s").running), 2);
});

test("prompts within 15 minutes join into one stretch, and longer gaps don't", () => {
  const time = (...slots) => timeBy([row({ active: hex(...Array.from({ length: 40 }, (_, i) => i)), prompted: hex(...slots), prompts: slots.length })], () => "x").get("x");
  assert.equal(blocks(time(5).handsOn), 1);
  assert.equal(blocks(time(5, 8).handsOn), 4);
  assert.equal(blocks(time(5, 9).handsOn), 2);
  assert.equal(time(5, 8).prompts, 2);
});

test("hands-on never exceeds AI running, and old collectors leave no entry", () => {
  const time = timeBy([row({ active: hex(5, 8), prompted: hex(5, 8) })], () => "x").get("x");
  assert.equal(time.handsOn, time.running);
  assert.equal(timeBy([row({})], () => "x").size, 0);
});

test("worktrees are counted once and overlap needs two separate folders", () => {
  const a = "w0000000000000001", b = "w0000000000000002", clone = "m0000000000000003";
  const time = timeBy([
    row({ active: hex(1, 2, 3), folders: a }),
    row({ active: hex(3, 4), folders: a }), // a second session in the same folder isn't parallel
    row({ active: hex(4, 5), folders: b }),
    row({ active: hex(5), folders: clone }),
    row({ active: hex(9), day: "2026-11-02", folders: a }),
  ], () => "x").get("x");
  assert.equal(time.worktrees.size, 2);
  assert.equal(time.folders.size, 3);
  assert.equal(blocks(time.parallel), 2);
  assert.equal(timeBy([row({ active: hex(1) })], () => "x").get("x").folders.size, 0);
});

test("shows each person's most recent version and flags who's behind the team's newest", () => {
  const out = latestToolVersions([
    row({ tool: "claude-code", tool_version: "2.1.9", day: "2026-11-01" }),
    row({ tool: "claude-code", tool_version: "2.1.10", day: "2026-11-02" }),
    row({ tool: "claude-code", tool_version: "2.1.300", day: "2026-10-01", developer: "B" }),
    row({ tool: "codex", tool_version: "0.1.0" }),
    row({ tool: "claude-code", tool_version: null, developer: "C" }),
  ]);
  const of = (person, tool) => out.find((r) => r.person === person && r.tool === tool);
  assert.deepEqual([of("A", "claude-code").version, of("A", "claude-code").behind], ["2.1.10", true]);
  assert.deepEqual([of("B", "claude-code").version, of("B", "claude-code").behind], ["2.1.300", false]);
  assert.equal(of("A", "codex").behind, false);
  assert.equal(out.length, 3);
});

test("the page's script parses, since a syntax error leaves the dashboard stuck on Loading", () => {
  new Function(page.slice(page.indexOf("<script>") + 8, page.indexOf("</script>")));
});

test("sorts commits by what's known about AI's part, and never calls missing data manual", () => {
  const person = "A", repo = "r";
  // 12:00 IST on 2 Nov is slot 144, and 06:30 UTC.
  const commit = (over) => ({ person, repo, authored_at: "2026-11-02T06:30:00Z", ai: false, lines_added: 1, ...over });
  const usage = (slot, over) => ({ developer: person, repo, day: "2026-11-02", active: hex(slot), ist: true, ...over });
  const laptop = (over) => ({ developer: person, version: "0.7.2", first_ist_day: "2026-10-01", last_seen: "2026-11-03T00:00:00Z", ...over });
  const kind = (c, rows, machines = [laptop()]) => { const one = commit(c); classifyCommits([one], rows, machines); return one.kind; };

  assert.equal(kind({ ai: true }, []), "marked");
  assert.equal(kind({}, [usage(144)]), "nearby"); // same block
  assert.equal(kind({}, [usage(108)]), "nearby"); // exactly 3 hours before
  assert.equal(kind({}, [usage(107)]), "noAi"); // just over 3 hours before
  assert.equal(kind({}, [usage(145)]), "noAi"); // after the commit
  assert.equal(kind({}, [usage(144, { repo: "other" })]), "noAi"); // a different repo
  assert.equal(kind({}, [usage(144, { developer: "B" })]), "noAi"); // someone else
  // Reaches back past midnight: 00:30 IST on 3 Nov, with AI at 23:00 IST on 2 Nov.
  assert.equal(kind({ authored_at: "2026-11-02T19:00:00Z" }, [usage(276)]), "nearby");

  assert.equal(kind({}, []), "noAi");
  assert.equal(kind({}, [], []), "unknown"); // no laptop
  assert.equal(kind({}, [], [laptop({ version: "0.7.1" })]), "unknown"); // too old to report activity
  assert.equal(kind({}, [], [laptop(), laptop({ version: "0.4.0" })]), "unknown"); // one of two laptops is old
  assert.equal(kind({}, [], [laptop({ last_seen: "2026-11-02T00:00:00Z" })]), "unknown"); // committed after its last report
  assert.equal(kind({}, [], [laptop({ first_ist_day: "2026-11-03" })]), "unknown"); // before its logs begin
  assert.equal(kind({}, [], [laptop({ first_ist_day: null })]), "unknown");
  assert.equal(kind({}, [usage(144, { ist: false })], [laptop({ version: "0.7.1" })]), "unknown"); // UTC rows can't place a commit
});

test("adds up logged time", () => {
  const rows = [
    { day: "2026-11-01", person: "A", project: "P", seconds: 3600, repos: "o/a,o/b" },
    { day: "2026-11-02", person: "A", project: "P", seconds: 1800, repos: "o/a" },
    { day: "2026-11-02", person: "B", project: "Q", seconds: 7200, repos: "" },
  ];
  const a = logBy(rows, (t) => t.person).get("A");
  assert.equal(a.hours, 1.5);
  assert.equal(a.days.size, 2);
  assert.deepEqual([...a.repos], ["o/a", "o/b"]);
  assert.equal(logBy(rows, (t) => t.project).get("Q").repos.size, 0);
});

test("counts leave, and only work on a full day off stands out", () => {
  const leave = [
    { person: "A", day: "2026-11-01", portion: 1 },
    { person: "A", day: "2026-11-02", portion: 1 },
    { person: "A", day: "2026-11-03", portion: 0.5 },
  ];
  const out = leaveBy(leave, () => new Set(["2026-11-02", "2026-11-03"])).get("A");
  assert.deepEqual(out, { days: 2.5, workedOn: 1 });
});

test("median", () => {
  assert.equal(median([]), null);
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1]), 2.5);
});

test("projects sharing a repo become one group, and every repo appears once", () => {
  const log = (project, repos) => ({ project, repos, person: "A", day: "2026-11-01", seconds: 3600 });
  const groups = projectGroups([log("v2", "o/mono"), log("support", "o/mono"), log("poc", "o/a,o/b"), log("bridge", "o/b,o/c"), log("internal", "")], ["o/mono", "o/a", "o/d", "o/d"]);
  const shape = groups.map((g) => [[...g.projects].sort().join("+"), [...g.repos].sort().join("+")]).sort();
  assert.deepEqual(shape, [["", "o/d"], ["bridge+poc", "o/a+o/b+o/c"], ["internal", ""], ["support+v2", "o/mono"]]);
  const seen = groups.flatMap((g) => [...g.repos]);
  assert.equal(new Set(seen).size, seen.length);
});

test("prompt hours count a block once, place it in the right hour, and skip UTC rows", () => {
  const rows = [
    row({ prompted: `${hex(120, 121)},${hex(121)}`, ist: true }), // 10:00 and 10:05, with overlap
    row({ prompted: hex(228), ist: true, repo: "s" }), // 19:00
    row({ prompted: hex(0), ist: false }), // UTC, so its hour can't be trusted
    row({ prompted: hex(132), ist: true, day: "2026-11-02" }), // 11:00
    row({ prompted: null, ist: true, developer: "B" }),
  ];
  const [person, ...others] = promptHours(rows);
  assert.equal(others.length, 0);
  assert.equal(person.slots[10], 2);
  assert.equal(person.slots[19], 1);
  assert.equal(person.slots[0], 0);
  assert.equal(person.days, 2);
  assert.equal(person.first, (10 * 60 + 11 * 60) / 2);
  assert.equal(person.last, (19 * 60 + 11 * 60) / 2);
});

test("sorts commits by whether they reached a server", () => {
  assert.equal(commitStage({ on_default: true, branch: "main" }), "main");
  assert.equal(commitStage({ on_default: true, branch: null }), "main");
  assert.equal(commitStage({ on_default: false, branch: "Master" }), "main");
  assert.equal(commitStage({ on_default: true, branch: "develop" }), "develop");
  assert.equal(commitStage({ on_default: false, branch: "stage" }), "develop");
  assert.equal(commitStage({ on_default: false, branch: "feat/x" }), "branch");
  assert.equal(commitStage({ on_default: false, branch: null }), "unknown");
});

test("lists branch work per person, oldest first, with any open PR", () => {
  const commit = (over) => ({ repo: "o/r", branch: "feat/a", person: "A", on_default: false, authored_at: "2026-10-05T00:00:00Z", ...over });
  const rows = offServerWork([
    commit({}),
    commit({ authored_at: "2026-10-01T00:00:00Z" }),
    commit({ branch: "feat/b", authored_at: "2026-09-01T00:00:00Z" }),
    commit({ branch: "main" }),
    commit({ branch: null }),
  ], [{ repo: "o/r", head_ref: "feat/a", number: 7, merged_at: null }, { repo: "o/r", head_ref: "feat/b", number: 8, merged_at: "2026-10-01" }]);
  assert.deepEqual(rows.map((r) => [r.branch, r.commits, r.pr]), [["feat/b", 1, null], ["feat/a", 2, 7]]);
  assert.equal(rows[1].first, "2026-10-01T00:00:00Z");
  assert.equal(rows[1].last, "2026-10-05T00:00:00Z");
});

test("a day's timeline joins blocks, finds first and last, and places commits in IST", () => {
  const rows = [
    row({ ist: true, active: hex(120, 121, 122, 123, 180), prompted: hex(120, 121), prompts: 3 }),
    row({ ist: true, active: hex(121), prompted: hex(130), prompts: 2, repo: "s" }),
    row({ ist: false, day: "2026-11-01", active: hex(1), prompted: hex(1), prompts: 9 }),
  ];
  const commits = [{ authored_at: "2026-11-01T10:00:00Z" }, { authored_at: "2026-11-01T20:00:00Z" }];
  const [next, day] = dayTimeline(rows, commits);
  assert.equal(next.day, "2026-11-02"); // 20:00 UTC is 01:30 the next morning in IST
  assert.deepEqual(next.commits, [90]);
  assert.equal(next.firstPrompt, null);
  assert.equal(next.lastActivity, 90);
  assert.equal(day.prompts, 5);
  assert.deepEqual(day.promptRuns, [[120, 122], [130, 131]]);
  assert.deepEqual(day.activeRuns, [[120, 124], [180, 181]]);
  assert.equal(day.firstPrompt, 600);
  assert.equal(blocks(day.promptSpan), 11); // 10:00 to the end of the 10:50 block
  assert.equal(next.promptSpan, null);
  assert.equal(day.lastPrompt, 650);
  assert.deepEqual(day.commits, [930]); // 10:00 UTC is 15:30 in IST
  assert.equal(day.lastActivity, 930); // the commit came after the last AI block
});

test("joins a day's activity into stretches, up to 15 minutes apart, and counts commits in each", () => {
  const stretches = dayStretches({ activeRuns: [[120, 124]], promptRuns: [[120, 121], [127, 128], [140, 141]], commits: [610, 705, 900] });
  assert.deepEqual(stretches, [
    { from: 600, to: 640, commits: 1 }, // 10:00, with a gap of exactly 15 minutes joined
    { from: 700, to: 710, commits: 1 },
    { from: 900, to: 905, commits: 1 },
  ]);
});

test("tokens per hour is null without positive hours", () => {
  assert.equal(tokensPerHour(1000, 0), null);
  assert.equal(tokensPerHour(1000, null), null);
  assert.equal(tokensPerHour(1000, undefined), null);
  assert.equal(tokensPerHour(1000, -2), null);
  assert.equal(tokensPerHour(1000, 4), 250);
});

test("tokens per hour cell is a dash when it can't be worked out", () => {
  const none = { model: "", tool: "" };
  assert.equal(tokensPerHourCell(1000, 3, 4, none), 250);
  assert.equal(tokensPerHourCell(1000, 3, 4, { model: "opus", tool: "" }), null);
  assert.equal(tokensPerHourCell(1000, 3, 4, { model: "", tool: "codex" }), null);
  assert.equal(tokensPerHourCell(1000, 0, 4, none), null);
  assert.equal(tokensPerHourCell(1000, 3, undefined, none), null);
  assert.equal(tokensPerHourCell(1000, 3, 0, none), null);
});

test("Claude Code review line counts merged PRs only", () => {
  const pr = (over) => ({ merged_at: "2026-11-02", created_at: "2026-11-01", human_review: false, any_review: false, agent_review: null, ...over });
  const tally = (...prs) => prs.reduce((g, p) => (addPr(g, p), g), emptyGroup("r"));
  const none = tally(pr(), pr());
  assert.equal(claudeReviewLine(none), "Claude Code review not recorded yet");
  assert.equal(none.unreviewed, 2);
  assert.equal(claudeReviewLine(emptyGroup("r")), "Claude Code review not recorded yet");
  const mix = tally(pr({ agent_review: true, any_review: true }), pr({ agent_review: false }), pr(), pr());
  assert.equal(claudeReviewLine(mix), "1 of 4 merged had a Claude Code review, 3 not recorded");
  // An open PR's value says nothing about merged ones.
  const open = tally(pr({ merged_at: null, agent_review: true }), pr());
  assert.equal(claudeReviewLine(open), "Claude Code review not recorded yet");
  assert.equal(claudeReviewLine(tally(pr({ merged_at: null, agent_review: true }), pr({ agent_review: true }))), "1 of 1 merged had a Claude Code review, 0 not recorded");
  assert.equal(claudeReviewLine(tally(pr({ agent_review: false }), pr({ agent_review: false }))), "0 of 2 merged had a Claude Code review, 2 not recorded");
  const person = tally(pr({ human_review: true, any_review: true }));
  assert.equal(person.humanReviewed, 1);
  assert.equal(claudeReviewLine(person), "Claude Code review not recorded yet");
});

const mach = (over) => ({ developer: "A", first_ist_day: "2026-10-10", last_seen: "2026-11-01T06:00:00Z", ...over });
const logRow = (day, person = "A") => ({ person, day, seconds: 3600 });

test("reporting window: earliest start, latest last_seen as an IST day", () => {
  assert.deepEqual(reportingWindow([mach({}), mach({ first_ist_day: "2026-10-05", last_seen: "2026-11-03T20:00:00Z" })], "A"), { from: "2026-10-05", until: "2026-11-04" });
  assert.deepEqual(reportingWindow([mach({ first_ist_day: null }), mach({ first_ist_day: "2026-10-12" })], "A").from, "2026-10-12");
  assert.deepEqual(reportingWindow([mach({ developer: "B", first_ist_day: "2026-09-01" }), mach({})], "A").from, "2026-10-10");
});

test("reporting window is null with no laptops or no start day", () => {
  assert.equal(reportingWindow([], "A"), null);
  assert.equal(reportingWindow([mach({ first_ist_day: null }), mach({ developer: "B" })], "A"), null);
});

test("hours in window: inclusive ends, outside and other people left out, null without a window", () => {
  const w = { from: "2026-10-10", until: "2026-11-01" };
  const log = ["2026-10-09", "2026-10-10", "2026-11-01", "2026-11-02"].map((d) => logRow(d)).concat(logRow("2026-10-20", "B"));
  assert.equal(hoursInWindow(log, "A", w), 2);
  assert.equal(hoursInWindow(log, "A", null), null);
});

test("tokens per reported hour limits tokens and hours to the same days", () => {
  const machines = [mach({})]; // 2026-10-10 to 2026-11-01
  const u = (day, tokens, developer = "A", output = 0) => ({ developer, day, input: tokens, output });
  const usage = [u("2026-10-09", 4000000), u("2026-10-10", 300000, "A", 200000), u("2026-11-01", 500000), u("2026-11-02", 9e6), u("2026-10-20", 9e6, "B")];
  const log = [{ person: "A", day: "2026-10-10", seconds: 16 * 3600 }, logRow("2026-10-01")];
  const none = { model: "", tool: "" };
  assert.equal(tokensPerReportedHour(usage, log, machines, "A", none), 62500);
  assert.equal(tokensPerReportedHour([u("2026-10-09", 5), u("2026-11-02", 5)], log, machines, "A", none), null);
  assert.equal(tokensPerReportedHour(usage, [logRow("2026-10-01")], machines, "A", none), null);
  assert.equal(tokensPerReportedHour(usage, log, [mach({ first_ist_day: null })], "A", none), null);
  assert.equal(tokensPerReportedHour(usage, log, machines, "A", { model: "m", tool: "" }), null);
  assert.equal(tokensPerReportedHour(usage, log, machines, "A", { model: "", tool: "claude" }), null);
});
