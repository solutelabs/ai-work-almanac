"""Builds what the collector would send from deliberately broken logs, from a fixed seed so failures repeat.
test_stress.py checks it doesn't crash. worker/test/contract.test.js checks the server accepts every row of
it, because the server rejects a whole batch over one bad value and that laptop would then stop reporting."""
import collections
import json
import pathlib
import random
import sys
import tempfile

import collect

JUNK = [None, 0, -1, 1.5, True, "", " ", "x" * 300, "../../etc/passwd", "a/b", "a\nb", "a\x00b", "\x7f", "क्लाइंट", "🙂" * 50,
        [], [1], ["x"], {}, {"a": 1}, 10 ** 30, -(10 ** 30), "2026-13-40T99:99:99Z", "soon", "<synthetic>", [None], [{"lines": 5}], [{"lines": [1, None]}]]
STAMP = "2026-10-01T10:00:00.123Z"
REMOTES = ["https://github.com/Your-Org/Example-App.git", "git@gitlab.com:acme/app.git", "https://x:tok@dev.azure.com/a/b/_git/c", "git@host:a b/c d.git",
           "https://github.com/a/b%20c", "https://github.com/a/~b.git", "ssh://[::1]:22/a/b", "https://exa mple.com/a/b", "https://例え.jp/オーナー/レポ.git",
           "git@github-work:a/b", "/local", "file:///a/b", "", "\n", "git@h:a", "https://h/a/b/", "https://h/.../..", "https://github.com/a/b/c/d"]


def valid_claude(i):
    return [{"type": "user", "sessionId": f"s{i}", "cwd": "/w", "timestamp": STAMP, "version": "2.1.0", "message": {"content": "fix it"}},
            {"type": "user", "sessionId": f"s{i}", "cwd": "/w", "timestamp": STAMP,
             "toolUseResult": {"filePath": "/w/a", "structuredPatch": [{"lines": ["+a", "-b"]}]}},
            {"type": "assistant", "sessionId": f"s{i}", "cwd": "/w", "timestamp": STAMP, "version": "2.1.0",
             "message": {"id": f"m{i}", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                         "usage": {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 1, "cache_creation_input_tokens": 1}}}]


def valid_codex(i):
    return [{"type": "session_meta", "payload": {"id": f"c{i}", "cwd": "/w", "cli_version": "0.1.0"}},
            {"type": "turn_context", "payload": {"cwd": "/w", "model": "gpt-5"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": "hi"}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "function_call", "name": "shell", "arguments": "{\"command\": [\"ls\"]}"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 9 + i, "output_tokens": 3}}}}]


def corrupt(record, rng):
    """Replaces one value, at any depth, with junk."""
    holder, keys = record, []
    while True:
        keys = list(holder) if isinstance(holder, dict) else list(range(len(holder))) if isinstance(holder, list) else []
        if not keys:
            return record
        key = rng.choice(keys)
        child = holder[key]
        if not isinstance(child, (dict, list)) or rng.random() < 0.35:
            holder[key] = rng.choice(JUNK)
            return record
        holder = child


def write_log(folder, name, records):
    path = pathlib.Path(folder) / name
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write((record if isinstance(record, str) else json.dumps(record)) + "\n")
    return path


def broken_logs(folder, seed=1, sessions=150):
    rng = random.Random(seed)
    extras = ["{not json", "", "[]", "5", "null", "\"text\"", "{\"type\":", "\x00\x00"]
    claude, codex = [], []
    for i in range(sessions):
        for builder, out in ((valid_claude, claude), (valid_codex, codex)):
            records = [corrupt(r, rng) if rng.random() < 0.6 else r for r in builder(i)]
            out.append(records + [rng.choice(extras)])
    return ([write_log(folder, f"claude{i}.jsonl", r) for i, r in enumerate(claude)],
            [write_log(folder, f"codex{i}.jsonl", r) for i, r in enumerate(codex)])


def build(seed=1, sessions=150):
    with tempfile.TemporaryDirectory() as folder:
        claude, codex = broken_logs(folder, seed, sessions)
        stats = collections.Counter()
        lookup = collect.RepoLookup({"/w": "your-org/app"})
        rows = [row for path in claude for row in collect.claude_rows(path, lookup, stats)]
        rows += [row for path in codex for row in collect.codex_rows(path, lookup, stats)]
        repo_of = lambda cwd: lookup(cwd, collections.Counter())
        records = []
        for paths, tool, reader in ((claude, "claude-code", collect.claude_messages), (codex, "codex", collect.codex_messages)):
            for path in paths:
                records += collect.capture_records(path, tool, reader, "", "salt", repo_of)[0]
        batches = list(collect.capture_batches(records))
    repos = [repo for repo in map(collect.parse_remote, REMOTES) if repo]
    commits = [{"repo": repo, "oid": "a" * 40, "authored_at": "2026-10-01T10:00:00Z", "ai": False, "on_default": True, "lines_added": 1, "lines_removed": 0} for repo in repos]
    return {"ingest": [collect.payload_for(rows[i:i + collect.BATCH_ROWS], stats) for i in range(0, len(rows), collect.BATCH_ROWS)],
            "capture": [{"machine": "laptop", "records": batch} for batch in batches],
            "commits": [{"commits": commits}] if commits else [],
            "harness": [{"machine": "laptop", "repos": [{"repo": repo, "plugin": True, "plugin_version": "2.4.0", "hooks": False} for repo in repos]}] if repos else []}


if __name__ == "__main__":
    # Bytes rather than print, because Windows would otherwise re-encode this for its console.
    sys.stdout.buffer.write(json.dumps(build(int(sys.argv[1]) if len(sys.argv) > 1 else 1)).encode())
