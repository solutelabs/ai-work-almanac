#!/usr/bin/env python3
"""Who commits how much across the org's repos, and how much of it is Claude co-authored.

Usage: ./tracker.py [--active-days 90] [--since YYYY-MM-DD] [--out report.html]
Needs git and gh, logged in with read access to the org. Standard library only.
"""
import argparse
import base64
import collections
import concurrent.futures
import datetime
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ORG = os.environ.get("TRACKER_ORG") or "your-org"  # the GitHub org whose repos are read
CACHE = pathlib.Path.home() / ".cache/ai-work-almanac/repos"

# Anchored to trailer lines so prose like "docs(claude-md)" doesn't count, and to Anthropic's
# address so a human colleague named Claude doesn't either.
CLAUDE_RE = re.compile(
    r"^\s*(co-authored-by:.*(@anthropic\.com|claude\[bot\])|.*generated with \[?claude)",
    re.I | re.M,
)
# Marks a commit as made with any AI tool, not only Claude.
AI_TRAILER_RE = re.compile(r"^\s*(co-authored-by:.*(codex|openai|cursor|copilot|aider|gemini|devin)|.*generated (with|by) \[?(codex|cursor|copilot|aider|gemini))", re.I | re.M)
# Generated files would otherwise swamp the line counts.
NOISE_RE = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|\.next|Pods|coverage)/"
    r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|Podfile\.lock|Gemfile\.lock"
    r"|poetry\.lock|uv\.lock|Cargo\.lock|composer\.lock|go\.sum)$"
    r"|\.(min\.js|min\.css|map|snap|svg|pen|jsonl)$"
)
NOREPLY_RE = re.compile(r"^(?:\d+\+)?(.+)@users\.noreply\.github\.com$", re.I)
COMMIT_BATCH = 2000
TIME_LOG_DAYS = 35
SOLIDTIME_API = os.environ.get("SOLIDTIME_API") or "https://solidtime.example.com/api/v1"  # your solidtime server
SOLIDTIME_PROJECTS = pathlib.Path(__file__).with_name("solidtime_projects.json")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
LOG_FORMAT = "%x1e%H%x1f%aN%x1f%aE%x1f%ad%x1f%B%x1f"
# shortcut: a size cutoff can't tell an import from a huge real commit; these 42 of 17k commits
# were 48% of all lines and the ones sampled were imports, so list paths if real work gets caught.
BULK_LINES = 20_000
PEOPLE_FILE = pathlib.Path(__file__).with_name("people.json")


def run(*cmd):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # fail on bad auth instead of hanging
    if os.environ.get("GH_TOKEN"):  # no gh on the CI runner; env keeps the token out of process listings
        basic = base64.b64encode(f"x-access-token:{os.environ['GH_TOKEN']}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", check=True, env=env
    ).stdout


def github(path, body=None):
    token = os.environ.get("GH_TOKEN") or run("gh", "auth", "token").strip()
    return read_json_with_retries(urllib.request.Request(
        f"https://api.github.com/{path}", data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "pulse-tracker-git"}))


def read_json_with_retries(request):
    for attempt in range(3):  # GitHub's GraphQL API returns the odd 502 under load
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def list_repos(active_days):
    repos, page = [], 1
    while True:
        batch = github(f"orgs/{ORG}/repos?per_page=100&page={page}")
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=active_days)).strftime("%Y-%m-%d")
    return [r for r in repos
            if not r["archived"] and not r["fork"] and (not active_days or r["pushed_at"] >= cutoff)]


def read_repo(repo):
    """Clone or update the default branch only, and return its raw log."""
    name, branch = repo["name"], repo["default_branch"]
    path = CACHE / f"{name}.git"
    if path.exists():
        run("git", "-C", str(path), "fetch", "--quiet", "--no-tags", "origin",
            f"+refs/heads/{branch}:refs/heads/{branch}")
    else:
        # Clone beside the target so an interrupted clone never looks like a finished one.
        tmp = CACHE / f"{name}.tmp"
        run("rm", "-rf", str(tmp))
        run("git", "clone", "--quiet", "--bare", "--single-branch", "--no-tags", "--branch", branch,
            f"https://github.com/{ORG}/{name}.git", str(tmp))
        tmp.rename(path)
    # Default branch only: squash merges rewrite SHAs, so all branches would double-count work.
    return run("git", "-C", str(path), "log", f"refs/heads/{branch}", "--no-merges", "--numstat",
               "--date=short", f"--format={LOG_FORMAT}", "--")


def person_key(name, email):
    match = NOREPLY_RE.match(email)
    return (match.group(1) if match else email).lower() or name.lower()


def load_aliases(path=PEOPLE_FILE):
    """people.json maps a display name to every identity (see person_key) that person commits as."""
    people = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {key.lower(): name for name, keys in people.items() for key in keys}


def make_commit(repo, sha, name, email, date, body, added, deleted, aliases):
    bulk = max(added, deleted) > BULK_LINES
    claude = (bool(CLAUDE_RE.search(body)) or "claude[bot]" in name.lower()
              or email.lower().endswith("@anthropic.com"))
    person = person_key(name, email)
    alias = (aliases or {}).get(person)
    return {
        "repo": repo, "sha": sha, "name": alias or name, "person": alias or person, "identity": person,
        "date": date, "added": 0 if bulk else added, "deleted": 0 if bulk else deleted,
        "bulk": added if bulk else 0, "claude": claude,
    }


def is_other_bot(name):
    return "[bot]" in name.lower() and "claude" not in name.lower()  # dependabot and friends aren't developers


def parse_log(text, repo, aliases=None):
    commits = []
    for record in text.split("\x1e")[1:]:
        sha, name, email, date, body, numstat = record.split("\x1f")
        if is_other_bot(name):
            continue
        added = deleted = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            # Binary files report "-" for both counts.
            if len(parts) == 3 and parts[0] != "-" and not NOISE_RE.search(parts[2].rstrip("}")):
                added += int(parts[0])
                deleted += int(parts[1])
        commits.append(make_commit(repo, sha, name, email, date, body, added, deleted, aliases))
    return commits


def aggregate(commits, key):
    rows = collections.defaultdict(collections.Counter)
    for c in commits:
        row = rows[key(c)]
        row["commits"] += 1
        row["added"] += c["added"]
        row["deleted"] += c["deleted"]
        if c["claude"]:
            row["claude_commits"] += 1
            row["claude_added"] += c["added"]
    return rows


def percent(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "–"


def table(title, labels, rows, sort_by_label=False):
    """rows maps a tuple of label cells to its aggregate counter."""
    heads = [*labels, "Commits", "With Claude", "% commits", "Lines added", "In Claude commits",
             "% lines", "Lines deleted"]
    order = sorted(rows.items(), key=(lambda kv: kv[0]) if sort_by_label
                   else (lambda kv: -kv[1]["added"]), reverse=sort_by_label)
    body = []
    for cells, r in order:
        stats = [f"{r['commits']:,}", f"{r['claude_commits']:,}",
                 percent(r["claude_commits"], r["commits"]), f"{r['added']:,}",
                 f"{r['claude_added']:,}", percent(r["claude_added"], r["added"]),
                 f"{r['deleted']:,}"]
        tds = "".join(f"<td>{html.escape(str(c))}</td>" for c in cells)
        body.append(f"<tr>{tds}{''.join(f'<td class=n>{s}</td>' for s in stats)}</tr>")
    ths = "".join(f'<th scope=col{" class=n" if i >= len(labels) else ""}>{h}</th>'
                  for i, h in enumerate(heads))
    return f"<h2>{title}</h2><table><thead><tr>{ths}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def display_names(commits):
    """Each person's most-used name, so one identity shows as one row."""
    names = collections.defaultdict(collections.Counter)
    for c in commits:
        names[c["person"]][c["name"]] += 1
    return lambda c: names[c["person"]].most_common(1)[0][0]


def dashboard_rows(commits):
    who = display_names(commits)
    rows = aggregate(commits, lambda c: (c["date"][:7], f"{ORG}/{c['repo']}".lower(), who(c)))
    return [{"month": month, "repo": repo, "person": person, "commits": r["commits"],
             "claude_commits": r["claude_commits"], "lines_added": r["added"], "lines_removed": r["deleted"],
             "claude_lines_added": r["claude_added"]} for (month, repo, person), r in rows.items()]


def dashboard_identities(commits):
    """Lets the dashboard match someone's GitHub sign-in to the name their commits appear under."""
    who = display_names(commits)
    return [{"key": key, "person": person} for key, person in sorted({c["identity"]: who(c) for c in commits}.items()) if key]


COMMIT_QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { oid message authoredDate parents { totalCount } author { name email } }
      }
    } } }
  }
}"""


def parse_api_commits(nodes, repo, aliases):
    """Whole-history queries leave out line counts, because asking makes GitHub time out on large repos."""
    commits = []
    for node in nodes:
        author = node.get("author") or {}
        name, email = author.get("name") or "", author.get("email") or ""
        if node["parents"]["totalCount"] > 1 or is_other_bot(name):
            continue
        commits.append(make_commit(repo, node["oid"], name, email, ist_day(node["authoredDate"]), node["message"],
                                   node.get("additions", 0), node.get("deletions", 0), aliases))
    return commits


def fetch_commits(repo, aliases):
    """Reads history without cloning, so it needs no disk and suits a fresh runner."""
    commits, after = [], None
    while True:
        data = github("graphql", {"query": COMMIT_QUERY, "variables": {"owner": ORG, "name": repo, "after": after}})
        if data.get("errors"):
            raise ValueError(str(data["errors"])[:200])
        branch = data["data"]["repository"]["defaultBranchRef"]
        if not branch:
            return commits  # an empty repo has no default branch yet
        history = branch["target"]["history"]
        commits += parse_api_commits(history["nodes"], repo, aliases)
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        after = history["pageInfo"]["endCursor"]


RECENT_COMMIT_QUERY = """query($owner: String!, $name: String!, $since: GitTimestamp!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name target { ... on Commit {
      history(first: 50, since: $since, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { oid message authoredDate additions deletions parents { totalCount } author { name email } }
      }
    } } }
  }
}"""


def parse_recent_commits(nodes, repo, aliases):
    """One commit each, with its exact time and lines. Only a recent window can ask GitHub for lines."""
    commits = []
    for node in nodes:
        for commit in parse_api_commits([node], repo, aliases):
            at = datetime.datetime.fromisoformat(node["authoredDate"].replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
            commits.append({**commit, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "ai": commit["claude"] or bool(AI_TRAILER_RE.search(node["message"]))})
    return commits


def fetch_recent_commits(repo, since, aliases):
    commits, after = [], None
    while True:
        data = github("graphql", {"query": RECENT_COMMIT_QUERY, "variables": {"owner": ORG, "name": repo, "since": since, "after": after}})
        if data.get("errors"):
            raise ValueError(str(data["errors"])[:200])
        branch = data["data"]["repository"]["defaultBranchRef"]
        if not branch:
            return commits
        history = branch["target"]["history"]
        commits += [{**c, "branch": branch["name"]} for c in parse_recent_commits(history["nodes"], repo, aliases)]
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        after = history["pageInfo"]["endCursor"]


BRANCH_LIST_QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name target { ... on Commit { oid committedDate } } }
    }
  }
}"""

BRANCH_HISTORY_QUERY = """query($owner: String!, $name: String!, $ref: String!, $since: GitTimestamp!, $after: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) { target { ... on Commit {
      history(first: 50, since: $since, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { oid message authoredDate additions deletions parents { totalCount } author { name email } }
      }
    } } }
  }
}"""


def graphql(query, **variables):
    data = github("graphql", {"query": query, "variables": {"owner": ORG, **variables}})
    if data.get("errors"):
        raise ValueError(str(data["errors"])[:200])
    return data["data"]["repository"]


# Matches the dashboard's list. Read first, so a commit on both develop and a feature branch counts as on develop.
SERVER_BRANCHES = {"main", "master", "develop", "development", "dev", "stage", "staging", "release"}


def active_branches(repo, since, merged_heads=frozenset()):
    """Branches other than the default with a commit in the window, so quiet ones cost nothing. A branch whose
    latest commit went in through a merged PR is skipped: a squash merge gives its work new commit IDs."""
    names, after = [], None
    while True:
        data = graphql(BRANCH_LIST_QUERY, name=repo, after=after)
        default = (data.get("defaultBranchRef") or {}).get("name")
        for node in data["refs"]["nodes"]:
            target = node.get("target") or {}
            server = node["name"].lower() in SERVER_BRANCHES
            if node["name"] != default and target.get("committedDate", "") >= since and (server or target.get("oid") not in merged_heads):
                names.append(node["name"])
        if not data["refs"]["pageInfo"]["hasNextPage"]:
            return sorted(names, key=lambda name: name.lower() not in SERVER_BRANCHES)
        after = data["refs"]["pageInfo"]["endCursor"]


def fetch_branch_commits(repo, since, aliases, known_shas, merged_heads=frozenset()):
    """Commits on other branches that aren't already on the default branch."""
    # shortcut: a page with nothing new ends a branch, to avoid re-reading the default branch's history. Work older than 50 merged-in commits can be missed on a first scan; later scans see new work.
    seen, commits = set(known_shas), []
    for branch in active_branches(repo, since, merged_heads):
        after = None
        while True:
            ref = graphql(BRANCH_HISTORY_QUERY, name=repo, ref=f"refs/heads/{branch}", since=since, after=after).get("ref")
            if not ref:
                break  # deleted since the list was read
            history = ref["target"]["history"]
            fresh = [n for n in history["nodes"] if n["oid"] not in seen]
            seen.update(n["oid"] for n in fresh)
            commits += [{**c, "branch": branch} for c in parse_recent_commits(fresh, repo, aliases)]
            if not fresh or not history["pageInfo"]["hasNextPage"]:
                break
            after = history["pageInfo"]["endCursor"]
    return commits


def commit_rows(commits, all_commits, on_default=True):
    who = display_names(all_commits)
    return [{"repo": f"{ORG}/{c['repo']}".lower(), "oid": c["sha"], "person": who(c), "authored_at": c["at"], "ai": c["ai"],
             "lines_added": c["added"], "lines_removed": c["deleted"], "on_default": on_default, "branch": c.get("branch")} for c in commits]


PR_QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $after, states: [OPEN, MERGED], orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number createdAt updatedAt mergedAt isDraft headRefName headRefOid
        author { __typename login }
        reviews(first: 30) { nodes { body author { __typename login } } }
      }
    }
  }
}"""


def is_greptile_notice(review):
    """Greptile's "trial has ended" comment is posted as a review but isn't one."""
    return review["author"]["login"].lower() == "greptile-apps" and "trial has ended" in (review.get("body") or "").lower()


def parse_prs(nodes, repo, aliases):
    """Keeps people's ready PRs, noting whether anyone besides the author reviewed each one."""
    rows = []
    for pr in nodes:
        author = pr.get("author") or {}
        if pr["isDraft"] or author.get("__typename") != "User":
            continue
        login = author["login"].lower()
        reviewers = [r["author"] for r in pr["reviews"]["nodes"] if r.get("author")
                     and r["author"]["login"].lower() != login
                     and not is_greptile_notice(r)]
        rows.append({
            "repo": f"{ORG}/{repo}".lower(), "number": pr["number"], "person": aliases.get(login, author["login"]),
            "created_at": pr["createdAt"], "merged_at": pr["mergedAt"],
            "human_review": any(r["__typename"] == "User" for r in reviewers), "any_review": bool(reviewers),
            "agent_review": None, "head_ref": pr.get("headRefName"), "head_oid": pr.get("headRefOid"),
        })
    return rows


def fetch_prs(repo, aliases):
    """PRs updated in the last year. Results are newest first, so stop at the first older page."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    rows, after = [], None
    while True:
        data = github("graphql", {"query": PR_QUERY, "variables": {"owner": ORG, "name": repo, "after": after}})
        if data.get("errors"):
            raise ValueError(str(data["errors"])[:200])
        page = data["data"]["repository"]["pullRequests"]
        nodes = [pr for pr in page["nodes"] if pr["updatedAt"] >= cutoff]
        rows += parse_prs(nodes, repo, aliases)
        if len(nodes) < len(page["nodes"]) or not page["pageInfo"]["hasNextPage"]:
            return rows
        after = page["pageInfo"]["endCursor"]


def post_admin(url, route, body):
    if not url.startswith("https://") and "//localhost" not in url and "//127.0.0.1" not in url:
        sys.exit("--upload must be an https:// URL")
    token = os.environ.get("TRACKER_ADMIN_TOKEN")
    if not token:
        sys.exit("Set TRACKER_ADMIN_TOKEN to upload")
    request = urllib.request.Request(
        url.rstrip("/") + route, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "pulse-tracker-git"},  # Cloudflare rejects urllib's default
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.status


def upload(url, rows, identities, pull_requests):
    status = post_admin(url, "/api/admin/git", {"rows": rows, "identities": identities, "pull_requests": pull_requests})
    print(f"Uploaded {len(rows)} rows to {url}: {status}")


def solidtime(path, token):
    return read_json_with_retries(urllib.request.Request(f"{SOLIDTIME_API}{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "pulse-tracker-git"}))


def solidtime_pages(path, token):
    items, page = [], 1
    while True:
        data = solidtime(f"{path}{'&' if '?' in path else '?'}page={page}", token)
        items += data["data"]
        if page >= (data.get("meta") or {}).get("last_page", 1):
            return items
        page += 1


def fetch_time_entries(org, start, end, token):
    entries, offset = [], 0
    while True:
        found = solidtime(f"/organizations/{org}/time-entries?start={start}&end={end}&limit=500&offset={offset}", token)["data"]
        entries += found
        if len(found) < 500:
            return entries
        offset += 500


def time_rows(entries, members, projects, aliases, known_people, project_repos, from_day):
    """Hours per person, project and IST day. Built field by field, so billable rates and descriptions can't
    get through. Only people the dashboard already covers, so non-developers' timesheets stay out.
    shortcut: an entry across midnight counts on the day it started; split it if overnight logging becomes common."""
    people = {m["user_id"]: aliases.get(m["email"].lower(), m["name"]) for m in members}
    names = {p["id"]: p["name"] for p in projects}
    seconds = collections.Counter()
    for entry in entries:
        day = ist_day(entry["start"])
        person = people.get(entry["user_id"])
        if entry["end"] and entry.get("type", "work") == "work" and day >= from_day and person in known_people:
            seconds[(day, person, names.get(entry["project_id"], "No project"))] += entry["duration"] or 0
    return [{"day": day, "person": person, "project": project, "seconds": total, "repos": project_repos.get(project, [])}
            for (day, person, project), total in sorted(seconds.items())]


def upload_time_log(url, days, known_people, aliases, token):
    now = datetime.datetime.now(datetime.timezone.utc)
    from_day = ist_day((now - datetime.timedelta(days=days)).isoformat())
    org = solidtime("/users/me/memberships", token)["data"][0]["organization"]["id"]
    # A day early and a day late, because the window is in UTC and rows are kept by IST day.
    start = (now - datetime.timedelta(days=days + 1)).strftime("%Y-%m-%dT00:00:00Z")
    end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    rows = time_rows(fetch_time_entries(org, start, end, token), solidtime_pages(f"/organizations/{org}/members", token),
                     solidtime_pages(f"/organizations/{org}/projects?archived=all", token), aliases, known_people,
                     json.loads(SOLIDTIME_PROJECTS.read_text(encoding="utf-8")), from_day)
    post_admin(url, "/api/admin/time", {"from": from_day, "rows": rows})
    print(f"Uploaded {len(rows)} time log rows from {from_day} to {url}")


def upload_commits(url, rows):
    for start in range(0, len(rows), COMMIT_BATCH):
        post_admin(url, "/api/admin/commits", {"commits": rows[start:start + COMMIT_BATCH]})
    print(f"Uploaded {len(rows)} commits to {url}")


def ist_day(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(IST).strftime("%Y-%m-%d")


def render(commits, failures, since):
    who = display_names(commits)
    total = aggregate(commits, lambda _: "all")["all"]
    bulk = [c["bulk"] for c in commits if c["bulk"]]
    sections = [
        table("By developer", ["Developer"], aggregate(commits, lambda c: (who(c),))),
        table("By repository", ["Repository"], aggregate(commits, lambda c: (c["repo"],))),
        table("Developer by repository", ["Developer", "Repository"],
              aggregate(commits, lambda c: (who(c), c["repo"]))),
        table("By month", ["Month"], aggregate(commits, lambda c: (c["date"][:7],)), sort_by_label=True),
    ]
    if failures:
        items = "".join(f"<li>{html.escape(n)}: {html.escape(e)}</li>" for n, e in failures.items())
        sections.insert(0, f"<h2>Repositories that could not be read</h2><ul>{items}</ul>")
    scope = f"since {since}" if since else "all history"
    return f"""<!doctype html><html lang=en><meta charset=utf-8><title>Pulse</title>
<style>
body{{font:14px system-ui;margin:2rem auto;max-width:72rem;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:.3rem .6rem;border-bottom:1px solid #ddd;text-align:left}}
.n{{text-align:right;font-variant-numeric:tabular-nums}} .note{{background:#f5f5f5;padding:.6rem 1rem}}
</style>
<h1>Pulse</h1>
<p>{total['commits']:,} commits, {scope}, default branches. Generated {ist_day(datetime.datetime.now(datetime.timezone.utc).isoformat())}.
{percent(total['claude_commits'], total['commits'])} of commits and
{percent(total['claude_added'], total['added'])} of added lines are in commits with a Claude trailer.</p>
<p class=note>"In Claude commits" counts every line of a commit that carries a Claude co-author trailer.
It is not lines Claude wrote: a commit can mix human and Claude work, and commits made without the
trailer count as human. Lockfiles, build output, design files and bot commits are excluded.
{len(bulk)} commits over {BULK_LINES:,} lines ({sum(bulk):,} lines added) are treated as bulk imports: they
count as commits but not as lines.</p>
{''.join(sections)}</html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active-days", type=int, default=90,
                        help="only repos pushed to within this many days; 0 for all")
    parser.add_argument("--since", default="", help="only commits authored on or after YYYY-MM-DD")
    parser.add_argument("--out", default="report.html")
    parser.add_argument("--from-api", action="store_true", help="read commits from GitHub's API, not clones: no disk "
                        "needed, but no line counts")
    parser.add_argument("--upload", metavar="URL", help="also replace the dashboard's git data at this URL")
    parser.add_argument("--commit-days", type=int, default=3, help="when uploading, refresh each commit's detail for "
                        "this many days back; older ones stay stored, so 3 is enough once they've been filled in")
    args = parser.parse_args()

    repos = list_repos(args.active_days)
    if not repos:
        sys.exit(f"No active repos visible in {ORG}. Check that the GitHub token can read the org's private repos.")
    aliases = load_aliases()
    if not args.from_api:
        CACHE.mkdir(parents=True, exist_ok=True)
    commits, failures = [], {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        read = (lambda r: fetch_commits(r["name"], aliases)) if args.from_api else (lambda r: parse_log(read_repo(r), r["name"], aliases))
        jobs = {pool.submit(read, r): r["name"] for r in repos}
        for job in concurrent.futures.as_completed(jobs):
            name = jobs[job]
            try:
                found = job.result()
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                failures[name] = str(getattr(e, "stderr", None) or e).strip()[:200]
                print(f"FAILED {name}: {failures[name]}", file=sys.stderr)
                continue
            commits += [c for c in found if c["date"] >= args.since]
            print(f"{name}: {len(found)} commits", file=sys.stderr)

    pathlib.Path(args.out).write_text(render(commits, failures, args.since), encoding="utf-8")
    print(f"{len(commits)} commits from {len(repos) - len(failures)}/{len(repos)} repos -> {args.out}")
    if args.upload:
        # A failed repo would vanish from a full replace, so keep the last good snapshot instead.
        if failures:
            sys.exit(f"Not uploading: could not read {', '.join(sorted(failures))}")
        pull_requests = [pr for repo in repos for pr in fetch_prs(repo["name"], aliases)]
        upload(args.upload, dashboard_rows(commits), dashboard_identities(commits), pull_requests)
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.commit_days)).strftime("%Y-%m-%dT00:00:00Z")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            recent = [c for found in pool.map(lambda r: fetch_recent_commits(r["name"], since, aliases), repos) for c in found]
        upload_commits(args.upload, commit_rows(recent, commits + recent))
        # Separate, because these only add detail: a failure here mustn't stop the rest of the upload.
        known_shas = {c["sha"] for c in recent}
        merged_heads = {pr["head_oid"] for pr in pull_requests if pr["merged_at"]}
        active = [r["name"] for r in repos if r["pushed_at"] >= since[:10]]
        branch_commits, branch_failures = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for name, job in [(name, pool.submit(fetch_branch_commits, name, since, aliases, known_shas, merged_heads)) for name in active]:
                try:
                    branch_commits += job.result()
                except (ValueError, OSError) as error:
                    branch_failures.append(f"{name}: {error}")
        upload_commits(args.upload, commit_rows(branch_commits, commits + recent + branch_commits, on_default=False))
        if os.environ.get("SOLIDTIME_TOKEN"):
            # Timesheets get filled in late and edited, so re-read a month of them every time.
            who = display_names(commits + recent)
            known = {who(c) for c in commits + recent} | set(aliases.values())
            upload_time_log(args.upload, max(args.commit_days, TIME_LOG_DAYS), known, aliases, os.environ["SOLIDTIME_TOKEN"])
        if branch_failures:
            sys.exit("Could not read branches in " + "; ".join(branch_failures))


if __name__ == "__main__":
    main()
