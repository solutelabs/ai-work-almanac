import io
import json
import unittest
from datetime import datetime, timezone

import weekly_status as ws

NOW = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
HOOK = "https://hooks.slack.com/x"


def pr(title, base, head, merged="2026-11-05T10:00:00Z", n=1):
    return {"title": title, "html_url": "https://github.com/x/pull/%d" % n, "merged_at": merged,
            "base": {"ref": base}, "head": {"ref": head}}


def milestone(description, closed=3, open_=2):
    return {"title": "1.2", "html_url": "https://github.com/x/milestone/2", "description": description,
            "closed_issues": closed, "open_issues": open_}


class Build(unittest.TestCase):
    def test_a_normal_week(self):
        text = ws.build_status(NOW, [milestone("Go: yes\nBlocked on: review")],
                               [pr("Shipped", "main", "release", n=1), pr("Waiting", "develop", "feat/a", merged="2026-11-06T10:00:00Z", n=2),
                                pr("Too old", "main", "release", merged="2026-10-01T10:00:00Z", n=3)],
                               [pr("Open one", "develop", "feat/b", merged=None, n=4)])
        self.assertIn("<https://github.com/x/pull/1|Shipped>", text)
        self.assertIn("<https://github.com/x/pull/2|Waiting>", text)
        self.assertNotIn("Too old", text)
        self.assertIn("<https://github.com/x/milestone/2|1.2>: 3 of 5 done\n  Go: yes\n  Blocked on: review", text)
        self.assertIn("<https://github.com/x/pull/4|Open one>", text)

    def test_empty_sections_say_so(self):
        text = ws.build_status(NOW, [], [], [])
        for words in ("Nothing released this week.", "Nothing is waiting", "No open releases.", "No open pull requests."):
            self.assertIn(words, text)

    def test_missing_lines_say_not_stated(self):
        text = ws.build_status(NOW, [milestone(None), milestone("Go: soon")], [], [])
        self.assertEqual(text.count("Blocked on: not stated"), 2)
        self.assertEqual(text.count("Go: not stated"), 1)
        self.assertIn("Go: soon", text)

    def test_waiting_means_merged_after_the_latest_main_merge_however_old(self):
        text = ws.build_status(NOW, [], [
            pr("Already shipped", "develop", "feat/a", merged="2026-10-20T10:00:00Z", n=1),
            pr("Release", "main", "develop", merged="2026-10-25T10:00:00Z", n=2),
            pr("Old but waiting", "develop", "feat/b", merged="2026-10-26T10:00:00Z", n=3)], [])
        self.assertNotIn("Already shipped", text)
        self.assertIn("Old but waiting", text)
        self.assertIn("Nothing released this week.", text)

    def test_go_and_blocked_lines_are_escaped(self):
        text = ws.build_status(NOW, [milestone("Go: <!channel> yes\nBlocked on: a & b")], [], [])
        self.assertIn("Go: &lt;!channel&gt; yes", text)
        self.assertIn("Blocked on: a &amp; b", text)
        self.assertNotIn("<!channel>", text)

    def test_sync_prs_are_left_out(self):
        text = ws.build_status(NOW, [], [pr("Sync back", "develop", "sync/release-1.2")], [])
        self.assertNotIn("Sync back", text)


class Reply:
    def __init__(self, body, link=None):
        self.body, self.headers = io.BytesIO(json.dumps(body).encode()), {"Link": link}

    def read(self, *a):
        return self.body.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def opener(calls, fail_on=None):
    def call(request, timeout=0):
        url = request.full_url
        calls.append(url)
        if fail_on and fail_on in url:
            raise OSError(HOOK)
        if "page=2" in url:
            return Reply([pr("p2", "main", "h", merged="2026-11-06T00:00:00Z")])
        if "state=closed" in url:
            return Reply([], link='<https://api.github.com/closed?page=2>; rel="next"')
        return Reply({} if "hooks.slack.com" in url else [])
    return call


class Run(unittest.TestCase):
    def run_main(self, env, argv=(), fail_on=None):
        calls, out, err = [], io.StringIO(), io.StringIO()
        code = ws.main(list(argv), env, opener(calls, fail_on), NOW, out, err)
        return code, calls, out.getvalue(), err.getvalue()

    def test_posts_and_follows_pagination(self):
        code, calls, out, _ = self.run_main({"GITHUB_TOKEN": "t", "SLACK_STATUS_WEBHOOK": HOOK})
        self.assertEqual(code, 0)
        self.assertTrue(any("page=2" in c for c in calls))
        self.assertEqual(calls[-1], HOOK)
        self.assertIn("p2", out)

    def test_dry_run_posts_nothing(self):
        code, calls, out, _ = self.run_main({"GITHUB_TOKEN": "t", "SLACK_STATUS_WEBHOOK": HOOK}, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn(HOOK, calls)
        self.assertIn("Released to the live site", out)

    def test_failed_fetch_posts_nothing_and_fails(self):
        code, calls, _, err = self.run_main({"GITHUB_TOKEN": "t", "SLACK_STATUS_WEBHOOK": HOOK}, fail_on="page=2")
        self.assertEqual(code, 1)
        self.assertTrue(any("page=2" in c for c in calls), "it got as far as the second page of closed PRs")
        self.assertNotIn(HOOK, calls)
        self.assertNotIn("hooks.slack.com", err)

    def test_no_webhook_prints_and_fails(self):
        code, _, out, err = self.run_main({"GITHUB_TOKEN": "t"})
        self.assertEqual(code, 1)
        self.assertIn("SLACK_STATUS_WEBHOOK", err)
        self.assertIn("Open pull requests", out)

    def test_a_failed_post_fails_without_the_webhook_in_the_output(self):
        code, _, _, err = self.run_main({"GITHUB_TOKEN": "t", "SLACK_STATUS_WEBHOOK": HOOK}, fail_on="hooks.slack.com")
        self.assertEqual(code, 1)
        self.assertNotIn("hooks.slack.com", err)


if __name__ == "__main__":
    unittest.main()
