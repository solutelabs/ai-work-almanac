import collections
import json
import os
import pathlib
import tempfile
import time
import unittest

import collect

SECRET = "SECRET-prompt-and-code"
TS = "2026-10-01T10:00:00Z"


def lookup_all(known=None):
    return collect.RepoLookup(known if known is not None else {"/w": "your-org/app"})


def write_log(records):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for record in records:
        handle.write(record if isinstance(record, str) else json.dumps(record))
        handle.write("\n")
    handle.close()
    return pathlib.Path(handle.name)


def reply(message_id="m1", output=10, model="claude-opus-5-5"):
    return {"type": "assistant", "sessionId": "s1", "cwd": "/w", "timestamp": TS,
            "message": {"id": message_id, "model": model, "content": SECRET,
                        "usage": {"input_tokens": 5, "output_tokens": output, "cache_read_input_tokens": 7,
                                  "cache_creation_input_tokens": 2}}}


def edit(result):
    return {"type": "user", "sessionId": "s1", "cwd": "/w", "timestamp": TS, "toolUseResult": result}


class Claude(unittest.TestCase):
    def rows(self, records, lookup=None):
        stats = collections.Counter()
        return collect.claude_rows(write_log(records), lookup or lookup_all(), stats), stats

    def test_counts_streamed_replies_once_using_the_last(self):
        rows, _ = self.rows([reply(output=1), reply(output=10), reply("m2", output=3)])
        self.assertEqual(rows[0]["output_tokens"], 13)
        self.assertEqual((rows[0]["input_tokens"], rows[0]["cache_read_tokens"], rows[0]["cache_write_tokens"]),
                         (10, 14, 4))

    def test_counts_edits_and_created_files(self):
        patch = {"filePath": "/w/a.py", "structuredPatch": [{"lines": ["+a", "+b", "-c", " d"]}]}
        create = {"filePath": "/w/b.py", "type": "create", "content": "1\n2\n3\n", "structuredPatch": []}
        rows, _ = self.rows([reply(), edit(patch), edit(create), edit({"filePath": "/w/c.py", "type": "text"})])
        self.assertEqual((rows[0]["lines_added"], rows[0]["lines_removed"]), (5, 1))

    def test_never_sends_content(self):
        result = {"filePath": f"/w/{SECRET}.py", "type": "create", "content": SECRET + "\n", "oldString": SECRET}
        rows, _ = self.rows([{"type": "last-prompt", "lastPrompt": SECRET, "sessionId": "s1"}, reply(), edit(result)])
        sent = json.dumps(collect.payload_for(rows, collections.Counter()))
        self.assertNotIn(SECRET, sent)
        self.assertNotIn("/w", sent)
        self.assertEqual(set(rows[0]), set(collect.ROW_FIELDS))

    def test_reports_other_orgs_by_name(self):
        rows, _ = self.rows([reply()], lookup_all({"/w": "someone/personal"}))
        self.assertEqual(rows[0]["repo"], "someone/personal")

    def test_sessions_with_no_repo_still_count(self):
        rows, stats = self.rows([dict(reply(), cwd="/no/such/folder")], lookup_all({}))
        self.assertEqual((rows[0]["repo"], rows[0]["output_tokens"]), (collect.NO_REPO, 10))
        self.assertEqual(stats["skipped_unresolved"], 1)

    def test_tolerates_bad_lines(self):
        rows, stats = self.rows(["{not json", reply()])
        self.assertEqual(stats["bad_lines"], 1)
        self.assertEqual(len(rows), 1)

    def test_marks_active_blocks_and_only_prompts_a_person_typed(self):
        def said(minute, text, **extra):
            return {"type": "user", "sessionId": "s1", "cwd": "/w", "timestamp": f"2026-10-01T10:{minute:02}:00Z", "message": {"content": text}, **extra}
        rows, _ = self.rows([
            said(1, "fix the bug"),
            said(12, [{"type": "text", "text": "and the test"}]),
            said(22, "<task-notification>done</task-notification>"),
            said(23, "[Request interrupted by user]"),
            said(24, "<system-reminder>x</system-reminder>"),
            said(26, "subagent brief", isSidechain=True),
            said(31, "skill text", isMeta=True),
            said(36, [{"type": "tool_result", "content": "x"}]),
            dict(reply(), timestamp="2026-10-01T10:41:00Z"),
        ])
        slot = lambda minute: 1 << (186 + minute // 5)  # 10:00 UTC is 15:30 IST
        self.assertEqual(rows[0]["prompts"], 2)
        self.assertEqual(int(rows[0]["prompted_slots"], 16), slot(1) | slot(12))
        self.assertEqual(int(rows[0]["active_slots"], 16), sum(slot(m) for m in (1, 12, 22, 26, 31, 36, 41)))
        self.assertEqual(len(rows), 1)

    def test_activity_goes_on_one_row_and_never_creates_one(self):
        rows, stats = self.rows([
            reply(), reply("m2", model="claude-sonnet-5-5"),
            {"type": "user", "cwd": "/w", "timestamp": "2026-09-30T10:00:00Z", "message": {"content": "no reply came"}},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(bool(r["active_slots"]) for r in rows), 1)
        self.assertEqual(sum(r["prompts"] for r in rows), 0)

    def test_an_unknown_folder_is_only_counted_once(self):
        _, stats = self.rows([dict(reply(), cwd="/no/such/folder")], lookup_all({}))
        self.assertEqual(stats["skipped_unresolved"], 1)

    def test_folders_are_hashed_traced_to_their_root_and_worktrees_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone, tree = pathlib.Path(tmp, "clone"), pathlib.Path(tmp, "tree")
            (clone / ".git").mkdir(parents=True)
            (clone / "src").mkdir()
            tree.mkdir()
            (tree / ".git").write_text("gitdir: elsewhere")
            self.assertEqual(collect.folder_id(str(clone / "src")), collect.folder_id(str(clone)))
            self.assertEqual(collect.folder_id(str(clone))[0], "m")
            self.assertEqual(collect.folder_id(str(tree))[0], "w")
        for gone in ("/gone/app/.claude/worktrees/x", "C:\\gone\\.treehouse\\app-1", "/gone/.codex/worktrees/y/app"):
            self.assertEqual(collect.folder_id(gone)[0], "w", gone)
        self.assertEqual(collect.folder_id("/gone/app/.claude/worktrees/x/sub"), collect.folder_id("/gone/app/.claude/worktrees/x"))
        self.assertEqual(collect.folder_id("/gone/app")[0], "m")

    def test_a_row_lists_each_folder_once_and_never_a_path(self):
        both = lookup_all({"/w": "your-org/app", "/gone/.treehouse/x": "your-org/app"})
        rows, _ = self.rows([reply(), dict(reply("m2"), cwd="/gone/.treehouse/x"), reply("m3")], both)
        self.assertEqual(rows[0]["folders"].split(","), sorted([collect.folder_id("/gone/.treehouse/x"), collect.folder_id("/w")]))
        self.assertNotIn("treehouse", json.dumps(rows))

    def test_sends_the_latest_tool_version_and_drops_odd_ones(self):
        rows, _ = self.rows([dict(reply(), version="2.1.300"), dict(reply("m2"), version="2.1.316")])
        self.assertEqual(rows[0]["tool_version"], "2.1.316")
        rows, _ = self.rows([dict(reply(), version=f"/Users/{SECRET}")])
        self.assertIsNone(rows[0]["tool_version"])

    def test_days_and_blocks_are_in_ist(self):
        rows, _ = self.rows([dict(reply(), timestamp="2026-10-01T18:29:59.123Z"), dict(reply("m2"), timestamp="2026-10-01T18:30:00Z")])
        self.assertEqual([r["day"] for r in rows], ["2026-10-01", "2026-10-02"])
        self.assertEqual([int(r["active_slots"], 16) for r in rows], [1 << 287, 1])
        self.assertTrue(all(r["ist"] is True for r in rows))
        self.assertEqual(collect.ist_day("2026-13-40T99:00:00Z"), "")

    def test_bad_timestamps_mark_nothing(self):
        rows, _ = self.rows([reply(), dict(reply("m2"), timestamp="soon")])
        self.assertEqual(rows[0]["active_slots"], self.rows([reply()])[0][0]["active_slots"])

    def test_flags_a_changed_format_instead_of_reporting_zeros(self):
        _, stats = self.rows([{"type": "assistant", "cwd": "/w", "timestamp": TS, "message": {"renamed": 1}}])
        self.assertEqual(stats["unrecognized_files"], 1)


def token_event(input_tokens, cached, output):
    return {"type": "event_msg", "timestamp": TS, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output},
        "last_token_usage": {"input_tokens": 999999}}}}


class Codex(unittest.TestCase):
    def rows(self, records):
        stats = collections.Counter()
        return collect.codex_rows(write_log(records), lookup_all(), stats), stats

    def test_sums_running_totals_so_repeated_events_count_once(self):
        rows, _ = self.rows([
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/w", "base_instructions": SECRET}},
            {"type": "turn_context", "payload": {"cwd": "/w", "model": "gpt-5.6-sol"}},
            {"type": "event_msg", "timestamp": TS, "payload": {"type": "token_count", "info": None}},
            token_event(100, 60, 9),
            token_event(100, 60, 9),
            token_event(200, 120, 18),
        ])
        row = rows[0]
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]), (80, 120, 18))
        self.assertEqual((row["lines_added"], row["lines_removed"], row["model"]), (None, None, "gpt-5.6-sol"))
        self.assertNotIn(SECRET, json.dumps(rows))

    def test_marks_activity_and_counts_real_user_messages(self):
        rows, _ = self.rows([
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"cwd": "/w", "model": "gpt"}},
            {"type": "response_item", "timestamp": "2026-10-01T10:01:00Z", "payload": {"type": "message", "role": "user", "content": []}},
            {"type": "event_msg", "timestamp": "2026-10-01T10:01:00Z", "payload": {"type": "user_message", "message": SECRET}},
            dict(token_event(100, 0, 9), timestamp="2026-10-01T10:06:00Z"),
        ])
        slot = lambda minute: 1 << (186 + minute // 5)
        self.assertEqual((rows[0]["prompts"], int(rows[0]["prompted_slots"], 16)), (1, slot(1)))
        self.assertEqual(self.rows([
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/w", "cli_version": "0.162.0-alpha.17.2"}},
            {"type": "turn_context", "payload": {"cwd": "/w", "model": "gpt"}},
            token_event(100, 0, 9),
        ])[0][0]["tool_version"], "0.162.0-alpha.17.2")
        self.assertEqual(int(rows[0]["active_slots"], 16), slot(1) | slot(6))
        self.assertNotIn(SECRET, json.dumps(rows))

    def test_flags_a_changed_format(self):
        _, stats = self.rows([{"type": "turn_context", "payload": {"cwd": "/w", "model": "x"}}])
        self.assertEqual(stats["unrecognized_files"], 1)


class DeletedFolders(unittest.TestCase):
    def lookup(self):
        return collect.RepoLookup({}, ["your-org/ledger", "your-org/Example-App"])

    def test_matches_worktree_paths_to_org_repos(self):
        for cwd in ("/gone/.treehouse/ledger-159b41", "/gone/Github/ledger", "/gone/ledger/.claude/worktrees/x", "/gone/EXAMPLE-APP"):
            self.assertIn(self.lookup().match_deleted_folder(cwd), ("your-org/ledger", "your-org/example-app"), cwd)

    def test_leaves_unknown_and_existing_folders_alone(self):
        self.assertIsNone(self.lookup().match_deleted_folder("/gone/favourite-auto-shopify-theme"))
        self.assertIsNone(self.lookup().match_deleted_folder("/gone/ledger-extra"))
        with tempfile.TemporaryDirectory() as tmp:
            existing = pathlib.Path(tmp) / "ledger"
            existing.mkdir()
            self.assertIsNone(self.lookup().match_deleted_folder(str(existing)))

    def test_a_deleted_worktree_produces_rows(self):
        stats = collections.Counter()
        record = dict(reply(), cwd="/gone/.treehouse/ledger-159b41")
        rows = collect.claude_rows(write_log([record]), self.lookup(), stats)
        self.assertEqual(rows[0]["repo"], "your-org/ledger")
        self.assertEqual(stats["skipped_unresolved"], 0)


class CodexShape(unittest.TestCase):
    def test_reports_names_and_never_content(self):
        log = write_log([
            {"type": "session_meta", "payload": {"cli_version": "0.1", "cwd": f"/w/{SECRET}"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "arguments": SECRET}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": SECRET}},
            {"type": "event_msg", "payload": {"type": "patch_apply_end", "stdout": SECRET,
                                              "changes": {f"/w/{SECRET}.py": {"unified_diff": SECRET, "add": SECRET}}}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": SECRET}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "FileChange", "id": SECRET, "changes": [{"path": SECRET, "diff": SECRET}]}}},
        ])
        tools, events, fields, versions = collect.codex_shape([log], collections.Counter())
        self.assertEqual(tools, {"exec": 1, "apply_patch": 1})
        self.assertEqual(events, {"patch_apply_end": 1, "agent_message": 1, "item_completed": 1, "item:FileChange": 1})
        self.assertEqual(fields["item:FileChange.changes"], {"path", "diff"})
        self.assertEqual(fields["patch_apply_end.changes"], {"unified_diff", "add"})
        self.assertEqual(versions, {"0.1": 1})
        self.assertNotIn(SECRET, repr((tools, events, dict(fields), versions)))


class SameRunMatching(unittest.TestCase):
    def test_a_deleted_folder_matches_a_live_repo_found_later_in_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "projects"
            root.mkdir()
            live = pathlib.Path(tmp) / "client" / "shop-theme"
            live.mkdir(parents=True)
            collect.subprocess.run(["git", "-C", str(live), "init", "-q"], check=True)
            collect.subprocess.run(["git", "-C", str(live), "remote", "add", "origin", "git@github.com:client/shop-theme.git"], check=True)
            (root / "a.jsonl").write_text(json.dumps(dict(reply("m1"), cwd=f"{tmp}/old/shop-theme")) + "\n")
            (root / "b.jsonl").write_text(json.dumps(dict(reply("m2"), cwd=str(live))) + "\n")
            old = collect.log_sources
            collect.log_sources = lambda: [(root, collect.claude_rows)]
            try:
                rows, _, stats = collect.collect({})
            finally:
                collect.log_sources = old
            self.assertEqual({r["repo"] for r in rows}, {"client/shop-theme"})
            self.assertEqual(stats["skipped_unresolved"], 0)
            self.assertEqual(stats["files_parsed"], 2)


class ParserUpgrade(unittest.TestCase):
    def test_a_new_parser_version_rereads_every_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = pathlib.Path(tmp) / "p"
            projects.mkdir()
            log = projects / "a.jsonl"
            log.write_text(json.dumps(reply()) + "\n")
            info = log.stat()
            state = {"files": {str(log): [info.st_mtime_ns, info.st_size]}, "repos": {"/w": "your-org/app"}}
            old = collect.log_sources
            collect.log_sources = lambda: [(projects, collect.claude_rows)]
            try:
                self.assertEqual(len(collect.collect(state)[0]), 1)
                self.assertEqual(state["parser"], collect.PARSER_VERSION)
            finally:
                collect.log_sources = old


class Harness(unittest.TestCase):
    """Runs real git for the hooks, because that's the part that varies."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.claude = self.home / ".claude"
        (self.claude / "plugins").mkdir(parents=True)
        self.repo = self.home / "client"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:your-org/app.git")
        self.known = {str(self.repo): "your-org/app"}

    def git(self, *args):
        return collect.subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def install(self, **entry):
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {collect.HARNESS_PLUGIN: [{"scope": "user", "version": "2.4.0", **entry}]}}))

    def settings(self, path, enabled):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"enabledPlugins": {collect.HARNESS_PLUGIN: enabled}}))

    def hook(self, text, name="pre-commit", mode=0o755):
        folder = self.repo / ".githooks"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text)
        (folder / name).chmod(mode)
        self.git("config", "core.hooksPath", ".githooks")

    def row(self):
        rows = collect.harness_rows(self.known, [], self.claude)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_plugin_needs_installing_and_enabling(self):
        self.assertIsNone(self.row()["plugin_version"])
        self.install()
        self.assertFalse(self.row()["plugin"])
        self.settings(self.claude / "settings.json", True)
        self.assertEqual(self.row(), {"repo": "your-org/app", "plugin": True, "plugin_version": "2.4.0", "hooks": False})

    def test_settings_closest_to_the_repo_win(self):
        self.install()
        self.settings(self.claude / "settings.json", True)
        self.settings(self.repo / ".claude" / "settings.json", False)
        self.assertFalse(self.row()["plugin"])
        self.settings(self.repo / ".claude" / "settings.local.json", True)
        self.assertTrue(self.row()["plugin"])

    def test_project_install_only_counts_in_that_project(self):
        self.settings(self.claude / "settings.json", True)
        self.install(scope="project", projectPath="/somewhere/else")
        self.assertFalse(self.row()["plugin"])
        self.install(scope="project", projectPath=str(self.repo))
        self.assertTrue(self.row()["plugin"])

    def test_damaged_plugin_files_read_as_off(self):
        for text in ("{not json", "[]", '{"plugins": 5}', json.dumps({"plugins": {collect.HARNESS_PLUGIN: [None, 5]}})):
            (self.claude / "plugins" / "installed_plugins.json").write_text(text)
            self.assertFalse(self.row()["plugin"])
        self.install(version="not a version!")
        self.settings(self.claude / "settings.json", True)
        self.assertEqual(self.row()["plugin_version"], "unknown")

    def test_only_the_harness_scanner_counts_as_hooks(self):
        self.assertFalse(self.row()["hooks"])
        self.hook("#!/bin/sh\nexec python3 scripts/check_no_data.py\n")
        self.assertFalse(self.row()["hooks"])
        self.hook(f"#!/bin/bash\n# Pre-commit hook: {collect.HARNESS_HOOK_MARK}\n")
        self.assertTrue(self.row()["hooks"])

    def test_scanner_called_from_a_composed_hook_counts(self):
        self.hook("#!/bin/bash\n.githooks/secret-scanner\nprettier\n")
        self.assertFalse(self.row()["hooks"])
        self.hook(f"# {collect.HARNESS_HOOK_MARK}\n", name="secret-scanner")
        self.assertTrue(self.row()["hooks"])

    @unittest.skipIf(os.name == "nt", "Windows has no executable flag, and git runs the hook anyway")
    def test_hook_git_cant_run_does_not_count(self):
        self.hook(f"# {collect.HARNESS_HOOK_MARK}\n", mode=0o644)
        self.assertFalse(self.row()["hooks"])

    def test_default_hooks_folder_counts(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"# {collect.HARNESS_HOOK_MARK}\n")
        hook.chmod(0o755)
        self.assertTrue(self.row()["hooks"])

    def test_a_yes_from_any_clone_counts(self):
        other = self.home / "client-2"
        other.mkdir()
        collect.subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        collect.subprocess.run(["git", "-C", str(other), "remote", "add", "origin", "git@github.com:your-org/app.git"], check=True)
        self.hook(f"# {collect.HARNESS_HOOK_MARK}\n")
        self.assertTrue(self.row()["hooks"])

    def test_folders_never_worked_in_are_not_named(self):
        beside = self.home / "personal"
        beside.mkdir()
        collect.subprocess.run(["git", "-C", str(beside), "init", "-q"], check=True)
        collect.subprocess.run(["git", "-C", str(beside), "remote", "add", "origin", "git@github.com:me/private.git"], check=True)
        self.assertEqual([r["repo"] for r in collect.harness_rows(self.known, [], self.claude)], ["your-org/app"])
        rows = collect.harness_rows(self.known, [{"repo": "me/private"}], self.claude)
        self.assertEqual([r["repo"] for r in rows], ["me/private", "your-org/app"])

    def test_nothing_from_settings_or_hooks_is_sent(self):
        self.install()
        self.settings(self.claude / "settings.json", True)
        self.hook(f"# {collect.HARNESS_HOOK_MARK} SECRET-hook\n")
        self.assertEqual(sorted(self.row()), ["hooks", "plugin", "plugin_version", "repo"])
        self.assertNotIn("SECRET", json.dumps(self.row()))


class LocalCommits(unittest.TestCase):
    """Runs real git, because that's the part that varies."""

    def git(self, cwd, *args, email="asha@acme.io", when="2026-10-30T10:00:00+05:30"):
        env = {**os.environ, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_NAME": "A", "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_NAME": "A",
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return collect.subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    def commit(self, repo, name, lines, message="work", **kwargs):
        pathlib.Path(repo, name).write_text("x\n" * lines)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message, **kwargs)
        return self.git(repo, "rev-parse", "HEAD")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.repo = self.home / "client"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.email", "asha@acme.io")
        self.git(self.repo, "remote", "add", "origin", "git@gitlab.com:acme/secret-app.git")
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400))
        self.when = now
        self.main = self.commit(self.repo, "a.py", 3, "secret plan", when=now)

    def rows(self, known=None):
        return {r["oid"]: r for r in collect.local_commits(known if known is not None else {str(self.repo): "x"})}

    def test_reports_only_known_fields_in_utc_and_no_message_or_path(self):
        row = self.rows()[self.main]
        self.assertEqual(set(row), {"repo", "oid", "authored_at", "ai", "lines_added", "lines_removed", "on_default"})
        self.assertEqual((row["repo"], row["lines_added"], row["ai"], row["on_default"]), ("gitlab.com-acme/secret-app", 3, False, True))
        self.assertTrue(row["authored_at"].endswith("Z"))
        self.assertNotIn("secret plan", json.dumps(row))
        self.assertNotIn(str(self.home), json.dumps(row))

    def test_only_this_persons_commits_including_lookalike_addresses(self):
        other = self.commit(self.repo, "b.py", 1, email="other@acme.io", when=self.when)
        lookalike = self.commit(self.repo, "c.py", 1, email="xasha@acme.io", when=self.when)
        self.assertEqual(set(self.rows()), {self.main})
        self.assertNotIn(other, self.rows())
        self.assertNotIn(lookalike, self.rows())

    def test_branch_commits_are_marked_so_they_cant_double_count(self):
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        branch = self.commit(self.repo, "b.py", 2, when=self.when)
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", self.main)
        rows = self.rows()
        self.assertFalse(rows[branch]["on_default"])
        self.assertTrue(rows[self.main]["on_default"])

    def test_generated_files_and_bulk_imports_stay_out_of_lines(self):
        lock = self.commit(self.repo, "package-lock.json", 500, when=self.when)
        self.assertEqual(self.rows()[lock]["lines_added"], 0)
        bulk = self.commit(self.repo, "dump.sql", collect.COMMIT_BULK_LINES + 1, when=self.when)
        self.assertEqual(self.rows()[bulk]["lines_added"], 0)

    def test_notices_ai_tools_and_skips_merges(self):
        marked = self.commit(self.repo, "b.py", 1, "x\n\nCo-authored-by: Cursor <cursoragent@cursor.com>", when=self.when)
        self.assertTrue(self.rows()[marked]["ai"])
        self.git(self.repo, "checkout", "-q", "-b", "feature", self.main)
        self.commit(self.repo, "c.py", 1, when=self.when)
        self.git(self.repo, "checkout", "-q", "main")
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "feature", when=self.when)
        self.assertNotIn(self.git(self.repo, "rev-parse", "HEAD"), self.rows())

    def test_finds_a_repo_beside_a_known_one_and_inside_a_subfolder(self):
        beside = self.home / "manual-only"
        beside.mkdir()
        self.git(beside, "init", "-q", "-b", "main")
        self.git(beside, "config", "user.email", "asha@acme.io")
        self.git(beside, "remote", "add", "origin", "https://github.com/acme/manual.git")
        oid = self.commit(beside, "m.py", 1, when=self.when)
        (self.repo / "src").mkdir()
        self.assertEqual(self.rows({str(self.repo / "src"): "x"})[oid]["repo"], "acme/manual")

    def test_a_folder_git_cant_read_or_with_no_remote_is_skipped(self):
        local_only = self.home / "local-only"
        local_only.mkdir()
        self.git(local_only, "init", "-q", "-b", "main")
        self.git(local_only, "config", "user.email", "asha@acme.io")
        self.commit(local_only, "l.py", 1, when=self.when)
        (self.home / "broken" / ".git").mkdir(parents=True)
        self.assertEqual(set(self.rows()), {self.main})
        self.assertEqual(collect.local_commits({"/no/such/folder": "x"}), [])

    def test_sends_a_repo_only_when_its_commits_change(self):
        rows = list(self.rows().values())
        first, digests = collect.changed_commits(rows, {})
        self.assertEqual(first, rows)
        self.assertEqual(collect.changed_commits(rows, digests), ([], {}))
        self.assertEqual(collect.changed_commits([{**rows[0], "on_default": False}], digests)[0][0]["on_default"], False)


class Remotes(unittest.TestCase):
    def test_reads_github_remotes_and_drops_credentials(self):
        for url in ("https://github.com/Your-Org/Example-App.git", "git@github.com:your-org/example-app.git",
                    "ssh://git@github.com/your-org/example-app", "https://x:tok@github.com/your-org/example-app/"):
            self.assertEqual(collect.parse_remote(url), "your-org/example-app", url)

    def test_lookalike_hosts_cant_pass_for_a_github_repo(self):
        for url in ("https://evil.com/github.com/your-org/example-app", "https://notgithub.com/your-org/example-app",
                    "https://github.com.evil.com/your-org/example-app", "https://gitlab.com/your-org/example-app"):
            self.assertNotEqual(collect.parse_remote(url), "your-org/example-app", url)

    def test_reads_other_hosts_and_never_keeps_credentials(self):
        for url, expected in (("git@gitlab.com:acme/app.git", "gitlab.com-acme/app"),
                              ("https://user:tok@gitlab.com/acme/team/app.git", "gitlab.com-team/app"),
                              ("git@bitbucket.org:acme/app.git", "bitbucket.org-acme/app"),
                              ("https://acme@dev.azure.com/acme/proj/_git/app", "dev.azure.com-proj/app"),
                              ("git@ssh.dev.azure.com:v3/acme/proj/app", "ssh.dev.azure.com-proj/app"),
                              ("ssh://git@git.acme.io:2222/team/app.git", "git.acme.io-team/app")):
            self.assertEqual(collect.parse_remote(url), expected, url)
            self.assertNotIn("tok", collect.parse_remote(url))

    def test_an_ssh_alias_for_github_counts_as_github(self):
        for url in ("git@github-work:Your-Org/App.git", "git@github.com-work:your-org/app.git", "git@github.com-my-alias:your-org/app"):
            self.assertEqual(collect.parse_remote(url), "your-org/app", url)
        self.assertEqual(collect.parse_remote("git@github.example.com:acme/app.git"), "github.example.com-acme/app")

    def test_local_paths_arent_repos(self):
        for url in ("/local/path", "../sibling.git", "file:///srv/app.git", "C:\\repos\\app", ""):
            self.assertIsNone(collect.parse_remote(url), url)


class Incremental(unittest.TestCase):
    def test_only_changed_files_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = pathlib.Path(tmp) / "claude/projects/p"
            projects.mkdir(parents=True)
            log = projects / "a.jsonl"
            log.write_text(json.dumps(reply()) + "\n")
            old = collect.log_sources
            collect.log_sources = lambda: [(projects, collect.claude_rows)]
            try:
                state = {"repos": {"/w": "your-org/app"}}
                rows, marks, _ = collect.collect(state)
                self.assertEqual(len(rows), 1)
                state["files"].update(marks)
                self.assertEqual(collect.collect(state)[0], [])
                with open(log, "a") as handle:
                    handle.write(json.dumps(reply("m2")) + "\n")
                self.assertEqual(collect.collect(state)[0][0]["output_tokens"], 20)
            finally:
                collect.log_sources = old


class Scheduling(unittest.TestCase):
    PY, SCRIPT = "/py dir/python3", "/h/.pulse-tracker/collect.py"

    def test_mac_runs_hourly_via_launchd(self):
        plan = collect.schedule_plan("Darwin", self.PY, self.SCRIPT, "/Users/a")
        path, body = next(iter(plan["files"].items()))
        self.assertEqual(path, "/Users/a/Library/LaunchAgents/com.example.pulse-tracker.plist")
        self.assertIn("<integer>3600</integer>", body)
        self.assertIn("/py dir/python3", body)
        self.assertEqual(plan["setup"][-1][0][:2], ["launchctl", "bootstrap"])

    def test_linux_uses_a_persistent_user_timer_and_quotes_paths(self):
        plan = collect.schedule_plan("Linux", self.PY, self.SCRIPT, "/home/a")
        self.assertIn(f'ExecStart="{self.PY}" "{self.SCRIPT}" run',
                      plan["files"]["/home/a/.config/systemd/user/pulse-tracker.service"])
        self.assertIn("Persistent=true", plan["files"]["/home/a/.config/systemd/user/pulse-tracker.timer"])

    def test_windows_uses_task_scheduler(self):
        plan = collect.schedule_plan("Windows", r"C:\Program Files\Py\pythonw.exe", r"C:\u\collect.py", r"C:\u")
        argv = plan["setup"][0][0]
        self.assertEqual(argv[:6], ["schtasks", "/Create", "/F", "/SC", "HOURLY", "/TN"])
        self.assertEqual(argv[-1], r'"C:\Program Files\Py\pythonw.exe" "C:\u\collect.py" run')
        battery = plan["setup"][1]
        self.assertIn("-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable", battery[0][-1])
        self.assertFalse(battery[1], "an old Windows without this command should still install")
        self.assertEqual(plan["setup"][2][0][:2], ["schtasks", "/Run"])
        self.assertEqual(plan["teardown"][0][0][:2], ["schtasks", "/Delete"])

    def test_cron_keeps_other_entries_and_replaces_ours(self):
        once = collect.cron_with("5 5 * * * backup\n", "0 * * * * run")
        twice = collect.cron_with(once, "0 * * * * run2")
        self.assertEqual(twice.count(collect.CRON_TAG), 1)
        self.assertIn("backup", twice)
        self.assertIn("run2", twice)
        self.assertEqual(collect.cron_with(twice, None), "5 5 * * * backup\n")


class SchedulerFallback(unittest.TestCase):
    def patched(self, **values):
        for name, value in values.items():
            self.addCleanup(setattr, collect, name, getattr(collect, name))
            setattr(collect, name, value)

    def refuse(self, _steps):
        raise collect.subprocess.CalledProcessError(1, "systemctl")

    def setUp(self):
        self.lines = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = pathlib.Path.home
        pathlib.Path.home = staticmethod(lambda: pathlib.Path(tmp.name))
        self.addCleanup(setattr, pathlib.Path, "home", original)
        self.patched(use_cron=lambda: False, apply_plan=self.refuse, update_crontab=self.lines.append)

    def as_system(self, name):
        self.addCleanup(setattr, collect.platform, "system", collect.platform.system)
        collect.platform.system = lambda: name

    def test_linux_falls_back_to_cron_when_systemd_refuses(self):
        self.as_system("Linux")
        self.assertEqual(collect.schedule("/py", "/s.py"), "cron")
        self.assertIn('"/s.py" run', self.lines[0])

    def test_other_systems_report_the_failure(self):
        self.as_system("Windows")
        with self.assertRaises(SystemExit):
            collect.schedule("/py", "/s.py")


class Urls(unittest.TestCase):
    def test_refuses_to_send_a_token_over_plain_http(self):
        with self.assertRaises(SystemExit):
            collect.validate_url("http://tracker.example.com")
        self.assertEqual(collect.validate_url("https://t.example.com/"), "https://t.example.com")
        self.assertEqual(collect.validate_url("http://localhost:8787"), "http://localhost:8787")


class GitHubSignIn(unittest.TestCase):
    def run_flow(self, answers):
        calls, waits = [], []

        def fake_request(url, data=None, headers=None):
            calls.append(url)
            if url == collect.GITHUB_DEVICE_URL:
                return {"device_code": "d", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
                        "interval": 5, "expires_in": 900}
            return answers.pop(0)

        real_request, real_open = collect.request_json, collect.webbrowser.open
        collect.request_json, collect.webbrowser.open = fake_request, lambda url: True
        try:
            return collect.github_sign_in("client", wait=waits.append), waits
        finally:
            collect.request_json, collect.webbrowser.open = real_request, real_open

    def test_waits_for_approval_and_backs_off_when_told(self):
        token, waits = self.run_flow([{"error": "authorization_pending"}, {"error": "slow_down", "interval": 10},
                                      {"access_token": "gho_x"}])
        self.assertEqual(token, "gho_x")
        self.assertEqual(waits, [5, 5, 10])

    def test_stops_when_the_developer_declines(self):
        with self.assertRaises(SystemExit):
            self.run_flow([{"error": "access_denied"}])

    def test_never_saves_the_github_sign_in(self):
        self.assertNotIn("github_token", collect.ROW_FIELDS)
        self.assertNotIn("github_token", json.dumps(collect.payload_for([], collections.Counter())))


class Doctor(unittest.TestCase):
    def test_explains_folders_without_naming_repos(self):
        self.assertEqual(collect.why_skipped(""), "no folder recorded")
        self.assertEqual(collect.why_skipped("/no/such/folder"), "folder no longer exists")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collect.why_skipped(tmp), "not a git repo")
            collect.subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            self.assertEqual(collect.why_skipped(tmp), "git repo with no origin remote")
            for url, expected in (("https://github.com/your-org/app.git", "ok"),
                                  ("git@github.com:someone/secret-app.git", "ok"),
                                  ("git@github-work:your-org/app.git", "ok"),
                                  ("https://user:tok@gitlab.com/your-org/app", "ok"),
                                  ("/srv/git/app.git", "remote host not recognised: ")):
                collect.subprocess.run(["git", "-C", tmp, "remote", "remove", "origin"], capture_output=True)
                collect.subprocess.run(["git", "-C", tmp, "remote", "add", "origin", url], check=True)
                self.assertEqual(collect.why_skipped(tmp), expected)


def said(role="user", text="fix the bug", cwd="/w", stamp="2026-11-05T10:00:00Z", **extra):
    return {"type": role, "sessionId": "s1", "cwd": cwd, "timestamp": stamp, "message": {"role": role, "content": text}, **extra}


class Capture(unittest.TestCase):
    def run_capture(self, messages, since="", after=0):
        return collect.capture_records(write_log(messages), "claude-code", collect.claude_messages, since, "salt", lambda cwd: "someone/personal", after)

    def test_keeps_prompts_text_and_tool_commands_but_not_contents_or_output(self):
        blocks = [{"type": "text", "text": "done"},
                  {"type": "tool_use", "name": "Bash", "input": {"command": "npm test", "description": SECRET}},
                  {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py", "old_string": SECRET, "new_string": SECRET}},
                  {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/b.py", "content": SECRET}}]
        records, _ = self.run_capture([
            said(), said("assistant", blocks),
            said("user", [{"type": "tool_result", "content": SECRET}], toolUseResult={"stdout": SECRET}),
        ])
        self.assertEqual([(r["role"], r["text"]) for r in records], [("prompt", "fix the bug"), ("response", "done"), ("tool", "Bash npm test"), ("tool", "Edit /w/a.py"), ("tool", "Write /w/b.py")])
        self.assertNotIn(SECRET, json.dumps(records))

    def test_scrubs_secrets_from_commands_and_limits_their_length(self):
        records, _ = self.run_capture([said("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234' x " + "y" * 5000}}])])
        self.assertNotIn("abcdefghijklmnop1234", records[0]["text"])
        self.assertEqual(records[0]["findings"][0]["type"], "bearer-token")
        self.assertLess(len(records[0]["text"]), collect.CAPTURE_MAX_COMMAND + 50)  # placeholders can add a little

    def test_covers_every_repo_and_marks_subagents(self):
        records, _ = self.run_capture([said(), said(isSidechain=True, text="from a subagent")])
        self.assertEqual([(r["repo"], r["agent"]) for r in records], [("someone/personal", False), ("someone/personal", True)])

    def test_folders_with_no_repo_are_labelled_not_dropped(self):
        repo_of = collect.RepoLookup({})
        self.assertEqual(repo_of("/gone/somewhere", collections.Counter()), collect.NO_REPO)

    def test_skips_injected_context(self):
        records, _ = self.run_capture([said(isMeta=True, text=SECRET), said(text=f"<system-reminder>{SECRET}</system-reminder>"),
                                       said(text=f"<system-reminder>{SECRET}</system-reminder>real question")])
        self.assertEqual([r["text"] for r in records], ["real question"])

    def test_resumes_after_what_was_already_sent(self):
        records, position = self.run_capture([said(text="one"), said(text="two"), said(text="three")], after=2)
        self.assertEqual(([r["text"] for r in records], position), (["three"], 3))

    def test_preview_date_filter_still_counts_positions(self):
        records, position = self.run_capture([said(stamp="2026-11-01T00:00:00Z"), said()], since="2026-11-05")
        self.assertEqual((len(records), position), (1, 2))

    def test_truncates_very_long_text(self):
        record = self.run_capture([said(text="x" * (collect.CAPTURE_MAX_TEXT + 5))])[0][0]
        self.assertEqual((len(record["text"]), record["truncated"]), (collect.CAPTURE_MAX_TEXT, True))

    def test_batches_stay_within_limits(self):
        record = {"text": "x" * 250_000}
        self.assertEqual([len(b) for b in collect.capture_batches([record] * 3)], [1, 1, 1])
        self.assertEqual([len(b) for b in collect.capture_batches([{"text": "x"}] * 150)], [100, 50])

    def test_progress_is_saved_per_file_and_unchanged_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.jsonl").write_text(json.dumps(said(text="one")) + "\n")
            old = collect.capture_sources
            collect.capture_sources = lambda: [(root, "claude-code", collect.claude_messages)]
            try:
                state = {}
                results = list(collect.capture_files(state, "", "salt"))
                self.assertEqual(len(results[0][1]), 1)
                state["capture"][results[0][0]] = results[0][2]
                self.assertEqual(list(collect.capture_files(state, "", "salt")), [])
                with open(root / "a.jsonl", "a") as handle:
                    handle.write(json.dumps(said(text="two")) + "\n")
                self.assertEqual([r["text"] for r in list(collect.capture_files(state, "", "salt"))[0][1]], ["two"])
            finally:
                collect.capture_sources = old

    def test_codex_keeps_messages_and_commands_but_not_output_or_patch_contents(self):
        log = write_log([
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/w"}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"<environment_context>{SECRET}</environment_context>"}, {"type": "input_text", "text": "hello"}]}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": SECRET}]}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "function_call", "name": "exec", "arguments": json.dumps({"cmd": ["git", "status"]})}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "function_call_output", "output": SECRET}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": f"*** Begin Patch\n*** Update File: src/a.py\n+{SECRET}\n*** End Patch"}},
            {"type": "response_item", "timestamp": TS, "payload": {"type": "custom_tool_call_output", "output": SECRET}},
        ])
        records, _ = collect.capture_records(log, "codex", collect.codex_messages, "", "salt", lambda cwd: "your-org/app")
        self.assertEqual([(r["role"], r["text"]) for r in records], [("prompt", "hello"), ("response", "hi"), ("tool", "exec git status"), ("tool", "apply_patch src/a.py")])
        self.assertNotIn(SECRET, json.dumps(records))


class Redaction(unittest.TestCase):
    def test_removes_known_secret_formats(self):
        samples = {
            "aws-access-key": "AKIAIOSFODNN7EXAMPLE", "github-token": "ghp_" + "a1" * 18, "slack-token": "xoxb-123456789012-abcdef",
            "anthropic-key": "sk-ant-api03-" + "a" * 30, "openai-key": "sk-proj-" + "a" * 30, "stripe-key": "sk_live_" + "a" * 24,
            "google-api-key": "AIza" + "a" * 35, "tracker-token": "slt_" + "ab" * 20, "jwt": "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeK",
        }
        for kind, secret in samples.items():
            text, findings = collect.redact(f"use {secret} here", "salt")
            self.assertEqual(text, f"use [REDACTED:{kind}] here", kind)
            self.assertEqual(findings[0]["type"], kind)
            self.assertNotIn(secret, json.dumps(findings))

    def test_removes_contextual_secrets(self):
        for text, secret in (("postgres://app:hunter2hunter@db.example.com:5432/x", "hunter2hunter"),
                             ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
                             ("DB_PASSWORD=correcthorsebattery", "correcthorsebattery"),
                             ('"api_key": "abcd1234efgh5678"', "abcd1234efgh5678"),
                             ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "abc")):
            clean, findings = collect.redact(text, "salt")
            self.assertNotIn(secret, clean, text)
            self.assertTrue(findings, text)

    def test_leaves_ordinary_text_alone(self):
        for text in ("Please fix the token limit bug in password reset.", "see https://example.com/a:b", "the secret is to test it"):
            self.assertEqual(collect.redact(text, "salt"), (text, []))

    def test_stays_fast_on_long_unbroken_text(self):
        for text in ("a" * 200_000, "Ab3+/" * 40_000, "a-" * 100_000, "token " * 40_000, "://" + "a" * 200_000, "-----BEGIN PRIVATE KEY-----" + "a" * 200_000):
            start = time.time()
            collect.redact(text, "salt")
            self.assertLess(time.time() - start, 2, text[:20])

    def test_fingerprints_match_for_repeats_and_depend_on_the_salt(self):
        one = collect.redact("AKIAIOSFODNN7EXAMPLE", "a")[1][0]["fingerprint"]
        self.assertEqual(one, collect.redact("AKIAIOSFODNN7EXAMPLE", "a")[1][0]["fingerprint"])
        self.assertNotEqual(one, collect.redact("AKIAIOSFODNN7EXAMPLE", "b")[1][0]["fingerprint"])


class Writes(unittest.TestCase):
    def test_state_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json"
            collect.write_json(path, {"a": 1})
            self.assertEqual(collect.read_json(path, None), {"a": 1})
            self.assertEqual(os.listdir(tmp), ["s.json"])


if __name__ == "__main__":
    unittest.main()
