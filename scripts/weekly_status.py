"""Posts the weekly Pulse status to Slack: what shipped, what's waiting, what's open.

Runs in the status workflow, which the Worker's Monday cron starts. Standard library only.
It holds no names of people and nothing about logged time. If any GitHub request fails, nothing is posted.

    python3 scripts/weekly_status.py --dry-run     # print the text, post nothing
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY") or "your-org/ai-work-almanac"  # set by GitHub Actions
API = "https://api.github.com"


def parse_time(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def escape(text):
    # Slack mrkdwn would read a stray >, < or & as markup, such as <!channel>
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(item):
    return "<%s|%s>" % (item["html_url"], escape(item["title"]))


def line_of(description, label):
    found = re.search(r"^\s*%s:[ \t]*(.+)$" % label, description or "", re.M | re.I)
    return escape(found.group(1).strip()) if found else "not stated"


def section(title, lines, empty):
    return "*%s*\n%s" % (title, "\n".join(lines) if lines else empty)


def build_status(now, milestones, merged, open_prs):
    """Returns the Slack text. `merged` is PRs that have a merged_at, in any order."""
    since = now - timedelta(days=7)
    main_merges = [parse_time(p["merged_at"]) for p in merged if p["base"]["ref"] == "main"]
    last_release = max(main_merges, default=None)
    released = [p for p in merged if p["base"]["ref"] == "main" and parse_time(p["merged_at"]) >= since]
    # Anything merged to develop after the latest merge to main is unreleased, however old.
    waiting = [p for p in merged if p["base"]["ref"] == "develop" and not p["head"]["ref"].startswith("sync/")
               and (last_release is None or parse_time(p["merged_at"]) > last_release)]
    releases = []
    for m in milestones:
        done, total = m["closed_issues"], m["closed_issues"] + m["open_issues"]
        releases.append("- %s: %d of %d done\n  Go: %s\n  Blocked on: %s" % (
            link(m), done, total, line_of(m.get("description"), "Go"), line_of(m.get("description"), "Blocked on")))
    return "\n\n".join([
        "*Pulse status, week to %s*" % now.strftime("%-d %b %Y"),
        section("Released to the live site", ["- " + link(p) for p in released], "Nothing released this week."),
        section("Merged, not released yet", ["- " + link(p) for p in waiting], "Nothing is waiting for a release."),
        section("Next releases", releases, "No open releases."),
        section("Open pull requests", ["- " + link(p) for p in open_prs], "No open pull requests."),
    ])


def get_pages(path, token, opener=urllib.request.urlopen):
    """Follows GitHub's Link header. Any failure raises, so nothing gets posted."""
    url, items = API + path, []
    while url:
        request = urllib.request.Request(url, headers={
            "authorization": "Bearer " + token, "accept": "application/vnd.github+json", "user-agent": "your-org-status"})
        with opener(request, timeout=30) as response:
            items += json.load(response)
            next_link = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link") or "")
        url = next_link.group(1) if next_link else None
    return items


def fetch_all(token, now, opener=urllib.request.urlopen):
    def get(path):
        return get_pages(path, token, opener)

    milestones = get("/repos/%s/milestones?state=open&per_page=100" % REPO)
    # shortcut: reads every closed PR, so it slows as history grows; stop at the latest main merge then.
    closed = [p for p in get("/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100" % REPO)
              if p.get("merged_at")]
    return milestones, closed, get("/repos/%s/pulls?state=open&per_page=100" % REPO)


def post(webhook, text, opener=urllib.request.urlopen):
    request = urllib.request.Request(webhook, json.dumps({"text": text}).encode(), {"content-type": "application/json"})
    with opener(request, timeout=30):
        pass


def main(argv=None, env=None, opener=urllib.request.urlopen, now=None, out=sys.stdout, err=sys.stderr):
    env = os.environ if env is None else env
    dry = "--dry-run" in (sys.argv[1:] if argv is None else argv)
    now = now or datetime.now(timezone.utc)
    try:
        text = build_status(now, *fetch_all(env.get("GITHUB_TOKEN", ""), now, opener))
    except Exception as error:  # only the type is shown: messages can carry URLs
        print("Could not read GitHub, so nothing was posted: %s" % type(error).__name__, file=err)
        return 1
    print(text, file=out)
    if dry:
        return 0
    if not env.get("SLACK_STATUS_WEBHOOK"):
        print("SLACK_STATUS_WEBHOOK isn't set, so nothing was posted.", file=err)
        return 1
    try:
        post(env["SLACK_STATUS_WEBHOOK"], text, opener)
    except Exception as error:
        print("Slack refused the post: %s" % type(error).__name__, file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
