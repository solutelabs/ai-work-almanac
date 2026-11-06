import http.client
import json
import unittest
import urllib.error
from unittest import mock

import live_check as lc

REPO = b'import x\nVERSION = "0.10.0"\n'
NOW = "2026-11-05T12:00:00.000Z"
GOOD = {"now": NOW, "git_synced_at": "2026-11-05T11:50:00.000Z", "last_laptop_seen": "2026-11-05T09:00:00.000Z", "laptops_seen_24h": 3}


class Collector(unittest.TestCase):
    def test_pass(self):
        self.assertIsNone(lc.check_collector(REPO, REPO))

    def test_fail_on_version_and_on_bytes(self):
        self.assertIn("0.9.0", lc.check_collector(b'VERSION = "0.9.0"\n', REPO))
        self.assertIn("differs", lc.check_collector(REPO + b"# x\n", REPO))

    def test_no_data(self):
        self.assertTrue(lc.check_collector(b"", REPO))
        self.assertTrue(lc.check_collector(None, REPO))
        self.assertTrue(lc.check_collector(b"no version here", b"also none"))


class Enroll(unittest.TestCase):
    def test_pass_fail_no_data(self):
        self.assertIsNone(lc.check_enroll(200))
        self.assertTrue(lc.check_enroll(503))
        self.assertTrue(lc.check_enroll(None))


ACCESS = b"https://team.cloudflareaccess.com/cdn-cgi/access/login/pulse"


class Gated(unittest.TestCase):
    def test_pass_only_on_a_redirect_to_access(self):
        for status in (301, 302, 307):
            self.assertIsNone(lc.check_gated("/dashboard", status, ACCESS))

    def test_a_401_or_403_or_other_redirect_fails(self):
        for status, where in ((401, b""), (403, b""), (302, b"https://evil.example/login"), (302, b"")):
            self.assertTrue(lc.check_gated("/dashboard", status, where), status)

    def test_fail_when_open(self):
        self.assertIn("without sign-in", lc.check_gated("/dashboard", 200))

    def test_no_data_or_an_error_is_not_a_pass(self):
        for status in (None, 404, 500, 530):
            self.assertTrue(lc.check_gated("/api/dashboard", status, ACCESS), status)


class Health(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(lc.check_health(GOOD), [])

    def test_stale_git_and_laptops(self):
        old = dict(GOOD, git_synced_at="2026-11-05T08:00:00.000Z", last_laptop_seen="2026-10-31T08:00:00.000Z")
        found = lc.check_health(old)
        self.assertEqual(len(found), 2)
        self.assertFalse(any(c.isdigit() for c in "".join(found)), "reasons carry no ages, so they stay the same hour to hour")

    def test_a_quiet_weekend_passes(self):
        self.assertEqual(lc.check_health(dict(GOOD, last_laptop_seen="2026-11-02T00:00:00.000Z")), [])

    def test_no_data(self):
        for bad in (None, {}, "x", {"now": NOW}):
            self.assertTrue(lc.check_health(bad), bad)
        self.assertEqual(len(lc.check_health(dict(GOOD, git_synced_at=None))), 1)
        self.assertEqual(len(lc.check_health(dict(GOOD, last_laptop_seen="junk"))), 1)


def served(overrides=None, health=GOOD, health_status=200):
    answers = {
        "/collect.py": (200, REPO), "/api/enroll/config": (200, b"{}"), "/dashboard": (302, ACCESS), "/api/dashboard": (302, ACCESS),
        "/api/admin/health": (health_status, json.dumps(health).encode()),
    }
    answers.update(overrides or {})
    return lambda url, headers=None: answers[url.replace("https://x", "")]


class Everything(unittest.TestCase):
    def run_it(self, main_age=None, **kwargs):
        return lc.run_checks(get=served(**kwargs), repo_script=REPO, admin_token="t", base="https://x", main_age=main_age)

    def test_a_collector_mismatch_waits_for_the_deploy(self):
        stale = {"/collect.py": (200, b'VERSION = "0.9.0"\n')}
        self.assertEqual(self.run_it(overrides=stale, main_age=120), [])
        self.assertEqual(len(self.run_it(overrides=stale, main_age=1200)), 1)
        self.assertEqual(len(self.run_it(overrides=stale, main_age=None)), 1)

    def test_all_pass(self):
        self.assertEqual(self.run_it(), [])

    def test_each_failure_is_named(self):
        self.assertEqual(len(self.run_it(overrides={"/dashboard": (200, b"")})), 1)
        self.assertEqual(len(self.run_it(overrides={"/api/enroll/config": (None, b"")})), 1)

    def test_health_that_cant_be_read_fails(self):
        self.assertEqual(len(self.run_it(health_status=401)), 1)
        self.assertEqual(len(self.run_it(overrides={"/api/admin/health": (200, b"not json")})), 1)


class Issue(unittest.TestCase):
    def sync(self, failed, latest):
        calls = []

        def api(method, path, body):
            calls.append((method, path, body))
            return latest if method == "GET" else {}
        lc.sync_issue(failed, api)
        return [c for c in calls if c[0] != "GET"]

    def test_opens_one_issue_with_the_label(self):
        made = self.sync(["a"], [])
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][:2], ("POST", "/issues"))
        self.assertEqual(made[0][2]["labels"], ["live-check"])

    def test_a_repeat_with_the_same_failure_does_nothing(self):
        self.assertEqual(self.sync(["a"], [{"number": 4, "state": "open", "body": lc.report(["a"])}]), [])

    def test_a_changed_failure_updates_the_open_issue_and_opens_no_new_one(self):
        made = self.sync(["a", "b"], [{"number": 4, "state": "open", "body": lc.report(["a"])}])
        self.assertEqual([m[:2] for m in made], [("PATCH", "/issues/4"), ("POST", "/issues/4/comments")])

    def test_a_failure_after_a_close_reopens_the_latest_issue(self):
        made = self.sync(["a"], [{"number": 4, "state": "closed", "body": lc.report(["a"])}])
        self.assertEqual([m[:2] for m in made], [("PATCH", "/issues/4"), ("POST", "/issues/4/comments")])
        self.assertEqual(made[0][2]["state"], "open")

    def test_the_search_includes_closed_issues(self):
        paths = []
        lc.sync_issue([], lambda m, p, b: paths.append(p) or [])
        self.assertIn("state=all", paths[0])

    def test_closes_with_a_comment_when_clear(self):
        made = self.sync([], [{"number": 4, "state": "open", "body": "x"}])
        self.assertEqual([m[:2] for m in made], [("POST", "/issues/4/comments"), ("PATCH", "/issues/4")])
        self.assertEqual(made[1][2], {"state": "closed"})

    def test_clear_with_nothing_open_does_nothing(self):
        self.assertEqual(self.sync([], []), [])

    def test_clear_with_only_a_closed_issue_does_nothing(self):
        self.assertEqual(self.sync([], [{"number": 4, "state": "closed", "body": "x"}]), [])


class Fetch(unittest.TestCase):
    def test_a_blip_is_retried(self):
        opener = mock.MagicMock()
        ok = mock.MagicMock()
        ok.__enter__.return_value.status = 200
        ok.__enter__.return_value.read.return_value = b"hi"
        opener.open.side_effect = [OSError("down"), ok]
        with mock.patch("urllib.request.build_opener", return_value=opener), mock.patch("time.sleep"):
            self.assertEqual(lc.fetch("https://x"), (200, b"hi"))
        self.assertEqual(opener.open.call_count, 2)

    def test_sends_the_user_agent_and_a_short_timeout(self):
        opener = mock.MagicMock()
        opener.open.side_effect = OSError("down")
        with mock.patch("urllib.request.build_opener", return_value=opener), mock.patch("time.sleep"):
            lc.fetch("https://x", tries=1)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_header("User-agent"), "pulse-tracker-livecheck")
        self.assertEqual(opener.open.call_args[1]["timeout"], 10)

    def test_a_redirect_gives_its_location(self):
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://x", 302, "Found", {"location": "https://t.cloudflareaccess.com/x"}, None)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(lc.fetch("https://x"), (302, b"https://t.cloudflareaccess.com/x"))

    def test_a_broken_response_is_no_answer_not_a_crash(self):
        opener = mock.MagicMock()
        opener.open.side_effect = http.client.RemoteDisconnected("closed")
        with mock.patch("urllib.request.build_opener", return_value=opener), mock.patch("time.sleep"):
            self.assertEqual(lc.fetch("https://x"), (None, b""))

    def test_still_down_is_no_answer(self):
        opener = mock.MagicMock()
        opener.open.side_effect = OSError("down")
        with mock.patch("urllib.request.build_opener", return_value=opener), mock.patch("time.sleep"):
            self.assertEqual(lc.fetch("https://x"), (None, b""))


class MainTip(unittest.TestCase):
    def test_age_from_the_commit_date_and_none_when_unreadable(self):
        got = lc.main_tip_age(lambda *a: {"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}})
        self.assertGreater(got, 3600)
        self.assertIsNone(lc.main_tip_age(lambda *a: {}))
        self.assertIsNone(lc.main_tip_age(mock.Mock(side_effect=OSError())))


class Main(unittest.TestCase):
    def test_with_issue_a_failed_check_still_exits_zero_after_syncing(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
        with mock.patch.dict("os.environ", env), mock.patch.object(lc, "run_checks", return_value=["bad"]), \
                mock.patch.object(lc, "main_tip_age", return_value=None), mock.patch.object(lc, "sync_issue") as sync, mock.patch("builtins.print"):
            self.assertEqual(lc.main(["--issue"]), 0)
        sync.assert_called_once()

    def test_a_crash_in_the_sync_is_not_swallowed(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
        with mock.patch.dict("os.environ", env), mock.patch.object(lc, "run_checks", return_value=[]), \
                mock.patch.object(lc, "main_tip_age", return_value=None), mock.patch.object(lc, "sync_issue", side_effect=OSError("x")), \
                mock.patch("builtins.print"):
            with self.assertRaises(OSError):
                lc.main(["--issue"])

    def test_without_issue_a_failure_exits_one(self):
        with mock.patch.object(lc, "run_checks", return_value=["bad"]), mock.patch("builtins.print"):
            self.assertEqual(lc.main([]), 1)


if __name__ == "__main__":
    unittest.main()
