"""Runs the real script against a fake server, on whichever OS this is. Slower than test_collect.py, and
aimed at what goes wrong on someone's laptop: outages, overlapping runs, odd paths and very large logs."""
import http.server
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import collect
import fuzz_payload

SCRIPT = str(pathlib.Path(__file__).with_name("collect.py"))
SECRET = "AKIAABCDEFGHIJKLMNOP"
STAMP = "2026-10-01T10:00:00.000Z"
MAX_BODY = 2_000_000  # worker/src/validate.js


class FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests, self.fail, self.lock, self.script = [], {}, threading.Lock(), None
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def bodies(self, route):
        with self.lock:
            return [json.loads(body) for path, body in self.requests if path == route]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        if self.path == "/collect.py":
            if self.server.script is None:
                self.reply(503, {})
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.server.script)
            return
        self.reply(200, {"repos": []})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        status = self.server.fail.get(self.path, 200)
        with self.server.lock:
            self.server.requests.append((self.path, body))
        self.reply(status, {"ok": status == 200})


def reply_record(i, cwd, session="s1", text="ok"):
    return {"type": "assistant", "sessionId": session, "cwd": cwd, "timestamp": STAMP, "version": "2.1.0",
            "message": {"id": f"{session}-m{i}", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def prompt_record(cwd, session="s1", text="fix it"):
    return {"type": "user", "sessionId": session, "cwd": cwd, "timestamp": STAMP, "version": "2.1.0", "message": {"content": text}}


class Laptop(unittest.TestCase):
    """A throwaway home folder. HOME and USERPROFILE both move, so this can't touch the real one."""

    HOME_NAME = "home"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / self.HOME_NAME
        self.logs = self.home / ".claude" / "projects" / "p"
        self.logs.mkdir(parents=True)
        self.tracker = self.home / ".pulse-tracker"
        self.tracker.mkdir()
        self.work = self.home / "work"
        self.work.mkdir()
        self.server = FakeServer()
        self.addCleanup(self.server.shutdown)
        self.write_config()

    def write_config(self, capture=True):
        (self.tracker / "config.json").write_text(json.dumps({"url": self.server.url, "token": "t", "capture": capture, "salt": "s"}), encoding="utf-8")

    def env(self, **over):
        return {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home), "TRACKER_HOME": str(self.tracker),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "CODEX_HOME": str(self.home / ".codex"), "PYTHONIOENCODING": "utf-8",
                "GIT_CONFIG_GLOBAL": os.devnull, **over}

    def start(self, *args, **env):
        return subprocess.Popen([sys.executable, SCRIPT, *args], env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")

    def run_tool(self, *args, timeout=300, **env):
        process = self.start(*args, **env)
        out, err = process.communicate(timeout=timeout)
        self.assertNotIn("Traceback", err, err)
        return process.returncode, out, err

    def log(self, name, records):
        return fuzz_payload.write_log(self.logs, name, records)

    def last_run(self):
        return json.loads((self.tracker / "last_run.json").read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.tracker / "state.json").read_text(encoding="utf-8"))

    def usage_tokens(self):
        rows = {}
        for body in self.server.bodies("/api/ingest"):
            for r in body["rows"]:
                rows[(r["source"], r["day"], r["model"], r["repo"])] = r["output_tokens"]  # as the server's upsert keeps it
        return sum(rows.values())


class FullRun(Laptop):
    def test_sends_usage_and_text_then_only_a_heartbeat(self):
        self.log("a.jsonl", [prompt_record(str(self.work), text=f"key {SECRET}"), reply_record(1, str(self.work))])
        code, out, _ = self.run_tool("run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.usage_tokens(), 5)
        self.assertTrue(self.last_run()["ok"])
        self.assertEqual(len(self.server.bodies("/api/capture")), 1)
        before = len(self.server.bodies("/api/capture"))
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.server.bodies("/api/ingest")[-1]["rows"], [])
        self.assertEqual(len(self.server.bodies("/api/capture")), before)
        with self.server.lock:
            self.assertFalse([path for path, body in self.server.requests if SECRET.encode() in body], "a known secret left the laptop")

    def test_a_server_outage_loses_nothing(self):
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        self.server.fail["/api/ingest"] = 500
        code, _, err = self.run_tool("run")
        self.assertEqual(code, 1)
        self.assertIn("will retry", err)
        self.assertFalse(self.last_run()["ok"])
        del self.server.fail["/api/ingest"]
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.usage_tokens(), 5)

    def test_a_server_that_cant_be_reached_is_reported_not_a_crash(self):
        (self.tracker / "config.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "t"}), encoding="utf-8")
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        code, _, err = self.run_tool("run")
        self.assertEqual(code, 1)
        self.assertIn("will retry", err)

    def test_a_text_upload_failure_keeps_usage_and_resumes(self):
        self.log("a.jsonl", [prompt_record(str(self.work)), reply_record(1, str(self.work))])
        self.server.fail["/api/capture"] = 500
        self.assertEqual(self.run_tool("run")[0], 1)
        self.assertEqual(self.usage_tokens(), 5)
        del self.server.fail["/api/capture"]
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertTrue(self.server.bodies("/api/capture"))

    def test_a_commit_upload_failure_never_blocks_the_rest(self):
        repo = self.work / "client"
        repo.mkdir()
        identity = dict(GIT_AUTHOR_NAME="A", GIT_COMMITTER_NAME="A", GIT_AUTHOR_EMAIL="a@b.co", GIT_COMMITTER_EMAIL="a@b.co")
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "a@b.co"), ("remote", "add", "origin", "git@gitlab.com:acme/app.git"),
                     ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env=self.env(**identity), capture_output=True)
        self.log("a.jsonl", [prompt_record(str(repo)), reply_record(1, str(repo))])
        self.server.fail["/api/commits"] = 500
        code, _, err = self.run_tool("run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.usage_tokens(), 5)
        self.assertTrue(self.server.bodies("/api/capture"))
        del self.server.fail["/api/commits"]
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.server.bodies("/api/commits")[-1]["commits"][0]["repo"], "gitlab.com-acme/app")
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(len([b for b in self.server.bodies("/api/commits")]), 2, "unchanged commits were sent again")

    def test_a_log_still_being_written_is_finished_on_the_next_run(self):
        path = self.log("a.jsonl", [reply_record(1, str(self.work))])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(reply_record(2, str(self.work)))[:40])
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.usage_tokens(), 5)
        self.log("a.jsonl", [reply_record(1, str(self.work)), reply_record(2, str(self.work))])
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.usage_tokens(), 10)

    def test_damaged_saved_files_mean_starting_over_not_crashing(self):
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        for content in ("{not json", "[]", "5", "null", '{"files": 5, "repos": [], "capture": "x", "commits": 1, "parser": 1}', ""):
            (self.tracker / "state.json").write_text(content, encoding="utf-8")
            code, out, err = self.run_tool("run")
            self.assertEqual(code, 0, f"state {content!r}: {out}{err}")
        for content in ("{not json", "[]", "null", "{}", '{"url": 5, "token": 5}'):
            (self.tracker / "config.json").write_text(content, encoding="utf-8")
            code, _, err = self.run_tool("run")
            self.assertNotEqual(code, 0, content)
            self.assertIn("install", err.lower(), content)

    def test_runs_with_no_git_installed(self):
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        code, _, err = self.run_tool("run", PATH=str(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.usage_tokens(), 5)

    def test_every_command_runs(self):
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        self.assertEqual(self.run_tool("run", "--dry-run")[0], 0)
        self.assertEqual(self.run_tool("capture-preview", "--out", str(self.root / "preview.jsonl"))[0], 0)
        self.assertEqual(self.run_tool("status")[0], 0)
        self.run_tool("run")
        self.assertEqual(self.run_tool("status")[0], 0)
        self.assertEqual(self.run_tool("doctor")[0], 0)

    def test_output_survives_a_console_that_cant_show_unicode(self):
        name = self.work / "क्लाइंट"
        name.mkdir()
        self.log("a.jsonl", [prompt_record(str(name), text="नमस्ते"), reply_record(1, str(name))])
        for command in (("run",), ("run", "--dry-run"), ("doctor",), ("status",)):
            code, out, err = self.run_tool(*command, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
            self.assertEqual(code, 0, f"{command}: {out}{err}")


class Overlap(Laptop):
    def test_runs_started_together_dont_crash_or_lose_progress(self):
        for i in range(30):
            self.log(f"{i}.jsonl", [prompt_record(str(self.work), session=f"s{i}"), reply_record(1, str(self.work), session=f"s{i}")])
        processes = [self.start("run") for _ in range(6)]
        results = [(p.communicate(timeout=300), p.returncode) for p in processes]
        for (out, err), code in results:
            self.assertNotIn("Traceback", err, err)
            self.assertEqual(code, 0, out + err)
        self.assertEqual(self.usage_tokens(), 150)
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.server.bodies("/api/ingest")[-1]["rows"], [])
        self.assertFalse(list(self.tracker.glob("*.tmp*")), "left temporary files behind")
        self.assertFalse((self.tracker / "run.lock").exists(), "left its lock behind")

    def test_a_lock_from_a_crashed_run_doesnt_stop_reporting_forever(self):
        self.log("a.jsonl", [reply_record(1, str(self.work))])
        lock = self.tracker / "run.lock"
        lock.write_text("1", encoding="utf-8")
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.usage_tokens(), 0, "a fresh lock should make this run wait its turn")
        stale = time.time() - collect.RUN_LOCK_MAX_AGE - 60
        os.utime(lock, (stale, stale))
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertEqual(self.usage_tokens(), 5)


class OddHome(FullRun):
    """Everything in FullRun again, under the kind of home folder that breaks quoting and encoding."""
    HOME_NAME = "Mr O'Neil & Sons (क्लाइंट) 100%"


class Scale(Laptop):
    def test_a_huge_history_stays_within_the_servers_limits(self):
        started = time.monotonic()
        for i in range(600):
            self.log(f"{i}.jsonl", [prompt_record(str(self.work), session=f"s{i}", text="p" * 2000)] +
                     [reply_record(n, str(self.work), session=f"s{i}", text="r" * 4000) for n in range(40)])
        self.log("big.jsonl", [reply_record(n, str(self.work), session="big", text="x" * 150000 if n % 50 == 0 else "y") for n in range(20000)])
        code, out, err = self.run_tool("run", timeout=600)
        self.assertEqual(code, 0, out + err)
        with self.server.lock:
            sizes = [(path, len(body)) for path, body in self.server.requests]
        self.assertTrue(all(size <= MAX_BODY for _, size in sizes), [s for s in sizes if s[1] > MAX_BODY][:3])
        for body in self.server.bodies("/api/ingest"):
            self.assertLessEqual(len(body["rows"]), 1000)
        for body in self.server.bodies("/api/capture"):
            self.assertLessEqual(len(body["records"]), 1000)
        self.assertEqual(self.usage_tokens(), 600 * 40 * 5 + 20000 * 5)
        self.assertLess(time.monotonic() - started, 300, "too slow for a laptop to do in the background")
        started = time.monotonic()
        self.assertEqual(self.run_tool("run")[0], 0)
        self.assertLess(time.monotonic() - started, 30, "a run with nothing new should be quick")


class SelfUpdate(Laptop):
    """The installed copy against a server offering various scripts. Whatever it offers, the run must still send."""

    SOURCE = pathlib.Path(SCRIPT).read_text(encoding="utf-8")

    def setUp(self):
        super().setUp()
        self.installed = self.tracker / "collect.py"
        self.installed.write_text(self.SOURCE, encoding="utf-8")

    def offer(self, source):
        self.server.script = source.encode("utf-8")

    def with_version(self, version, source=None):
        return (source or self.SOURCE).replace(f'VERSION = "{collect.VERSION}"', f'VERSION = "{version}"', 1)

    def run_installed(self):
        process = subprocess.run([sys.executable, str(self.installed), "run"], env=self.env(), capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        self.assertNotIn("Traceback", process.stderr, process.stderr)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout + process.stderr

    def assert_kept(self, output, reason):
        self.assertEqual(self.installed.read_text(encoding="utf-8"), self.SOURCE, output)
        self.assertIn(reason, output)
        self.assertTrue(self.server.bodies("/api/ingest"), "the run must still send")
        self.assertEqual(list(self.tracker.glob("*.new")), [])

    def test_applies_a_newer_version_which_takes_over_on_the_next_run(self):
        newer = self.with_version("99.0.0")
        self.offer(newer)
        output = self.run_installed()
        self.assertIn("updated to 99.0.0", output)
        self.assertEqual(self.installed.read_text(encoding="utf-8"), newer)
        self.assertEqual(self.server.bodies("/api/ingest")[-1]["version"], collect.VERSION)
        self.assertIn("up to date", self.run_installed())
        self.assertEqual(self.server.bodies("/api/ingest")[-1]["version"], "99.0.0")
        self.assertEqual(list(self.tracker.glob("*.new")), [])

    def test_keeps_the_same_or_an_older_version(self):
        for version in (collect.VERSION, "0.1.0"):
            self.offer(self.with_version(version))
            self.assert_kept(self.run_installed(), "up to date")

    def test_waits_for_a_reinstall_when_the_notice_changes(self):
        self.offer(self.with_version("99.0.0").replace("Your prompts, the AI's text responses", "Your prompts, your screen, the AI's text responses", 1))
        self.assert_kept(self.run_installed(), "waits for a re-install")

    def test_refuses_a_version_that_cannot_start(self):
        self.offer(self.with_version("99.0.0").replace("import argparse\n", "import argparse\nraise RuntimeError('broken')\n", 1))
        self.assert_kept(self.run_installed(), "failed its start-up check")

    def test_checks_a_new_version_without_touching_real_state(self):
        marker = "pathlib.Path(os.environ['TRACKER_HOME'], 'touched').write_text('x')\n"
        self.offer(self.with_version("99.0.0").replace("VERSION = ", f"import os, pathlib\n{marker}VERSION = ", 1))
        self.assertIn("updated to 99.0.0", self.run_installed())
        self.assertFalse((self.tracker / "touched").exists())

    def test_ignores_bad_downloads_and_outages(self):
        for source in ("<html>not a script</html>", "x = (", "VERSION = open('x')\n", 'VERSION = "not.a.version"\n', ""):
            self.offer(source)
            self.run_installed()
            self.assertEqual(self.installed.read_text(encoding="utf-8"), self.SOURCE, source)
        self.server.script = None
        self.run_installed()
        self.assertEqual(self.installed.read_text(encoding="utf-8"), self.SOURCE)
        self.server.script = b"\xff\xfe\x00 not text"
        self.run_installed()
        self.assertEqual(self.installed.read_text(encoding="utf-8"), self.SOURCE)
        self.server.script = b"#" * (collect.UPDATE_MAX_BYTES + 1)
        self.assert_kept(self.run_installed(), "download too large")

    def test_only_the_installed_copy_updates_itself(self):
        self.offer(self.with_version("99.0.0"))
        code, out, _ = self.run_tool("run")
        self.assertEqual(code, 0)
        self.assertIn("not the installed copy", out)

    def test_refuses_a_plain_http_server(self):
        (self.tracker / "config.json").write_text(json.dumps({"url": "http://example.invalid", "token": "t", "capture": False, "salt": "s"}), encoding="utf-8")
        process = subprocess.run([sys.executable, str(self.installed), "run"], env=self.env(), capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        self.assertNotIn("Traceback", process.stderr, process.stderr)
        self.assertIn("must start with https", process.stderr)
        self.assertEqual(self.installed.read_text(encoding="utf-8"), self.SOURCE)


class BrokenLogs(unittest.TestCase):
    def test_parsers_survive_every_kind_of_damage(self):
        for seed in range(1, 6):
            payload = fuzz_payload.build(seed, sessions=60)
            self.assertTrue(payload["ingest"], "valid records should still produce rows")

    def test_one_bad_file_doesnt_stop_the_others(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            fuzz_payload.write_log(root, "good.jsonl", [reply_record(1, "/w")])
            fuzz_payload.write_log(root, "bad.jsonl", [reply_record(2, "/w")])

            def exploding(path, lookup, stats):
                if path.name == "bad.jsonl":
                    raise KeyError("a parser bug")
                return collect.claude_rows(path, lookup, stats)
            original = collect.log_sources
            collect.log_sources = lambda: [(root, exploding)]
            self.addCleanup(setattr, collect, "log_sources", original)
            rows, marks, stats = collect.collect({})
            self.assertEqual(len(rows), 1)
            self.assertEqual(stats["parse_errors"], 1)
            self.assertEqual(len(marks), 1, "the bad file must be retried, not marked as done")


@unittest.skipUnless(os.environ.get("COLLECTOR_REAL_INSTALL"), "changes this machine's scheduler and home folder")
class RealInstall(unittest.TestCase):
    """The OS's real scheduler, in the real home folder, since a scheduler ignores any test environment.
    So this only runs on throwaway CI machines."""

    def setUp(self):
        self.home = pathlib.Path.home()
        self.tracker = self.home / ".pulse-tracker"
        self.logs = self.home / ".claude" / "projects" / "ci"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home / ".claude", ignore_errors=True)
        self.server = FakeServer()
        self.addCleanup(self.server.shutdown)
        self.env = {key: value for key, value in os.environ.items() if key not in ("TRACKER_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}

    def tool(self, script, *args, **env):
        done = subprocess.run([sys.executable, str(script), *args], env={**self.env, **env}, capture_output=True, encoding="utf-8", errors="replace", timeout=300)
        self.assertNotIn("Traceback", done.stderr, done.stderr)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def scheduled(self, scheduler):
        if scheduler == "launchd":
            return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{collect.LAUNCHD_LABEL}"], capture_output=True).returncode == 0
        if scheduler == "Task Scheduler":
            return subprocess.run(["schtasks", "/Query", "/TN", collect.TASK_NAME], capture_output=True).returncode == 0
        if scheduler == "systemd":
            return subprocess.run(["systemctl", "--user", "is-enabled", "pulse-tracker.timer"], capture_output=True).returncode == 0
        return "pulse-tracker" in subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout

    def run_the_windows_task_command(self):
        """A task set to run for a logged-in user can't fire on a runner with nobody logged in. So this runs its
        exact registered command, which covers the quoting and windowless Python, but not the firing itself."""
        details = subprocess.run(["schtasks", "/Query", "/V", "/FO", "LIST", "/TN", collect.TASK_NAME], capture_output=True, encoding="utf-8", errors="replace").stdout
        fields = dict(line.split(":", 1) for line in details.splitlines() if ":" in line)
        self.assertNotIn("No Start On Batteries", fields["Power Management"], details)
        self.assertNotIn("Stop On Battery", fields["Power Management"], details)
        self.assertIn("pythonw", fields["Task To Run"], details)
        self.assertEqual(subprocess.run(fields["Task To Run"].strip(), env=self.env, timeout=120).returncode, 0, details)

    def wait_for_rows(self, scheduler):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(body["rows"] for body in self.server.bodies("/api/ingest")):
                return
            time.sleep(2)
        last_run = self.tracker / "last_run.json"
        why = subprocess.run(["schtasks", "/Query", "/V", "/FO", "LIST", "/TN", collect.TASK_NAME], capture_output=True, encoding="utf-8", errors="replace").stdout if scheduler == "Task Scheduler" else ""
        self.fail(f"{scheduler} never sent usage. Last run: {last_run.read_text() if last_run.exists() else 'none'}\n{why}")

    def test_installs_runs_by_itself_reinstalls_and_uninstalls(self):
        fuzz_payload.write_log(self.logs, "a.jsonl", [prompt_record(str(self.home)), reply_record(1, str(self.home))])
        out = self.tool(SCRIPT, "install", "--url", self.server.url, TRACKER_TOKEN="slt_test")
        scheduler = out.split("via ")[-1].split(".")[0]
        print("Scheduled with", scheduler)
        self.assertTrue(self.scheduled(scheduler), scheduler)
        installed = self.tracker / "collect.py"
        self.assertTrue(installed.exists())
        if scheduler in ("cron", "systemd"):
            # Both are hourly with no run on install, so run what they'd run.
            self.tool(installed, "run")
        if scheduler == "Task Scheduler":
            self.run_the_windows_task_command()
        self.wait_for_rows(scheduler)

        out = self.tool(SCRIPT, "install", "--url", self.server.url, TRACKER_TOKEN="slt_test")
        self.assertIn("Installed", out)
        self.assertTrue(self.scheduled(scheduler), f"{scheduler} after re-installing")

        self.tool(installed, "uninstall")
        self.assertFalse(self.scheduled(scheduler), f"{scheduler} still scheduled after uninstalling")
        self.assertFalse(self.tracker.exists())


if __name__ == "__main__":
    unittest.main()
