#!/usr/bin/env python3
"""Hourly AI-tool usage collector for Pulse. Python 3.9+, standard library only.

  python3 collect.py install           sign in with GitHub and start the hourly job
  python3 collect.py run --dry-run     print exactly what would be sent, send nothing
  python3 collect.py status            show the last run
  python3 collect.py capture-preview   write the prompts and responses that would be sent to a local file
  python3 collect.py doctor            explain skipped records, reading only and sending nothing
  python3 collect.py uninstall         remove the schedule, config and state

It reads local Claude Code and Codex logs and sends usage counts (the fields in ROW_FIELDS), with
GitHub repo names for every session; sessions with no repo count as "No repo".

It also sends your prompts, the AI's text responses and the commands and file paths its tools used,
from every folder and repo, including logs already on this machine (usually the last 30 days). Known
secret formats are removed on this machine first. The contents of files and the output of commands
are never sent. See exactly what would go with: collect.py capture-preview
"""
import argparse
import ast
import collections
import datetime
import hashlib
import json
import os
import pathlib
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

VERSION = "0.10.0"
PARSER_VERSION = 7  # bump when parsing changes, so every log is read again on the next run
DEFAULT_URL = "https://pulse.example.com"
GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
NO_REPO = "-/no-repo"
BATCH_ROWS = 500
TASK_NAME = "PulseTracker"
LAUNCHD_LABEL = "com.example.pulse-tracker"
CRON_TAG = "# pulse-tracker"

HOME_DIR = pathlib.Path(os.environ.get("TRACKER_HOME") or pathlib.Path.home() / ".pulse-tracker")
CONFIG_FILE = HOME_DIR / "config.json"
STATE_FILE = HOME_DIR / "state.json"
LAST_RUN_FILE = HOME_DIR / "last_run.json"
RUN_LOCK_FILE = HOME_DIR / "run.lock"
UPDATE_MAX_BYTES = 2_000_000
UPDATE_CHECK_SECONDS = 60

CAPTURE_BATCH_RECORDS = 100
CAPTURE_BATCH_CHARS = 300_000  # small enough for a Worker to handle within its CPU limit
CAPTURE_MAX_TEXT = 100_000
CAPTURE_MAX_COMMAND = 2_000
INSTALL_NOTICE = """This installs the Pulse AI usage tracker. Every hour it sends:
  - Usage counts: tool and its version, model, day, tokens, lines, repo name, how many prompts you typed,
    which 5-minute blocks of each day had AI activity or a prompt, and a hash of each working
    folder with whether it's a git worktree. Folder names aren't sent.
  - Your own commits from the repos it finds on this laptop, including client repos on any git
    host: repo name, commit ID, time, line counts, and whether it names an AI tool. Commit
    messages and code aren't sent.
  - Whether your team's harness is set up in the repos you've worked in: if its Claude plugin
    is on, the plugin's version, and if its git hooks are on. The contents of your settings and
    hooks aren't sent.
  - Your prompts, the AI's text responses, and the commands and file paths its tools used.
    This covers every folder and repo on this laptop, including personal ones, and includes
    conversations already in your logs, usually the last 30 days.
    Only the people your organisation names can read these, and Cloudflare account admins can
    technically access the storage. They're deleted after 90 days.

Not sent: the contents of files or the output of commands. Known secret formats are removed
on this machine before upload, but others can get through.

It updates itself from the same server when a new version ships. If a new version would change
this notice, it waits until you re-install, so what it sends can't grow without you seeing it.
"""

# Replaced on this machine before anything is uploaded. Most specific first.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("anthropic-key", re.compile(r"\bsk-ant-[\w-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})")),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("tracker-token", re.compile(r"\bslt_[0-9a-f]{20,}")),
    ("jwt", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")),
    ("bearer-token", re.compile(r"(?<=[Bb]earer )[A-Za-z0-9._~+/=-]{16,}")),
    ("connection-password", re.compile(r"(?<=://)[^\s:/@]+:[^\s@/]+(?=@)")),
    ("assigned-secret", re.compile(r"(?i)(?<![\w-])[\w-]{0,40}(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{8,}")),
]
INJECTED_TEXT = re.compile(r"<(system-reminder|environment_context|user_instructions|command-[a-z]+|local-command-[a-z]+)>.*?</\1>", re.S)

# The only fields that are ever uploaded. Rows are built from this list, not filtered down to it.
ROW_FIELDS = ("tool", "source", "session", "day", "model", "repo", "lines_added", "lines_removed",
              "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "active_slots", "prompted_slots", "prompts", "folders", "tool_version", "ist")
# The server rejects a whole batch over one bad value, and that laptop would then stop reporting, so these
# mirror worker/src/validate.js. ASCII because JavaScript's \w is, and Python's otherwise isn't.
TOOL_VERSION_RE = re.compile(r"^[\w.+-]{1,32}$", re.A)
SESSION_RE = re.compile(r"^[\w.-]{1,64}$", re.A)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_MODEL = 100
MAX_ONE_COUNT = 10 ** 12  # far above any real message, so a damaged value can't swamp the totals
MAX_TOTAL = 10 ** 13
RUN_LOCK_MAX_AGE = 6 * 3600
MAX_FOLDERS = 50
COMMIT_DAYS = 90
COMMIT_GIT_TIMEOUT = 60
MAX_COMMIT_ROOTS = 300
COMMIT_BATCH = 500
COMMIT_BULK_LINES = 20_000
HARNESS_PLUGIN = "harness@your-org-harness"  # your team's Claude Code plugin, as plugin@marketplace
HARNESS_HOOK_MARK = "blocks commits containing secrets"  # from the harness scanner; a repo's own hooks don't count
COMMIT_AI_RE = re.compile(r"^\s*(co-authored-by:.*(@anthropic\.com|claude|codex|openai|cursor|copilot|aider|gemini|devin)|.*generated (with|by) \[?(claude|codex|cursor|copilot|aider|gemini))", re.I | re.M)
# The same generated files the git upload leaves out of line counts.
COMMIT_NOISE_RE = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|\.next|Pods|coverage)/"
    r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|Podfile\.lock|Gemfile\.lock"
    r"|poetry\.lock|uv\.lock|Cargo\.lock|composer\.lock|go\.sum)$"
    r"|\.(min\.js|min\.css|map|snap|svg|pen|jsonl)$"
)
# A deleted worktree has no .git left to check. shortcut: only these common locations are recognised,
# so others count as ordinary folders; add a location if a tool's worktrees show up that way.
WORKTREE_PATH = re.compile(r"^.*?[/\\](?:\.claude[/\\]worktrees|\.codex[/\\]worktrees|\.treehouse|\.worktrees)[/\\][^/\\]+")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
REMOTE_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/\s]+@)github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.I,
)


OTHER_REMOTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://(?:[^@/]+@)?|[^@/\s]+@)([^/:\s]+)(?::\d+(?=/))?[:/](.+?)(?:\.git)?/?$", re.I)
REPO_PART_RE = re.compile(r"^[\w.-]+$", re.A)
# An SSH alias for GitHub, such as git@github-work:org/repo. shortcut: other aliases count as their own host, which only changes how the repo is named.
GITHUB_ALIAS_RE = re.compile(r"^github[\w-]*$", re.I)
WORKTREE_SUFFIX = re.compile(r"-[0-9a-f]{4,}$")


def parse_remote(url):
    """Return lowercase owner/repo, or None for a local path. Drops any embedded credentials. Other hosts keep
    their name in the owner, so a client's gitlab.com/your-org/app can't pass for our GitHub repo."""
    match = REMOTE_RE.match(url.strip())
    if match:
        return f"{match.group(1)}/{match.group(2)}".lower() if REPO_PART_RE.match(match.group(1)) and REPO_PART_RE.match(match.group(2)) else None
    match = OTHER_REMOTE_RE.match(url.strip())
    if not match:
        return None
    # Azure DevOps paths carry extra parts: v3/org/project/repo and org/project/_git/repo.
    parts = [part for part in match.group(2).split("/") if part and part not in ("_git", "v3")]
    host = match.group(1)
    if len(parts) < 2 or not all(REPO_PART_RE.match(part) for part in (host, *parts[-2:])):
        return None
    owner = parts[-2] if GITHUB_ALIAS_RE.match(host) else f"{host}-{parts[-2]}"
    return f"{owner}/{parts[-1]}".lower()


class RepoLookup:
    """Maps a session's working directory to its org/repo, remembering past answers because
    worktrees get deleted while their logs live on."""

    def __init__(self, known=None, org_repos=()):
        self.known = known if known is not None else {}
        self.names = {repo.split("/")[-1].lower(): repo.lower() for repo in org_repos}
        self.misses = {}
        self.learn_names()

    def learn_names(self):
        """Adds this laptop's live repos to the names deleted folders can match. Returns whether any were new."""
        before = len(self.names)
        for repo in self.known.values():
            self.names.setdefault(repo.split("/")[-1], repo)
        return len(self.names) > before

    def __call__(self, cwd, stats):
        """Return the repo, or NO_REPO after counting the record."""
        cwd = clean_folder(cwd, None)
        if cwd and cwd not in self.known and cwd not in self.misses:
            repo = self._read_remote(cwd) or self.match_deleted_folder(cwd)
            if repo:
                self.known[cwd] = repo
            else:
                self.misses[cwd] = True
        repo = self.known.get(cwd) if cwd else None
        if not repo:
            stats["skipped_unresolved"] += 1  # kept under this name; these now count as "No repo"
            return NO_REPO
        return repo

    def match_deleted_folder(self, cwd):
        """A deleted worktree has no remote left to read, so match its path against known repo names,
        deepest folder first: ~/.treehouse/ledger-159b41 and ~/ledger/.claude/worktrees/x are ledger.
        shortcut: a same-named folder from another project would be miscounted, and unknown repos land
        in "No repo"; read a transcript's recorded PR repo if this proves wrong."""
        if os.path.isdir(cwd):
            return None  # a folder that exists has spoken for itself: not a repo, or another org's
        for part in reversed(pathlib.PurePath(cwd).parts):
            repo = self.names.get(WORKTREE_SUFFIX.sub("", part.lower()))
            if repo:
                return repo
        return None

    @staticmethod
    def _read_remote(cwd):
        try:
            out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return parse_remote(out.stdout) if out.returncode == 0 else None


def clean_folder(value, current):
    return value if isinstance(value, str) and value and "\0" not in value else current


def clean_model(value, current):
    usable = isinstance(value, str) and 0 < len(value) <= MAX_MODEL and not value.startswith("<") and not CONTROL_RE.search(value)  # "<synthetic>" isn't a real model
    return value if usable else current


def new_totals():
    return {"lines_added": 0, "lines_removed": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "active_slots": 0, "prompted_slots": 0, "prompts": 0, "folders": (), "tool_version": None}


def git_root(cwd):
    path = pathlib.PurePath(cwd)
    for root in (path, *path.parents):
        if os.path.exists(os.path.join(root, ".git")):
            return str(root)
    return None


def folder_id(cwd):
    """A hash of the folder a session worked in, traced up to its repo root so subfolders don't count
    separately. Starts with w for a git worktree (its .git is a file) and m for anything else."""
    root = git_root(cwd)
    if root:
        return ("w" if os.path.isfile(os.path.join(root, ".git")) else "m") + hashlib.sha256(root.encode()).hexdigest()[:16]
    gone = WORKTREE_PATH.match(cwd)
    return ("w" if gone else "m") + hashlib.sha256((gone[0] if gone else cwd).encode()).hexdigest()[:16]


def new_activity():
    return {"active_slots": 0, "prompted_slots": 0, "prompts": 0, "tool_version": None}


def ist_minute(stamp):
    """A log timestamp in IST, or None if unreadable. shortcut: assumes UTC, which both tools write;
    read the offset if a tool ever logs local time."""
    match = STAMP_RE.match(str(stamp or ""))
    try:
        return datetime.datetime.strptime(match[0], "%Y-%m-%dT%H:%M") + IST_OFFSET if match else None
    except ValueError:
        return None


def ist_day(stamp):
    when = ist_minute(stamp)
    return when.strftime("%Y-%m-%d") if when else ""


def mark_activity(activity, stamp, cwd, prompted=False, version=None):
    """Slots are the 5-minute blocks of an IST day, as bits, so the dashboard can combine parallel sessions."""
    when = ist_minute(stamp)
    if not when:
        return
    slot = when.hour * 12 + when.minute // 5
    entry = activity[(when.strftime("%Y-%m-%d"), cwd)]
    entry["active_slots"] |= 1 << slot
    if isinstance(version, str) and TOOL_VERSION_RE.match(version):
        entry["tool_version"] = version
    if prompted:
        entry["prompted_slots"] |= 1 << slot
        entry["prompts"] += 1


def typed_by_a_person(text):
    text = INJECTED_TEXT.sub("", text or "").strip()
    return bool(text) and not text.startswith(("<", "[Request interrupted"))  # "<" is tool-generated, like task notifications


def attach_activity(totals, activity, lookup):
    """Activity goes on one row per day and repo, since it isn't split by model. A day and repo with no
    tokens has no row, so its activity is dropped."""
    rows = {}
    for key in totals:
        rows.setdefault((key[0], key[2]), key)
    for (day, cwd), seen in activity.items():
        key = rows.get((day, lookup(cwd, collections.Counter())))  # already counted once if the repo is unknown
        if key:
            row = totals[key]
            row["active_slots"] |= seen["active_slots"]
            row["prompted_slots"] |= seen["prompted_slots"]
            row["prompts"] += seen["prompts"]
            row["tool_version"] = seen["tool_version"] or row["tool_version"]
            if cwd:
                row["folders"] = (*row["folders"], folder_id(cwd))


def build_rows(tool, path, session, totals):
    source = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    rows = []
    for (day, model, repo), t in sorted(totals.items()):
        session_id = session if isinstance(session, str) and SESSION_RE.match(session) else source
        values = {"tool": tool, "source": source, "session": session_id, "day": day, "model": model, "repo": repo, "ist": True, **t}
        for field in ("lines_added", "lines_removed", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            values[field] = min(values[field], MAX_TOTAL)
        for field in ("active_slots", "prompted_slots"):
            values[field] = format(t[field], "x") if t[field] else None
        values["folders"] = ",".join(sorted(set(t["folders"]))[:MAX_FOLDERS]) or None
        rows.append({field: values[field] for field in ROW_FIELDS})
    return rows


def read_records(path, stats):
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                stats["bad_lines"] += 1
                continue
            if isinstance(record, dict):
                yield record


def edit_lines(result):
    """Lines added and removed by one Edit or Write result. Matches Claude's session totals to ~1%."""
    if not isinstance(result, dict) or "filePath" not in result:
        return 0, 0
    patch = result.get("structuredPatch")
    if isinstance(patch, list) and patch:
        hunks = [hunk.get("lines") for hunk in patch if isinstance(hunk, dict)]
        lines = [line for hunk in hunks if isinstance(hunk, list) for line in hunk if isinstance(line, str)]
        return sum(line.startswith("+") for line in lines), sum(line.startswith("-") for line in lines)
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return len(result["content"].splitlines()), 0
    return 0, 0


def as_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_ONE_COUNT else 0


def claude_rows(path, lookup, stats):
    totals = collections.defaultdict(new_totals)
    activity = collections.defaultdict(new_activity)
    replies = {}  # replies stream as repeats of one id, and the last repeat has the final counts
    session, cwd, model = None, None, "unknown"
    saw_assistant = False
    for record in read_records(path, stats):
        session = record.get("sessionId") or session
        cwd = clean_folder(record.get("cwd"), cwd)
        day = ist_day(record.get("timestamp"))
        kind = record.get("type")
        if kind in ("user", "assistant"):
            message = record.get("message")
            prompted = (kind == "user" and not record.get("isMeta") and not record.get("isSidechain") and not record.get("toolUseResult")
                        and isinstance(message, dict) and typed_by_a_person(block_text(message.get("content"), "text")))
            mark_activity(activity, record.get("timestamp"), cwd, prompted, record.get("version"))
        if kind == "assistant":
            saw_assistant = True
            message = record.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict) and message.get("id") and isinstance(message["id"], str):
                model = clean_model(message.get("model"), model)
                replies[message["id"]] = (day, model, cwd, message["usage"])
        elif kind == "user" and day:
            added, removed = edit_lines(record.get("toolUseResult"))
            if added or removed:
                repo = lookup(cwd, stats)
                if repo:
                    row = totals[(day, model, repo)]
                    row["lines_added"] += added
                    row["lines_removed"] += removed
    for day, reply_model, reply_cwd, usage in replies.values():
        repo = lookup(reply_cwd, stats) if day else None
        if repo:
            row = totals[(day, reply_model, repo)]
            row["input_tokens"] += as_count(usage.get("input_tokens"))
            row["output_tokens"] += as_count(usage.get("output_tokens"))
            row["cache_read_tokens"] += as_count(usage.get("cache_read_input_tokens"))
            row["cache_write_tokens"] += as_count(usage.get("cache_creation_input_tokens"))
    if saw_assistant and not replies:
        stats["unrecognized_files"] += 1  # the format changed: say so instead of reporting zeros
    attach_activity(totals, activity, lookup)
    return build_rows("claude-code", path, session, totals)


def codex_rows(path, lookup, stats):
    totals = collections.defaultdict(new_totals)
    activity = collections.defaultdict(new_activity)
    session, cwd, model, version = None, None, "unknown", None
    previous = {}
    saw_turn = saw_usage = False
    for record in read_records(path, stats):
        kind, payload = record.get("type"), record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind in ("event_msg", "response_item"):
            mark_activity(activity, record.get("timestamp"), cwd, kind == "event_msg" and payload.get("type") == "user_message", version)
        if kind == "session_meta":
            session, cwd, version = payload.get("id") or session, clean_folder(payload.get("cwd"), cwd), payload.get("cli_version") or version
        elif kind == "turn_context":
            saw_turn = True
            cwd, model = clean_folder(payload.get("cwd"), cwd), clean_model(payload.get("model"), model)
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(usage, dict):
                continue
            saw_usage = True
            # Differences of the running total, because last_token_usage repeats when an event is
            # re-emitted. Summing them matched the session's own total exactly on a real log.
            delta = {key: max(as_count(usage.get(key)) - previous.get(key, 0), 0)
                     for key in ("input_tokens", "cached_input_tokens", "output_tokens", "cache_write_input_tokens")}
            previous = {key: max(as_count(usage.get(key)), previous.get(key, 0)) for key in delta}
            day = ist_day(record.get("timestamp"))
            repo = lookup(cwd, stats) if day and any(delta.values()) else None
            if repo:
                row = totals[(day, model, repo)]
                # Codex counts cached input inside input_tokens; Claude reports it separately.
                row["input_tokens"] += max(delta["input_tokens"] - delta["cached_input_tokens"], 0)
                row["output_tokens"] += delta["output_tokens"]
                row["cache_read_tokens"] += delta["cached_input_tokens"]
                row["cache_write_tokens"] += delta["cache_write_input_tokens"]
    if saw_turn and not saw_usage:
        stats["unrecognized_files"] += 1
    attach_activity(totals, activity, lookup)
    rows = build_rows("codex", path, session, totals)
    for row in rows:  # Codex logs don't record line counts; null means unknown, not zero
        row["lines_added"] = row["lines_removed"] = None
    return rows


def log_sources():
    home = pathlib.Path.home()
    claude = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    codex = pathlib.Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions"
    return [(claude, claude_rows), (codex, codex_rows)]


def collect(state, org_repos=()):
    """Parse only logs changed since the last run. Rows are absolute, so re-sending one is harmless."""
    if state.get("parser") != PARSER_VERSION:
        state["files"] = {}
        state["parser"] = PARSER_VERSION
    lookup = RepoLookup(state.setdefault("repos", {}), org_repos)
    seen = state.setdefault("files", {})
    results, marks, errors = {}, {}, 0

    def parse(path, parser):
        counts = collections.Counter()
        results[path] = (parser, parser(path, lookup, counts), counts)

    for root, parser in log_sources():
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                info = path.stat()
                mark = [info.st_mtime_ns, info.st_size]
                if seen.get(str(path)) == mark:
                    continue
                parse(path, parser)
            except Exception:  # any bug or damaged file: skip it, retry it next run, and keep reporting the rest
                errors += 1
                continue
            marks[str(path)] = mark
    # A deleted folder can match a live repo this same run only found later, so look again.
    if lookup.learn_names() and any(counts["skipped_unresolved"] for _, _, counts in results.values()):
        lookup.misses.clear()
        for path, (parser, _, counts) in list(results.items()):
            if counts["skipped_unresolved"]:
                try:
                    parse(path, parser)
                except Exception:
                    errors += 1
    stats, rows = collections.Counter(parse_errors=errors, files_parsed=len(results)), []
    for _, file_rows, counts in results.values():
        rows += file_rows
        stats.update(counts)
    return rows, marks, stats


def redact(text, salt):
    """Removes known secret formats. Findings carry a salted fingerprint, so repeats can be recognised
    without the secret leaving this machine. shortcut: patterns miss secrets with no recognisable
    format, so the weekly findings review is a second pass and not a guarantee."""
    findings = []

    def replacer(kind):
        def replace(match):
            digest = hashlib.sha256((salt + match.group(0)).encode()).hexdigest()[:16]
            findings.append({"type": kind, "fingerprint": digest})
            return f"[REDACTED:{kind}]"
        return replace

    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(replacer(kind), text)
    return text, findings


def block_text(content, *kinds):
    """The written text of a message, ignoring tool calls, tool results and images."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") in kinds and isinstance(b.get("text"), str))


def tool_line(name, *details):
    return " ".join([str(name), *[str(d) for d in details if d]])[:CAPTURE_MAX_COMMAND]


def claude_tool_lines(content):
    """What a tool was asked to do: the command or path, never file contents or edits."""
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            given = block.get("input") if isinstance(block.get("input"), dict) else {}
            yield tool_line(block.get("name"), given.get("command"), given.get("file_path"), given.get("notebook_path"), given.get("path"))


def claude_messages(path, stats):
    """(session, cwd, timestamp, role, agent, text). agent marks subagents, as opposed to the main conversation."""
    for record in read_records(path, stats):
        kind = record.get("type")
        if kind not in ("user", "assistant") or record.get("isMeta") or record.get("toolUseResult"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        base = (record.get("sessionId"), record.get("cwd"), record.get("timestamp"))
        agent = bool(record.get("isSidechain"))
        yield (*base, "prompt" if kind == "user" else "response", agent, block_text(message.get("content"), "text"))
        if kind == "assistant":
            for line in claude_tool_lines(message.get("content")):
                yield (*base, "tool", agent, line)


def codex_tool_line(payload):
    name = payload.get("name")
    raw = payload.get("arguments") if payload.get("type") == "function_call" else payload.get("input")
    if name == "apply_patch":  # the patch holds file contents, so keep only which files it touched
        return tool_line(name, " ".join(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", str(raw or ""), re.M)))
    try:
        given = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return tool_line(name, raw)
    if not isinstance(given, dict):
        return tool_line(name, raw if isinstance(raw, str) else None)
    command = given.get("command") or given.get("cmd")
    return tool_line(name, " ".join(map(str, command)) if isinstance(command, list) else command, given.get("path"))


def codex_messages(path, stats):
    session, cwd = None, None
    for record in read_records(path, stats):
        kind, payload = record.get("type"), record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta":
            session, cwd = payload.get("id") or session, payload.get("cwd") or cwd
        elif kind == "turn_context":
            cwd = payload.get("cwd") or cwd
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
            yield (session, cwd, record.get("timestamp"), "prompt" if payload["role"] == "user" else "response", False,
                   block_text(payload.get("content"), "input_text", "output_text", "text"))
        elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            yield (session, cwd, record.get("timestamp"), "tool", False, codex_tool_line(payload))


def capture_records(path, tool, reader, since, salt, repo_of, after=0):
    """Records to upload, and the position reached. Positions count every message whatever the filters
    decide, so a later run resumes in the right place."""
    records, position = [], 0
    for session, cwd, stamp, role, agent, text in reader(path, collections.Counter()):
        text = INJECTED_TEXT.sub("", text or "").strip()
        if not text:
            continue
        position += 1
        # A time the server couldn't read would fail the whole batch, and it couldn't be placed on a day anyway.
        if position <= after or not ist_minute(stamp) or len(stamp) > 40 or CONTROL_RE.search(stamp) or stamp < since:
            continue
        clean, findings = redact(text, salt)
        records.append({"tool": tool, "session": session if isinstance(session, str) and SESSION_RE.match(session) else "unknown", "at": stamp, "role": role, "agent": agent,
                        "repo": repo_of(cwd), "text": clean[:CAPTURE_MAX_TEXT], "truncated": len(clean) > CAPTURE_MAX_TEXT, "findings": findings})
    return records, position


def capture_sources():
    (claude, _), (codex, _) = log_sources()
    return [(claude, "claude-code", claude_messages), (codex, "codex", codex_messages)]


def capture_files(state, since, salt, org_repos=()):
    """Yields (path, new records, position) per changed log, so progress can be saved file by file:
    the first upload is a month of history and a dropped connection mustn't restart it."""
    sent = state.setdefault("capture", {})
    lookup = RepoLookup(state.setdefault("repos", {}), org_repos)
    repo_of = lambda cwd: lookup(cwd, collections.Counter())
    cutoff = datetime_from(since) if since else 0
    for root, tool, reader in capture_sources():
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                info = path.stat()
                mark = sent.get(str(path), [None, 0])
                if info.st_mtime < cutoff or mark[0] == info.st_size:
                    continue
                found, position = capture_records(path, tool, reader, since, salt, repo_of, mark[1])
            except Exception:  # as in collect(): one bad file mustn't stop the others
                continue
            yield str(path), found, [info.st_size, position]


def datetime_from(stamp):
    return time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone


def capture_batches(records):
    batch, size = [], 0
    for record in records:
        if batch and (len(batch) >= CAPTURE_BATCH_RECORDS or size + len(record["text"]) > CAPTURE_BATCH_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += len(record["text"])
    if batch:
        yield batch


def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise SystemExit(f"Server URL must start with https:// (got {url})")
    return url.rstrip("/")


def post(config, payload, route="/api/ingest"):
    request = urllib.request.Request(
        config["url"] + route,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json",
                 "User-Agent": f"pulse-tracker/{VERSION}"},  # Cloudflare rejects urllib's default
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_org_repos(config):
    request = urllib.request.Request(config["url"] + "/api/repos", headers={
        "Authorization": "Bearer " + config["token"], "User-Agent": f"pulse-tracker/{VERSION}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["repos"]


def git_output(root, *args):
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=COMMIT_GIT_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def commit_roots(known):
    """Repos sessions ran in, plus folders beside them, so a repo worked on without AI is still found."""
    roots = {root for root in map(git_root, known) if root}
    for parent in {os.path.dirname(root) for root in roots}:
        try:
            with os.scandir(parent) as entries:
                roots.update(e.path for e in entries if e.is_dir() and os.path.exists(os.path.join(e.path, ".git")))
        except OSError:
            continue
    return sorted(roots)[:MAX_COMMIT_ROOTS]


def parse_commit_log(text, emails, repo, on_default):
    """Rows built field by field: the message is only read for an AI mention, and never leaves this function."""
    rows = {}
    for record in text.split("\x1e")[1:]:
        fields = record.split("\x1f")
        if len(fields) != 5:
            continue  # a message containing these separators; losing one commit beats losing every commit
        oid, stamp, email, body, numstat = fields
        if email.strip().lower() not in emails:
            continue  # --author matches parts of an address, so check the whole of it
        added = removed = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and not COMMIT_NOISE_RE.search(parts[2].rstrip("}")):
                added += int(parts[0])
                removed += int(parts[1])
        if max(added, removed) > COMMIT_BULK_LINES:
            added = removed = 0  # a bulk import, not work
        at = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
        rows[oid] = {"repo": repo, "oid": oid, "authored_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "ai": bool(COMMIT_AI_RE.search(body)),
                     "lines_added": added, "lines_removed": removed, "on_default": oid in on_default}
    return rows


def local_commits(known):
    """This person's own commits, from every branch, in the repos on this laptop."""
    roots = commit_roots(known)
    emails = {email.strip().lower() for email in (git_output(root, "config", "user.email") for root in roots) if email and email.strip()}
    commits = {}
    if not emails:
        return []
    since = f"--since={COMMIT_DAYS}.days.ago"
    for root in roots:
        repo = RepoLookup._read_remote(root)
        if not repo:
            continue
        text = git_output(root, "log", "--all", "--no-merges", since, "-F", *[f"--author={email}" for email in sorted(emails)],
                          "--format=%x1e%H%x1f%aI%x1f%ae%x1f%B%x1f", "--numstat", "--no-renames")
        if not text:
            continue
        default = next((ref for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master")
                        if git_output(root, "rev-parse", "--verify", "-q", ref) is not None), None)
        merged = set((git_output(root, "rev-list", default, since) or "").split()) if default else set()
        for oid, row in parse_commit_log(text, emails, repo, merged).items():
            key = (repo, oid)
            # Clones and worktrees of one repo can disagree about what's merged; a yes from any of them counts.
            commits[key] = {**row, "on_default": row["on_default"] or commits.get(key, row)["on_default"]}
    return list(commits.values())


def json_object(path):
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def harness_plugin(root, claude_dir):
    """The harness version active in this repo, or None. The settings closest to the repo win, as in Claude Code.
    shortcut: a plugin installed for one project isn't seen from that project's worktrees; compare git common dirs if anyone installs it that way."""
    installs = json_object(claude_dir / "plugins" / "installed_plugins.json").get("plugins")
    installs = installs.get(HARNESS_PLUGIN) if isinstance(installs, dict) else None
    active = None
    for entry in installs if isinstance(installs, list) else []:
        if isinstance(entry, dict) and (entry.get("scope") == "user" or (
                isinstance(entry.get("projectPath"), str) and os.path.realpath(entry["projectPath"]) == os.path.realpath(root))):
            active = entry
            break
    if not active:
        return None
    version = active.get("version")
    version = version if isinstance(version, str) and TOOL_VERSION_RE.match(version) else "unknown"
    for settings in (pathlib.Path(root, ".claude", "settings.local.json"), pathlib.Path(root, ".claude", "settings.json"), claude_dir / "settings.json"):
        enabled = json_object(settings).get("enabledPlugins")
        if isinstance(enabled, dict) and HARNESS_PLUGIN in enabled:
            return version if enabled[HARNESS_PLUGIN] is True else None
    return None


def harness_hooks(root):
    """Whether git would run the harness scanner here. Asks git for the folder, so it covers hooksPath and worktrees."""
    folder = (git_output(root, "rev-parse", "--git-path", "hooks") or "").strip()
    if not folder:
        return False
    hooks = pathlib.Path(root, folder)
    if not os.access(hooks / "pre-commit", os.X_OK):
        return False  # git skips a hook it can't run
    for name in ("pre-commit", "secret-scanner"):  # some repos call the scanner from their own pre-commit
        try:
            if HARNESS_HOOK_MARK in (hooks / name).read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def harness_rows(known, commits, claude_dir=None):
    """Only repos this person has worked in, so other folders on the laptop aren't named."""
    claude_dir = claude_dir or pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
    worked = set(known.values()) | {c["repo"] for c in commits}
    rows = {}
    for root in commit_roots(known):
        repo = RepoLookup._read_remote(root)
        if repo not in worked:
            continue
        version = harness_plugin(root, claude_dir)
        hooks = harness_hooks(root)
        best = rows.get(repo)
        # Clones and worktrees of one repo can differ; a yes from any of them counts, as with commits.
        rows[repo] = {"repo": repo, "plugin": bool(version) or bool(best and best["plugin"]),
                      "plugin_version": version or (best["plugin_version"] if best else None),
                      "hooks": hooks or bool(best and best["hooks"])}
    return sorted(rows.values(), key=lambda r: r["repo"])


def changed_commits(commits, sent):
    """Only repos whose commits changed since the last send, because most hours none do."""
    by_repo = collections.defaultdict(list)
    for row in commits:
        by_repo[row["repo"]].append(row)
    rows, digests = [], {}
    for repo, repo_rows in by_repo.items():
        digest = hashlib.sha256(json.dumps(sorted(repo_rows, key=lambda r: r["oid"]), sort_keys=True).encode()).hexdigest()[:16]
        if sent.get(repo) != digest:
            rows += repo_rows
            digests[repo] = digest
    return rows, digests


def payload_for(rows, stats):
    return {"version": VERSION, "machine": platform.node()[:100] or "unknown", "os": sys.platform,
            "stats": {k: stats[k] for k in ("files_parsed", "parse_errors", "bad_lines", "unrecognized_files",
                                            "skipped_unresolved", "skipped_other_org")},
            "rows": rows}


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_config():
    config = read_json(CONFIG_FILE, None)
    usable = isinstance(config, dict) and isinstance(config.get("url"), str) and isinstance(config.get("token"), str)
    return config if usable else None


def read_state():
    """A damaged state file only costs a re-read, and rows are absolute, so re-sending is harmless."""
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return {}
    for key in ("files", "repos", "capture", "commits"):
        if not isinstance(state.get(key, {}), dict):
            del state[key]
    state["repos"] = {cwd: repo for cwd, repo in state.get("repos", {}).items() if isinstance(repo, str) and "/" in repo}
    state["capture"] = {path: mark for path, mark in state.get("capture", {}).items() if isinstance(mark, list) and len(mark) == 2}
    return state


def take_run_lock():
    """Hourly runs can overlap a long first upload or a hand-started run. Two at once would each undo the
    other's saved progress. A lock left by a crash expires, so it can't stop reporting for good."""
    for _ in range(2):
        try:
            os.close(os.open(RUN_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - RUN_LOCK_FILE.stat().st_mtime < RUN_LOCK_MAX_AGE:
                    return False
                RUN_LOCK_FILE.unlink()
            except FileNotFoundError:
                pass  # the other run just finished, so try again
    return False


def write_json(path, value):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)  # a crash mid-write must not lose the state or config


def version_key(text):
    return tuple(int(part) for part in text.split(".")) if isinstance(text, str) and re.fullmatch(r"\d+(\.\d+)*", text, re.A) else None


def script_constants(source):
    """Reads a downloaded script's constants without running any of it."""
    constants = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and getattr(node.targets[0], "id", None) in ("VERSION", "INSTALL_NOTICE"):
            constants[node.targets[0].id] = ast.literal_eval(node.value)
    return constants


def self_update(config):
    """Replaces the installed script with a newer one, which takes over on the next run. Returns what happened.
    Anything wrong with the download leaves the working copy alone."""
    script = pathlib.Path(__file__).resolve()
    if script != (HOME_DIR / "collect.py").resolve():
        return "not the installed copy"
    request = urllib.request.Request(validate_url(config["url"]) + "/collect.py", headers={"User-Agent": f"pulse-tracker/{VERSION}"})
    with urllib.request.urlopen(request, timeout=UPDATE_CHECK_SECONDS) as response:
        body = response.read(UPDATE_MAX_BYTES + 1)
    if len(body) > UPDATE_MAX_BYTES:
        return "download too large"
    source = body.decode("utf-8")
    found = script_constants(source)
    latest, current = version_key(found.get("VERSION")), version_key(VERSION)
    if not latest or latest <= current:
        return "up to date"
    # What a person agreed to at install is in this notice, so a change to it needs them to see it.
    if found.get("INSTALL_NOTICE") != INSTALL_NOTICE:
        return f"{found['VERSION']} changes what's collected, so it waits for a re-install"
    with tempfile.TemporaryDirectory() as empty:
        candidate = HOME_DIR / f"collect.{os.getpid()}.new"
        try:
            candidate.write_bytes(body)
            # A separate tracker folder, so the new version can't touch real state while being checked.
            check = subprocess.run([sys.executable, str(candidate), "--help"], env={**os.environ, "TRACKER_HOME": empty},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=UPDATE_CHECK_SECONDS, check=False)
            if check.returncode != 0:
                return f"{found['VERSION']} failed its start-up check"
            shutil.copymode(script, candidate)
            os.replace(candidate, script)
        finally:
            try:
                candidate.unlink()
            except OSError:
                pass
    return f"updated to {found['VERSION']}"


def try_self_update(config):
    # First, and never fatal, so a version with a bug in sending can still be replaced by a fixed one.
    try:
        print(f"Update check: {self_update(config)}")
    except (Exception, SystemExit) as error:
        print(f"Update check failed, will retry next run: {error}", file=sys.stderr)


def cmd_run(args):
    config = read_config()
    if args.dry_run:
        state = {}  # ignore saved progress so the preview shows everything
        rows, _, stats = collect(state, fetch_org_repos(config) if config else ())
        commits = local_commits(state["repos"])
        print(json.dumps({**payload_for(rows, stats), "commits": commits, "harness": harness_rows(state["repos"], commits)}, indent=1))
        print(f"\nDry run: {len(rows)} rows and {len(commits)} commits, nothing sent.", file=sys.stderr)
        return 0
    if not config:
        raise SystemExit("Not installed. Run: collect.py install")
    if not take_run_lock():
        print("Another run is in progress, so this one is leaving it to finish.")
        return 0
    try:
        try_self_update(config)
        return send_everything(config)
    finally:
        try:
            RUN_LOCK_FILE.unlink()
        except OSError:
            pass


def send_everything(config):
    state = read_state()
    started = int(time.time())
    try:
        # Without the repo list, deleted worktrees would land in "No repo" and be marked as done.
        rows, marks, stats = collect(state, fetch_org_repos(config))
        for start in range(0, max(len(rows), 1), BATCH_ROWS):  # an empty run still reports in
            post(config, payload_for(rows[start:start + BATCH_ROWS], stats))
    except (OSError, ValueError) as error:  # URLError and HTTPError are OSErrors
        write_json(LAST_RUN_FILE, {"at": started, "ok": False, "error": str(error)[:300]})
        print(f"Upload failed, will retry next run: {error}", file=sys.stderr)
        return 1
    state["files"].update(marks)
    write_json(STATE_FILE, state)
    # Its own step, so a repo git can't read or a failed send never holds up usage or text.
    sent_commits = 0
    found = []
    try:
        found = local_commits(state["repos"])
        commits, digests = changed_commits(found, state.setdefault("commits", {}))
        for start in range(0, len(commits), COMMIT_BATCH):
            post(config, {"commits": commits[start:start + COMMIT_BATCH]}, "/api/commits")
        sent_commits = len(commits)
        state["commits"].update(digests)
        write_json(STATE_FILE, state)
    except (OSError, ValueError) as error:
        print(f"Commit upload failed, will retry next run: {error}", file=sys.stderr)
    try:
        harness = harness_rows(state["repos"], found)
        digest = hashlib.sha256(json.dumps(harness, sort_keys=True).encode()).hexdigest()[:16]
        if state.get("harness") != digest:  # most hours nothing changed
            post(config, {"machine": platform.node()[:100] or "unknown", "repos": harness}, "/api/harness")
            state["harness"] = digest
            write_json(STATE_FILE, state)
    except (OSError, ValueError) as error:
        print(f"Harness status upload failed, will retry next run: {error}", file=sys.stderr)
    captured = 0
    if config.get("capture"):  # only set by installing this version, which shows what gets sent
        try:
            for path, records, mark in capture_files(state, "", config.get("salt") or "", fetch_org_repos(config)):
                os.utime(RUN_LOCK_FILE)  # a long first upload is still alive, so its lock isn't stale
                for batch in capture_batches(records):
                    post(config, {"machine": platform.node()[:100] or "unknown", "records": batch}, "/api/capture")
                    captured += len(batch)
                state["capture"][path] = mark
                write_json(STATE_FILE, state)
        except (OSError, ValueError) as error:
            # The current file's position wasn't saved, so the next run re-sends it; identical batches overwrite.
            write_json(LAST_RUN_FILE, {"at": started, "ok": False, "rows": len(rows), "error": f"capture: {error}"[:300]})
            print(f"Text upload failed, will retry next run: {error}", file=sys.stderr)
            return 1
    write_json(LAST_RUN_FILE, {"at": started, "ok": True, "rows": len(rows), "captured": captured, **stats})
    print(f"Sent {len(rows)} rows from {stats['files_parsed']} changed log files, {sent_commits} commits and {captured} messages.")
    return 0


def cmd_capture_preview(args):
    """Writes what would be sent to a local file, from any date, and uploads nothing."""
    config = read_config() or {}
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.hours * 3600))
    repos = fetch_org_repos(config) if config.get("token") else ()
    records = [r for _, found, _ in capture_files({}, since, config.get("salt") or "preview", repos) for r in found]
    out = pathlib.Path(args.out)
    out.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    found = collections.Counter(f["type"] for r in records for f in r["findings"])
    print(f"{len(records)} messages from the last {args.hours} hours written to {out}. Nothing was sent.")
    print("By repo:", dict(collections.Counter(r["repo"] for r in records).most_common(12)))
    print("By role:", dict(collections.Counter(r["role"] for r in records)), "| from subagents:", sum(r["agent"] for r in records))
    print(f"Size: {sum(len(r['text']) for r in records) / 1e6:.1f} MB of text")
    print("Secrets removed:", dict(found) or "none")
    return 0


def schedule_plan(system, python, script, home):
    """What to write and run to schedule hourly runs. Pure, so all three OSes can be tested anywhere.
    Each command is (argv, must_succeed)."""
    home = pathlib.PurePosixPath(home) if system != "Windows" else home
    if system == "Darwin":
        path = home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        domain = f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}"
        plist = plistlib.dumps({"Label": LAUNCHD_LABEL, "ProgramArguments": [python, script, "run"],
                                "StartInterval": 3600, "RunAtLoad": True, "ProcessType": "Background"})
        return {"files": {str(path): plist.decode()},
                "setup": [(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], False),
                          (["launchctl", "bootstrap", domain, str(path)], True)],
                "teardown": [(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], False)]}
    if system == "Linux":
        unit_dir = home / ".config/systemd/user"
        service = f'[Unit]\nDescription=Pulse tracker\n\n[Service]\nType=oneshot\nExecStart="{python}" "{script}" run\n'
        timer = ("[Unit]\nDescription=Run the Pulse tracker hourly\n\n[Timer]\nOnBootSec=5min\n"
                 "OnUnitActiveSec=1h\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        return {"files": {str(unit_dir / "pulse-tracker.service"): service,
                          str(unit_dir / "pulse-tracker.timer"): timer},
                "setup": [(["systemctl", "--user", "daemon-reload"], True),
                          (["systemctl", "--user", "enable", "--now", "pulse-tracker.timer"], True)],
                "teardown": [(["systemctl", "--user", "disable", "--now", "pulse-tracker.timer"], False)]}
    if system == "Windows":
        return {"files": {},
                "setup": [(["schtasks", "/Create", "/F", "/SC", "HOURLY", "/TN", TASK_NAME,
                            "/TR", f'"{python}" "{script}" run'], True),
                          # Windows' default skips the task on battery, which is how laptops are often used. Also catch up after sleep.
                          (["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Set-ScheduledTask -TaskName {TASK_NAME} -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable)"], False),
                          # An hourly task first fires an hour from now; launchd runs at load, so match it.
                          (["schtasks", "/Run", "/TN", TASK_NAME], False)],
                "teardown": [(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], False)]}
    raise SystemExit(f"Unsupported system: {system}")


def cron_with(existing, line):
    """Crontab text with our entry replaced, or removed when line is None. Others' entries stay."""
    kept = [entry for entry in existing.splitlines() if CRON_TAG not in entry]
    return "\n".join(kept + ([f"{line} {CRON_TAG}"] if line else [])) + "\n"


def update_crontab(line):
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = current.stdout if current.returncode == 0 else ""  # "no crontab" exits non-zero
    subprocess.run(["crontab", "-"], input=cron_with(existing, line), text=True, check=True)


def use_cron():
    """Containers and minimal Linux systems have no user systemd."""
    if platform.system() != "Linux":
        return False
    if not shutil.which("systemctl"):
        return True
    return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode != 0


def background_python():
    """pythonw has no console window, so Windows users don't see a flash every hour."""
    if platform.system() == "Windows":
        windowless = pathlib.Path(sys.executable).with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return sys.executable


def apply_plan(steps):
    for argv, must_succeed in steps:
        subprocess.run(argv, check=must_succeed, capture_output=True)


def request_json(url, data=None, headers=None):
    """GETs, or POSTs when data is given."""
    request = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json", "User-Agent": f"pulse-tracker/{VERSION}", **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def github_sign_in(client_id, wait=time.sleep):
    """GitHub's device flow: the developer approves in a browser and no secret ships in this script.
    Returns a short-lived sign-in that's handed to the server once and never saved."""
    start = request_json(GITHUB_DEVICE_URL, urllib.parse.urlencode(
        {"client_id": client_id, "scope": "read:org user:email"}).encode())
    print(f"\nSign in with GitHub: open {start['verification_uri']} and enter this code:\n\n    {start['user_code']}\n")
    try:
        webbrowser.open(start["verification_uri"])
    except webbrowser.Error:
        pass  # headless machine: the printed link is enough
    interval, deadline = start.get("interval", 5), time.time() + start.get("expires_in", 900)
    poll = urllib.parse.urlencode({"client_id": client_id, "device_code": start["device_code"],
                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}).encode()
    while time.time() < deadline:
        wait(interval)
        answer = request_json(GITHUB_TOKEN_URL, poll)
        if answer.get("access_token"):
            return answer["access_token"]
        error = answer.get("error")
        if error == "slow_down":
            interval = answer.get("interval", interval + 5)
        elif error != "authorization_pending":
            raise SystemExit(f"GitHub sign-in stopped: {answer.get('error_description') or error}")
    raise SystemExit("The GitHub code expired. Run the install again.")


def server_error(error):
    try:
        return json.load(error).get("error") or str(error)
    except ValueError:
        return str(error)


def enroll(url):
    """Trades a GitHub sign-in for this laptop's own tracker token."""
    try:
        client_id = request_json(url + "/api/enroll/config")["client_id"]
        github_token = github_sign_in(client_id)
        result = request_json(url + "/api/enroll", json.dumps({"github_token": github_token}).encode(),
                              {"Content-Type": "application/json"})
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Could not sign you in: {server_error(error)}")
    except OSError as error:
        raise SystemExit(f"Could not reach the tracker or GitHub: {error}")
    print(f"Signed in as {result['name']}.")
    return result["token"]


def schedule_with_cron(python, script):
    try:
        update_crontab(f'0 * * * * "{python}" "{script}" run >/dev/null 2>&1')
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Could not schedule the collector: {error}")
    return "cron"


def schedule(python, script):
    if use_cron():
        return schedule_with_cron(python, script)
    plan = schedule_plan(platform.system(), python, str(script), str(pathlib.Path.home()))
    for path, content in plan["files"].items():
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(content, encoding="utf-8")
    try:
        apply_plan(plan["setup"])
    except (OSError, subprocess.CalledProcessError) as error:
        if platform.system() == "Linux":  # systemctl can exist and still refuse, as in some containers
            return schedule_with_cron(python, script)
        raise SystemExit(f"Could not schedule the collector: {error}")
    return {"Darwin": "launchd", "Linux": "systemd", "Windows": "Task Scheduler"}[platform.system()]


def cmd_install(args):
    url = validate_url(args.url)
    print(INSTALL_NOTICE)
    # TRACKER_TOKEN is for machines that can't open a browser; everyone else signs in with GitHub.
    token = os.environ.get("TRACKER_TOKEN") or enroll(url)
    previous = read_config() or {}
    config = {"url": url, "token": token,
              "capture": True,
              "salt": previous.get("salt") or secrets.token_hex(16)}  # kept, so fingerprints stay comparable
    try:
        post(config, payload_for([], collections.Counter()))  # catch a bad URL or token before scheduling
    except urllib.error.HTTPError as error:
        raise SystemExit(f"The server rejected this token ({error.code}). Ask for a new one.")
    except OSError as error:
        raise SystemExit(f"Could not reach {url}: {error}")

    HOME_DIR.mkdir(parents=True, exist_ok=True)
    write_json(CONFIG_FILE, config)
    os.chmod(CONFIG_FILE, 0o600)  # holds the token
    script = HOME_DIR / "collect.py"
    if pathlib.Path(__file__).resolve() != script.resolve():
        shutil.copyfile(__file__, script)

    scheduler = schedule(background_python(), script)
    print(f"Installed in {HOME_DIR}, running hourly via {scheduler}.")
    print(f"Preview what gets sent: {sys.executable} {script} run --dry-run")
    return 0


def cmd_uninstall(_args):
    if platform.system() == "Linux" and shutil.which("crontab"):  # either scheduler may have been used, whichever works now
        try:
            update_crontab(None)
        except (OSError, subprocess.CalledProcessError):
            pass
    if not use_cron():
        plan = schedule_plan(platform.system(), background_python(), str(HOME_DIR / "collect.py"),
                             str(pathlib.Path.home()))
        apply_plan(plan["teardown"])
        for path in plan["files"]:
            pathlib.Path(path).unlink(missing_ok=True)
    shutil.rmtree(HOME_DIR, ignore_errors=True)
    print("Removed the schedule, config and state. Data already sent stays on the server.")
    return 0


def cmd_status(_args):
    config = read_config()
    if not config:
        print("Not installed.")
        return 1
    print(f"Server: {config['url']}")
    print("Last run:", json.dumps(read_json(LAST_RUN_FILE, "none yet")))
    return 0


def why_skipped(cwd, lookup=None):
    """Why a session's folder does or doesn't map to a repo. Never names the repo."""
    if not cwd:
        return "no folder recorded"
    if not os.path.isdir(cwd):
        return "folder deleted, matched by name" if lookup and lookup.match_deleted_folder(cwd) else "folder no longer exists"
    try:
        out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return f"git could not run: {type(error).__name__}"
    if out.returncode:
        if "not permitted" in out.stderr.lower():
            return "blocked by macOS privacy settings"
        return "not a git repo" if "not a git repository" in out.stderr else "git repo with no origin remote"
    if parse_remote(out.stdout):
        return "ok"
    # Host only, with any embedded credentials removed: an SSH alias shows up here.
    host = re.sub(r"^[a-z+]+://", "", out.stdout.strip(), flags=re.I).split("/")[0].split("@")[-1].split(":")[0]
    return f"remote host not recognised: {host}"


def folder_area(cwd):
    """The first two folders under home, which is enough to spot a pattern without naming projects."""
    home = str(pathlib.Path.home())
    if not cwd or not cwd.startswith(home):
        return "outside home" if cwd else "none"
    return "~/" + "/".join(pathlib.PurePath(cwd[len(home):].lstrip("/\\")).parts[:2])


def codex_shape(paths, stats):
    """How Codex records its work, to judge whether lines can be counted. Names and field names only:
    never commands, arguments, paths or code."""
    tools, events, fields = collections.Counter(), collections.Counter(), collections.defaultdict(set)
    versions = collections.Counter()
    for path in paths:
        for record in read_records(path, stats):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if record.get("type") == "session_meta":
                versions[str(payload.get("cli_version"))[:20]] += 1
            elif record.get("type") == "response_item" and kind in ("function_call", "custom_tool_call"):
                tools[str(payload.get("name"))[:60]] += 1
            elif record.get("type") == "event_msg":
                events[str(kind)[:60]] += 1
                item = payload.get("item")
                if isinstance(item, dict):  # newer logs nest the work one level down
                    item_kind = f"item:{str(item.get('type'))[:50]}"
                    events[item_kind] += 1
                    if re.search("patch|diff|file|change", item_kind.lower()):
                        fields[item_kind] |= {str(key)[:40] for key in item}
                        for change in item.get("changes") or []:
                            if isinstance(change, dict):
                                fields[f"{item_kind}.changes"] |= {str(key)[:40] for key in change}
                if re.search("patch|diff|file|change", str(kind)):
                    fields[str(kind)[:60]] |= {str(key)[:40] for key in payload}
                    changes = payload.get("changes")
                    if isinstance(changes, dict):
                        for change in changes.values():
                            if isinstance(change, dict):
                                fields[f"{str(kind)[:60]}.changes"] |= {str(key)[:40] for key in change}
    return tools, events, fields, versions


def cmd_doctor(_args):
    config = read_config()
    records, stats = collections.Counter(), collections.Counter()
    unrecognized = collections.Counter()
    lookup = RepoLookup({}, fetch_org_repos(config)) if config else None

    def count_cwd(cwd, _stats):
        records[cwd] += 1
        return NO_REPO

    for root, parser in log_sources():
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            before = stats["unrecognized_files"]
            try:
                parser(path, count_cwd, stats)
            except Exception:
                stats["parse_errors"] += 1
                continue
            if stats["unrecognized_files"] > before:
                unrecognized[parser.__name__] += 1
                for record in read_records(path, stats):
                    unrecognized[f"{parser.__name__} record type: {record.get('type')}"] += 1

    reasons, areas = collections.Counter(), collections.defaultdict(collections.Counter)
    for cwd, count in records.items():
        reason = why_skipped(cwd, lookup)
        reasons[reason] += count
        areas[reason][folder_area(cwd)] += count
    print(f"Python {platform.python_version()} on {sys.platform}. Nothing here was sent anywhere.\n")
    print("Records by outcome:")
    for reason, count in reasons.most_common():
        where = ", ".join(f"{area} ({n})" for area, n in areas[reason].most_common(3))
        print(f"  {count:>8}  {reason}  [{where}]")
    print(f"\nFiles that couldn't be read: {stats['parse_errors']}. Unparsable lines: {stats['bad_lines']}.")
    print("Unrecognised log files:" if unrecognized else "Unrecognised log files: none")
    for name, count in sorted(unrecognized.items()):
        print(f"  {count:>8}  {name}")

    codex_root = log_sources()[1][0]
    paths = list(codex_root.rglob("*.jsonl")) if codex_root.is_dir() else []
    tools, events, fields, versions = codex_shape(paths, collections.Counter())
    print(f"\nCodex: {len(paths)} log files. Names only, no commands or code.")
    print("  CLI versions:", ", ".join(f"{name} ({n})" for name, n in versions.most_common(6)) or "none")
    print("  Tools:", ", ".join(f"{name} ({n})" for name, n in tools.most_common(25)) or "none")
    print("  Events:", ", ".join(f"{name} ({n})" for name, n in events.most_common(40)) or "none")
    for name, keys in sorted(fields.items()):
        print(f"  Fields in {name}: {', '.join(sorted(keys))}")
    return 0


def main():
    # A Windows console or a redirected log may not be able to show a name or path, which mustn't be fatal.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--url", default=DEFAULT_URL)
    install.set_defaults(func=cmd_install)
    run = commands.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)
    preview = commands.add_parser("capture-preview")
    preview.add_argument("--hours", type=int, default=24 * 35)
    preview.add_argument("--out", default=str(pathlib.Path(tempfile.gettempdir()) / "tracker-capture-preview.jsonl"))
    preview.set_defaults(func=cmd_capture_preview)
    commands.add_parser("status").set_defaults(func=cmd_status)
    commands.add_parser("doctor").set_defaults(func=cmd_doctor)
    commands.add_parser("uninstall").set_defaults(func=cmd_uninstall)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
