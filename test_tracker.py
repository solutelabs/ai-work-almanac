import unittest
from unittest import mock

import tracker
from tracker import aggregate, commit_rows, fetch_branch_commits, time_rows, dashboard_identities, dashboard_rows, ist_day, parse_api_commits, parse_recent_commits, parse_log, parse_prs, person_key, render


def record(name="Asha", email="asha@example.com", body="fix: thing", numstat="3\t1\tsrc/a.py\n"):
    return f"\x1eabc\x1f{name}\x1f{email}\x1f2026-10-01\x1f{body}\x1f\n\n{numstat}"


class ParseLog(unittest.TestCase):
    def one(self, **kw):
        return parse_log(record(**kw), "repo")[0]

    def test_counts_lines(self):
        c = self.one()
        self.assertEqual((c["added"], c["deleted"], c["claude"]), (3, 1, False))

    def test_skips_binary_and_generated_files(self):
        c = self.one(numstat="-\t-\tlogo.png\n500\t0\tpackage-lock.json\n9\t9\tdist/a.js\n2\t0\tsrc/a.py\n")
        self.assertEqual((c["added"], c["deleted"]), (2, 0))

    def test_detects_claude_trailers(self):
        for body in ("x\n\nCo-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>",
                     "x\n\nco-authored-by: Claude Sonnet 5.5 <noreply@anthropic.com>",
                     "x\n\n* item\n  Co-authored-by: claude[bot] <1+claude[bot]@users.noreply.github.com>",
                     "x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"):
            self.assertTrue(self.one(body=body)["claude"], body)

    def test_ignores_prose_and_a_colleague_named_claude(self):
        for body in ("docs(claude-md): update the CLAUDE.md notes",
                     "x\n\nCo-Authored-By: Claude Martin <claude.martin@example.com>"):
            self.assertFalse(self.one(body=body)["claude"], body)

    def test_claude_as_author_counts(self):
        self.assertTrue(self.one(name="claude[bot]", email="1+claude[bot]@users.noreply.github.com")["claude"])

    def test_skips_other_bots(self):
        self.assertEqual(parse_log(record(name="dependabot[bot]"), "repo"), [])


    def test_bulk_commits_count_as_commits_but_not_lines(self):
        c = self.one(numstat="20001\t5\tschema.json\n")
        self.assertEqual((c["added"], c["deleted"], c["bulk"]), (0, 0, 20001))
        self.assertEqual(self.one(numstat="20000\t0\ta.py\n")["added"], 20000)

    def test_skips_design_files(self):
        self.assertEqual(self.one(numstat="88269\t0\tdocs/question.pen\n")["added"], 0)


class Identity(unittest.TestCase):
    def test_aliases_merge_identities(self):
        aliases = {"asha@example.com": "Asha Rao", "asha": "Asha Rao"}
        text = record() + record(name="asha-sl", email="9+asha@users.noreply.github.com")
        rows = aggregate(parse_log(text, "r", aliases), lambda c: c["person"])
        self.assertEqual(list(rows), ["Asha Rao"])
        self.assertEqual(rows["Asha Rao"]["commits"], 2)

    def test_noreply_forms_are_one_person(self):
        self.assertEqual(person_key("A", "12+asha@users.noreply.github.com"), "asha")
        self.assertEqual(person_key("A", "asha@users.noreply.github.com"), "asha")
        self.assertEqual(person_key("A", "Asha@Example.com"), "asha@example.com")


class Report(unittest.TestCase):
    def test_aggregates_claude_share(self):
        commits = parse_log(record() + record(body="x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"), "r")
        row = aggregate(commits, lambda c: c["repo"])["r"]
        self.assertEqual((row["commits"], row["claude_commits"], row["added"], row["claude_added"]), (2, 1, 6, 3))

    def test_a_commit_late_on_the_last_utc_day_of_a_month_counts_in_the_next_ist_month(self):
        self.assertEqual(ist_day("2026-10-31T18:30:00Z"), "2026-11-01")
        self.assertEqual(ist_day("2026-10-31T18:29:00Z"), "2026-10-31")

    def test_dashboard_rows_group_by_month_repo_and_person(self):
        commits = parse_log(record() + record(body="x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"), "example-app")
        self.assertEqual(dashboard_rows(commits), [{
            "month": "2026-10", "repo": "your-org/example-app", "person": "Asha", "commits": 2, "claude_commits": 1,
            "lines_added": 6, "lines_removed": 2, "claude_lines_added": 3}])

    def test_identities_map_every_git_identity_to_one_display_name(self):
        aliases = {"asha@example.com": "Asha Rao", "asha-sl": "Asha Rao"}
        commits = parse_log(record() + record(name="x", email="9+asha-sl@users.noreply.github.com"), "r", aliases)
        self.assertEqual(dashboard_identities(commits), [
            {"key": "asha-sl", "person": "Asha Rao"}, {"key": "asha@example.com", "person": "Asha Rao"}])

    def test_escapes_author_names(self):
        out = render(parse_log(record(name="<script>x</script>"), "r"), {}, "")
        self.assertNotIn("<script>", out)


def api_commit(name="Asha", email="asha@example.com", message="fix: thing", parents=1):
    return {"oid": "abc", "message": message, "authoredDate": "2026-10-01T10:00:00Z",
            "parents": {"totalCount": parents}, "author": {"name": name, "email": email}}


class TimeLog(unittest.TestCase):
    MEMBERS = [{"user_id": "u1", "name": "Asha R", "email": "Asha@example.com", "billable_rate": 99999},
               {"user_id": "u2", "name": "Meera", "email": "meera@example.com", "billable_rate": 99999}]
    PROJECTS = [{"id": "p1", "name": "example-app", "billable_rate": 88888, "client_id": "c1"}]

    def rows(self, *entries, repos=None):
        return time_rows(list(entries), self.MEMBERS, self.PROJECTS, {"asha@example.com": "Asha Rao"}, {"Asha Rao"}, repos or {}, "2026-10-01")

    def entry(self, **over):
        return {"user_id": "u1", "project_id": "p1", "start": "2026-10-02T10:00:00Z", "end": "2026-10-02T11:00:00Z", "duration": 3600,
                "description": "secret client work", "type": "work", "billable": True, **over}

    def test_adds_up_by_ist_day_and_stores_only_known_fields(self):
        out = self.rows(self.entry(), self.entry(duration=1800), repos={"example-app": ["your-org/example-app"]})
        self.assertEqual(out, [{"day": "2026-10-02", "person": "Asha Rao", "project": "example-app", "seconds": 5400, "repos": ["your-org/example-app"]}])

    def test_rates_and_descriptions_never_get_through(self):
        sent = str(self.rows(self.entry()))
        for secret in ("99999", "88888", "secret client", "billable", "c1"):
            self.assertNotIn(secret, sent)

    def test_uses_ist_days_and_skips_running_timers_other_types_and_people_outside_the_dashboard(self):
        self.assertEqual(self.rows(self.entry(start="2026-10-02T18:30:00Z"))[0]["day"], "2026-10-03")
        self.assertEqual(self.rows(self.entry(end=None)), [])
        self.assertEqual(self.rows(self.entry(type="break")), [])
        self.assertEqual(self.rows(self.entry(user_id="u2")), [])
        self.assertEqual(self.rows(self.entry(start="2026-09-30T00:00:00Z")), [])

    def test_entries_with_no_project_still_count(self):
        self.assertEqual(self.rows(self.entry(project_id=None))[0]["project"], "No project")


class RecentCommits(unittest.TestCase):
    def rows(self, **over):
        commits = parse_recent_commits([{**api_commit(), "additions": 7, "deletions": 2, **over}], "example-app", {})
        return commit_rows(commits, commits)

    def test_keeps_exact_time_and_lines_but_never_the_message(self):
        row = self.rows(message="secret plan", authoredDate="2026-10-01T15:30:00+05:30")[0]
        self.assertEqual(row, {"repo": "your-org/example-app", "oid": "abc", "person": "Asha", "authored_at": "2026-10-01T10:00:00Z",
                               "ai": False, "lines_added": 7, "lines_removed": 2, "on_default": True, "branch": None})

    def test_recognises_other_ai_tools_as_well_as_claude(self):
        for message in ("x\n\nCo-authored-by: Claude <noreply@anthropic.com>", "x\n\nCo-authored-by: Cursor <cursoragent@cursor.com>",
                        "x\n\nCo-Authored-By: Codex <codex@openai.com>", "x\n\nCo-authored-by: Copilot <x@users.noreply.github.com>"):
            self.assertTrue(self.rows(message=message)[0]["ai"], message)
        self.assertFalse(self.rows(message="x\n\nCo-authored-by: Asha <asha@example.com>")[0]["ai"])

    def test_skips_merges_and_leaves_bulk_imports_out_of_lines(self):
        self.assertEqual(self.rows(parents={"totalCount": 2}), [])
        self.assertEqual(self.rows(additions=30000)[0]["lines_added"], 0)


class BranchCommits(unittest.TestCase):
    SINCE = "2026-10-01T00:00:00Z"

    def scan(self, branches, histories, known=()):
        """Stands in for GitHub: branches is [(name, last commit date)], histories is {branch: [pages of oids]}."""
        def github(_path, body):
            variables = body["variables"]
            if "ref" not in variables:
                nodes = [{"name": n, "target": {"committedDate": d}} for n, d in branches]
                return {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "refs": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}
            branch = variables["ref"].removeprefix("refs/heads/")
            if branch not in histories:
                return {"data": {"repository": {"ref": None}}}
            page = int(variables["after"] or 0)
            pages = histories[branch]
            nodes = [{**api_commit(), "oid": o, "additions": 1, "deletions": 0} for o in pages[page]]
            return {"data": {"repository": {"ref": {"target": {"history": {"pageInfo": {"hasNextPage": page + 1 < len(pages), "endCursor": str(page + 1)}, "nodes": nodes}}}}}}
        with mock.patch.object(tracker, "github", github):
            return [c["sha"] for c in fetch_branch_commits("example-app", self.SINCE, {}, known)]

    def test_finds_branch_work_once_and_leaves_out_the_default_branch(self):
        found = self.scan([("main", "2026-11-01"), ("a", "2026-11-01"), ("b", "2026-11-01")],
                          {"main": [["never"]], "a": [["one", "shared", "merged"]], "b": [["two", "shared"]]}, known={"merged"})
        self.assertEqual(sorted(found), ["one", "shared", "two"])

    def test_skips_quiet_branches_and_copes_with_a_deleted_one(self):
        self.assertEqual(self.scan([("old", "2026-01-01"), ("gone", "2026-11-01")], {"old": [["stale"]]}), [])

    def test_stops_at_a_page_with_nothing_new(self):
        calls = {"x": [["merged"], ["never-read"]]}
        self.assertEqual(self.scan([("x", "2026-11-01")], calls, known={"merged"}), [])

    def test_reads_server_branches_first_and_names_each_commit_branch(self):
        branches = [("feat", "2026-11-01"), ("develop", "2026-11-01")]
        histories = {"feat": [["shared", "own"]], "develop": [["shared"]]}
        self.assertEqual(self.scan(branches, histories), ["shared", "own"])

    def test_skips_a_merged_branch_unless_it_is_a_server_branch(self):
        def github(_path, body):
            nodes = [{"name": n, "target": {"oid": "merged-head", "committedDate": "2026-11-01"}} for n in ("feat", "develop", "other")]
            nodes[2]["target"]["oid"] = "still-open"
            return {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "refs": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}
        with mock.patch.object(tracker, "github", github):
            self.assertEqual(tracker.active_branches("example-app", self.SINCE, {"merged-head"}), ["develop", "other"])

    def test_marks_rows_as_off_the_default_branch(self):
        commits = parse_recent_commits([{**api_commit(), "additions": 1, "deletions": 0}], "example-app", {})
        self.assertFalse(commit_rows(commits, commits, on_default=False)[0]["on_default"])


class ApiCommits(unittest.TestCase):
    def test_matches_what_parse_log_builds_from_a_clone(self):
        self.assertEqual(parse_api_commits([api_commit()], "r", {}), parse_log(record(numstat=""), "r", {}))

    def test_detects_claude_and_skips_merges_and_other_bots(self):
        commits = parse_api_commits([api_commit(message="x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"),
                                     api_commit(parents=2), api_commit(name="dependabot[bot]")], "r", {})
        self.assertEqual([c["claude"] for c in commits], [True])

    def test_tolerates_a_missing_author(self):
        self.assertEqual(len(parse_api_commits([dict(api_commit(), author=None)], "r", {})), 1)


def pr(author="Asha-SL", approvers=(), merged="2026-10-02T00:00:00Z", draft=False, kind="User"):
    return {"number": 1, "createdAt": "2026-10-01T00:00:00Z", "mergedAt": merged, "isDraft": draft,
            "author": {"__typename": kind, "login": author},
            "reviews": {"nodes": [{"author": {"__typename": t, "login": a}, "body": (b or [None])[0]} for a, t, *b in approvers]}}


class PullRequests(unittest.TestCase):
    def one(self, *args, **kw):
        return parse_prs([pr(*args, **kw)], "example-app", {"asha-sl": "Asha Rao"})

    def test_uses_the_display_name_and_current_repo_form(self):
        row = self.one()[0]
        self.assertEqual((row["person"], row["repo"], row["number"]), ("Asha Rao", "your-org/example-app", 1))

    def test_agent_review_is_not_recorded_yet(self):
        self.assertIsNone(self.one()[0]["agent_review"])

    def test_separates_human_review_from_any_review(self):
        def reviewed(*approvers):
            row = self.one(approvers=approvers)[0]
            return row["human_review"], row["any_review"]
        self.assertEqual(reviewed(), (False, False))
        self.assertEqual(reviewed(("asha-sl", "User")), (False, False))
        self.assertEqual(reviewed(("claude", "Bot")), (False, True))
        self.assertEqual(reviewed(("claude", "Bot"), ("ravi", "User")), (True, True))

    def test_ignores_greptile_trial_notices_only(self):
        def reviewed(*approvers):
            row = self.one(approvers=approvers)[0]
            return row["human_review"], row["any_review"]
        notice = ("greptile-apps", "Bot", "Your Greptile Trial has ended.")
        self.assertEqual(reviewed(notice), (False, False))
        self.assertEqual(reviewed(("Greptile-Apps", "Bot", "TRIAL HAS ENDED")), (False, False))
        self.assertEqual(reviewed(("greptile-apps", "Bot", "")), (False, True))
        self.assertEqual(reviewed(("greptile-apps", "Bot")), (False, True))  # no body field at all, not a notice
        self.assertEqual(reviewed(("greptile-apps", "Bot", "Looks good, no issues found.")), (False, True))  # real review text
        self.assertEqual(reviewed(("ravi", "User", "trial has ended")), (True, True))
        self.assertEqual(reviewed(notice, ("claude", "Bot")), (False, True))
        self.assertEqual(reviewed(notice, ("ravi", "User")), (True, True))

    def test_skips_drafts_bots_and_deleted_authors(self):
        self.assertEqual(self.one(draft=True), [])
        self.assertEqual(self.one("dependabot", kind="Bot"), [])
        self.assertEqual(parse_prs([dict(pr(), author=None)], "example-app", {}), [])

    def test_open_prs_keep_a_null_merge_date(self):
        self.assertIsNone(self.one(merged=None)[0]["merged_at"])


if __name__ == "__main__":
    unittest.main()
