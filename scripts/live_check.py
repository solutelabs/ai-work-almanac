"""Checks the live site is working, and keeps one GitHub issue open while it isn't.

Runs in the upload workflow and by hand after a deploy. Standard library only.
Every check returns None when it passes, or a reason. A missing answer is a reason, never a pass.

    python3 scripts/live_check.py                  # print the results, exit 1 if any failed
    python3 scripts/live_check.py --issue          # also open, update or close the live-check issue
"""
import argparse
import http.client
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("TRACKER_URL") or "https://pulse.example.com"
LABEL = "live-check"
GIT_MAX_AGE = 2 * 3600  # the dashboard itself flags git data after 2 hours
LAPTOP_MAX_AGE = 96 * 3600  # laptops sleep overnight and over weekends, so a shorter window would fire every weekend
DEPLOY_GRACE = 600  # a deploy takes a few minutes after main moves
GATED = ("/dashboard", "/api/dashboard")
REDIRECTS = (301, 302, 303, 307, 308)
USER_AGENT = "pulse-tracker-livecheck"  # Cloudflare answers 403 to requests with none


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, headers=None, tries=3, pause=5):
    """Returns (status, body), or (None, b"") if nothing answered. Retries so a brief blip isn't an alarm.
    A redirect's body is its Location, so the gated checks can see where it goes."""
    status, body = None, b""
    for attempt in range(tries):
        if attempt:
            time.sleep(pause)
        try:
            with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url, headers={"user-agent": USER_AGENT, **(headers or {})}), timeout=10) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.headers.get("location", "").encode() if e.code in REDIRECTS else b""
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            status, body = None, b""
        if status is not None and status < 500:
            break
    return status, body


def version(script):
    found = re.search(rb'^VERSION = "([^"]+)"', script or b"", re.M)
    return found.group(1).decode() if found else None


def check_collector(served, repo):
    if not served:
        return "/collect.py gave no answer"
    if version(repo) is None:
        return "the repo's collect.py has no VERSION"
    if version(served) != version(repo):
        return "/collect.py is version %s, the repo has %s" % (version(served), version(repo))
    return None if served == repo else "/collect.py differs from the repo's copy at the same version"


def check_enroll(status):
    return None if status == 200 else "/api/enroll/config answered %s" % (status or "nothing")


def check_gated(path, status, location=b""):
    """Gated means a redirect to Cloudflare Access. A 401 would mean the shared password is back."""
    if status == 200:
        return "%s answers without sign-in" % path
    if status in REDIRECTS and b"cloudflareaccess.com" in (location or b""):
        return None
    return "%s answered %s, not a redirect to Cloudflare Access" % (path, status or "nothing")


def age(stamp, now):
    try:
        return (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except (AttributeError, ValueError):
        return None


def check_health(health):
    """Git data is fresh, and a laptop reported recently. Ages use the server's own clock."""
    if not isinstance(health, dict) or not health.get("now"):
        return ["/api/admin/health gave no usable answer"]
    problems = []
    for key, limit, what in (("git_synced_at", GIT_MAX_AGE, "git data"), ("last_laptop_seen", LAPTOP_MAX_AGE, "laptop report")):
        seconds = age(health.get(key), health["now"])
        if seconds is None:
            problems.append("no %s on record" % what)
        elif seconds > limit:
            problems.append("%s is out of date" % what)
    return problems


def run_checks(get=fetch, repo_script=None, admin_token="", base=BASE, main_age=None):
    """Returns a list of reasons. Empty means everything passed.
    A /collect.py mismatch is ignored while main's tip is under 10 minutes old, as the deploy may not have landed."""
    if repo_script is None:
        repo_script = (ROOT / "collector" / "collect.py").read_bytes()
    failed = []

    def note(reason):
        if reason:
            failed.append(reason)

    if main_age is None or main_age > DEPLOY_GRACE:
        note(check_collector(get(base + "/collect.py")[1], repo_script))
    note(check_enroll(get(base + "/api/enroll/config")[0]))
    for path in GATED:
        status, location = get(base + path)
        note(check_gated(path, status, location))
    status, body = get(base + "/api/admin/health", {"authorization": "Bearer " + admin_token} if admin_token else {})
    try:
        health = json.loads(body) if status == 200 else None
    except ValueError:
        health = None
    failed.extend(check_health(health) if status == 200 else ["/api/admin/health answered %s" % (status or "nothing")])
    return failed


def report(failed):
    return "The live site failed these checks:\n\n" + "\n".join("- " + r for r in failed)


def sync_issue(failed, api):
    """One issue, reused: the latest labelled one, open or closed, so a flapping check doesn't pile up issues.
    `api(method, path, body)` returns parsed JSON. Same failures again change nothing."""
    latest = api("GET", "/issues?state=all&labels=%s&per_page=1" % LABEL, None)
    current = latest[0] if latest else None
    is_open = current and current["state"] == "open"
    if failed and not current:
        api("POST", "/issues", {"title": "Live check is failing", "body": report(failed), "labels": [LABEL]})
    elif failed and (not is_open or current["body"] != report(failed)):
        api("PATCH", "/issues/%d" % current["number"], {"state": "open", "body": report(failed)})
        api("POST", "/issues/%d/comments" % current["number"], {"body": report(failed)})
    elif not failed and is_open:
        api("POST", "/issues/%d/comments" % current["number"], {"body": "Every live check passes again. Closing."})
        api("PATCH", "/issues/%d" % current["number"], {"state": "closed"})


def github_api(repo, token):
    def call(method, path, body):
        request = urllib.request.Request(
            "https://api.github.com/repos/%s%s" % (repo, path),
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"authorization": "Bearer " + token, "accept": "application/vnd.github+json", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read() or b"null")
    return call


def main_tip_age(api):
    """Seconds since main's tip commit, or None if it can't be read (then a mismatch counts)."""
    try:
        stamp = api("GET", "/commits/main", None)["commit"]["committer"]["date"]
        return age(stamp, datetime.now(timezone.utc).isoformat())
    except (urllib.error.URLError, OSError, http.client.HTTPException, KeyError, TypeError, ValueError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", action="store_true", help="open, update or close the live-check issue")
    args = parser.parse_args(argv)
    api = github_api(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]) if args.issue else None
    failed = run_checks(admin_token=os.environ.get("TRACKER_ADMIN_TOKEN", ""), main_age=main_tip_age(api) if api else None)
    print(report(failed) if failed else "Every live check passes.")
    if api:
        sync_issue(failed, api)
        return 0  # the issue is the alarm, so a failed check isn't a red run. A crash above still is.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
