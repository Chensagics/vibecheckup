"""Bootstrap tests: the demo corpus generator and vibecheck.sh.

Never touches real session data or the repo's data/ directory -- the generator
is always pointed at a temporary file.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GENERATOR = os.path.join(ROOT, "samples", "generate.py")
SCRIPT = os.path.join(ROOT, "vibecheck.sh")
sys.path.insert(0, ROOT)

from adapters.base import Event  # noqa: E402
from wcstats.clean import prose_text  # noqa: E402
from wcstats.tokenize import tokens  # noqa: E402

# The design contract (section 4) adds model + usage to the adapter event.
# Event.to_json omits both when they are empty, so they are optional per line.
EVENT_FIELDS = {f.name for f in dataclasses.fields(Event)} | {"model", "usage"}
REQUIRED_FIELDS = EVENT_FIELDS - {"model", "usage"}
USAGE_FIELDS = {"input", "output", "cache_read", "cache_write"}


def generate(path, *extra):
    return subprocess.run([sys.executable, GENERATOR, path, *extra],
                          capture_output=True, text=True, timeout=120)


class TestDemoCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "events.ndjson")
        proc = generate(cls.path, "--events", "400")
        assert proc.returncode == 0, proc.stderr
        with open(cls.path, encoding="utf-8") as fh:
            cls.events = [json.loads(line) for line in fh if line.strip()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_line_is_a_full_event(self):
        self.assertGreaterEqual(len(self.events), 400)
        for ev in self.events:
            self.assertLessEqual(set(ev), EVENT_FIELDS)
            self.assertLessEqual(REQUIRED_FIELDS, set(ev))
            self.assertIn(ev["role"], {"user", "assistant", "tool", "system"})
            self.assertIn(ev["kind"], {"prompt", "reply", "thinking", "tool_call",
                                       "tool_result", "error", "meta"})
            self.assertTrue(ev["tool"] and ev["session_id"] and ev["project"])

    def test_timestamps_are_utc_and_ordered(self):
        for ev in self.events:
            self.assertIsNotNone(dt.datetime.fromisoformat(ev["ts"]).tzinfo)
        stamps = [ev["ts"] for ev in self.events]
        self.assertEqual(stamps, sorted(stamps))

    def test_usage_matches_the_contract(self):
        priced = [e for e in self.events if e.get("usage") is not None]
        self.assertTrue(priced)
        for ev in priced:
            self.assertEqual(set(ev["usage"]), USAGE_FIELDS)
            self.assertTrue(all(isinstance(v, int) and v >= 0
                                for v in ev["usage"].values()))
            self.assertEqual(ev["tokens"], sum(ev["usage"].values()))
            self.assertTrue(ev["model"])
            self.assertEqual((ev["role"], ev["kind"]), ("assistant", "reply"))

    def test_grok_carries_no_usage_and_inexact_timestamps(self):
        grok = [e for e in self.events if e["tool"] == "grok"]
        self.assertTrue(grok, "grok should appear in the demo corpus")
        for ev in grok:
            self.assertIsNone(ev.get("usage"))
            self.assertIsNone(ev.get("model"))
            self.assertIsNone(ev["tokens"])
            self.assertFalse(ev["ts_exact"])

    def test_other_tools_price_every_reply(self):
        for ev in self.events:
            if ev["kind"] == "reply" and ev["tool"] != "grok":
                self.assertIsNotNone(ev.get("usage"), ev)

    def test_prompts_survive_the_prose_filter(self):
        prompts = [e for e in self.events if e["kind"] == "prompt"]
        self.assertTrue(prompts)
        kept = [p for p in prompts if tokens(prose_text(p))]
        self.assertEqual(len(kept), len(prompts),
                         "demo prompts must not read as machine noise")

    def test_corpus_spans_tools_projects_and_months(self):
        self.assertGreaterEqual(len({e["tool"] for e in self.events}), 4)
        self.assertGreaterEqual(len({e["project"] for e in self.events}), 5)
        self.assertGreaterEqual(len({e["ts"][:7] for e in self.events}), 3)

    def test_no_absolute_local_paths_leak_into_the_corpus(self):
        for ev in self.events:
            self.assertNotIn("/Users/", ev["text"] or "")
            self.assertNotIn("/home/", ev["text"] or "")
            self.assertNotIn("/", ev["project"])


class TestDeterminism(unittest.TestCase):
    def test_same_seed_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.ndjson"), os.path.join(d, "b.ndjson")
            generate(a, "--events", "200")
            generate(b, "--events", "200")
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_a_different_seed_changes_the_corpus(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.ndjson"), os.path.join(d, "b.ndjson")
            generate(a, "--events", "200")
            generate(b, "--events", "200", "--seed", "1")
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertNotEqual(fa.read(), fb.read())

    def test_bad_arguments_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "x.ndjson")
            self.assertEqual(generate(out, "--end-date", "nope").returncode, 2)
            self.assertEqual(generate(out, "--days", "2").returncode, 2)


class TestVibecheckScript(unittest.TestCase):
    def sh(self, *args, **kw):
        return subprocess.run(["sh", SCRIPT, *args], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=60, **kw)

    def test_script_is_present_and_executable(self):
        self.assertTrue(os.path.isfile(SCRIPT))
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_posix_syntax(self):
        for shell in ("sh", "bash", "dash", "zsh"):
            try:
                proc = subprocess.run([shell, "-n", SCRIPT], capture_output=True,
                                      text=True, timeout=30)
            except FileNotFoundError:
                continue  # shell not installed on this machine
            self.assertEqual(proc.returncode, 0, f"{shell}: {proc.stderr}")

    def test_help_lists_the_flags(self):
        proc = self.sh("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--demo", "--force", "--tool", "--limit", "--no-open"):
            self.assertIn(flag, proc.stdout)

    def test_unknown_flag_fails_with_usage(self):
        proc = self.sh("--nope")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown option", proc.stderr)

    def test_bad_flag_values_are_rejected_before_anything_runs(self):
        self.assertEqual(self.sh("--limit", "abc").returncode, 2)
        self.assertEqual(self.sh("--tool", "not a tool").returncode, 2)
        self.assertEqual(self.sh("--tool").returncode, 2)

    def test_demo_refuses_to_clobber_existing_events_non_interactively(self):
        events = os.path.join(ROOT, "data", "events.ndjson")
        if not os.path.exists(events):
            self.skipTest("no data/events.ndjson to protect")
        proc = self.sh("--demo", "--no-open")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--force", proc.stderr)


# --- python3 detection --------------------------------------------------------

# `command -v python3` used to be the whole check, which passes on a Mac with no
# Command Line Tools (the /usr/bin/python3 shim exists but only opens Apple's
# installer). Each stub below stands in for one way that goes wrong.
SHIM_PY = '#!/bin/sh\necho "requesting install" >&2\nexit 1\n'
OLD_PY = '#!/bin/sh\n[ "$1" = "-V" ] && { echo "Python 3.8.10"; exit 0; }\nexit 0\n'
JUNK_PY = '#!/bin/sh\necho "banana"\n'


class TestPythonProbe(unittest.TestCase):
    """vibecheck must name the problem itself, never hand over a traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)

    def stub(self, body):
        path = os.path.join(self.bin, "python3")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def run_script(self, *args, only_stub_on_path=False):
        env = dict(os.environ)
        if only_stub_on_path:
            # A PATH with no python3 at all. uname/cat are all the script needs
            # to reach its own error message.
            for tool in ("uname", "cat"):
                src = shutil.which(tool)
                if src:
                    os.symlink(src, os.path.join(self.bin, tool))
            env["PATH"] = self.bin
        else:
            env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        return subprocess.run(["/bin/sh", SCRIPT, *args], capture_output=True,
                              text=True, env=env, stdin=subprocess.DEVNULL,
                              timeout=60)

    def assertRefused(self, proc):
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("3.9+", proc.stderr)
        # The platform-specific install guidance survives.
        self.assertTrue("xcode-select" in proc.stderr or "apt install" in proc.stderr,
                        proc.stderr)

    def test_missing_python3_is_refused(self):
        self.assertRefused(self.run_script("--no-open", only_stub_on_path=True))

    def test_a_python3_that_does_not_run_is_refused(self):
        self.stub(SHIM_PY)
        proc = self.run_script("--no-open")
        self.assertRefused(proc)
        self.assertIn("no working python3", proc.stderr)

    def test_too_old_python3_is_refused_and_named(self):
        self.stub(OLD_PY)
        proc = self.run_script("--no-open")
        self.assertRefused(proc)
        self.assertIn("3.8.10", proc.stderr)

    def test_an_unreadable_version_is_refused_not_guessed(self):
        self.stub(JUNK_PY)
        proc = self.run_script("--no-open")
        self.assertRefused(proc)
        self.assertIn("banana", proc.stderr)

    def test_help_still_works_without_a_usable_python3(self):
        self.stub(SHIM_PY)
        proc = self.run_script("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--demo", proc.stdout)


# --- curl | sh ----------------------------------------------------------------

# curl replacement: the bootstrap must be exercised without a network call.
FAKE_CURL = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) shift; out="$1" ;; esac
  shift
done
[ -n "$out" ] || exit 1
exec cp "%s" "$out"
"""

STUB_MODULES = {
    "ingest.py": 'print("BOOTSTRAP_INGEST_RAN")\n',
    "analyze.py": 'print("BOOTSTRAP_ANALYZE_RAN")\n',
    "build_dashboard.py": 'open("dashboard.html", "w").write("<html></html>")\n',
}


class TestCurlPipeBootstrap(unittest.TestCase):
    """`curl ... | sh` must never mistake the user's cwd for a checkout.

    Piped to sh, "$0" is just "sh", so dirname used to yield "." -- and any
    directory holding a file called ingest.py counted as a checkout. Standing in
    an unrelated repo, that ran the user's own ingest.py and analyze.py and left
    a data/ directory in their tree.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = os.path.join(self.tmp.name, "prefix")
        self.cwd = os.path.join(self.tmp.name, "cwd")
        os.makedirs(self.cwd)
        # The user's own project: an ingest.py that is not ours.
        for name in ("ingest.py", "analyze.py"):
            with open(os.path.join(self.cwd, name), "w", encoding="utf-8") as fh:
                fh.write(f'print("MARKER_USER_{name[:-3].upper()}")\n')
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.tarball = self.build_tarball()
        curl = os.path.join(self.bin, "curl")
        with open(curl, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CURL % self.tarball)
        os.chmod(curl, 0o755)

    def build_tarball(self):
        """A stand-in repo: the real vibecheck.sh plus stubs for the stages."""
        stage = os.path.join(self.tmp.name, "stage", "repo")
        os.makedirs(os.path.join(stage, "adapters"))
        os.makedirs(os.path.join(stage, "wcstats"))
        shutil.copy(SCRIPT, os.path.join(stage, "vibecheck.sh"))
        # The checkout sentinel: both files must be present for a directory to
        # count as a checkout.
        with open(os.path.join(stage, "adapters", "base.py"), "w") as fh:
            fh.write("# sentinel\n")
        with open(os.path.join(stage, "wcstats", "prices.json"), "w") as fh:
            fh.write("{}\n")
        for name, body in STUB_MODULES.items():
            with open(os.path.join(stage, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        path = os.path.join(self.tmp.name, "repo.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(stage, arcname="repo")
        return path

    def pipe(self, *args):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["VIBECHECK_HOME"] = self.prefix
        with open(SCRIPT, "rb") as fh:
            return subprocess.run(["/bin/sh", "-s", "--", *args], stdin=fh,
                                  capture_output=True, text=True, cwd=self.cwd,
                                  env=env, timeout=120)

    def test_the_users_own_ingest_py_is_never_run(self):
        proc = self.pipe("--no-open")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("MARKER_USER_INGEST", proc.stdout)
        self.assertNotIn("MARKER_USER_ANALYZE", proc.stdout)

    def test_it_bootstraps_into_its_own_prefix_instead(self):
        proc = self.pipe("--no-open")
        self.assertIn("no checkout here", proc.stdout)
        self.assertIn("BOOTSTRAP_INGEST_RAN", proc.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.prefix, "vibecheck.sh")))

    def test_nothing_is_written_into_the_users_directory(self):
        self.pipe("--no-open")
        self.assertEqual(sorted(os.listdir(self.cwd)), ["analyze.py", "ingest.py"])

    def test_a_real_checkout_carries_both_sentinel_files(self):
        for rel in ("ingest.py", "adapters/base.py", "wcstats/prices.json"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *rel.split("/"))),
                            f"{rel} is the checkout sentinel vibecheck.sh looks for")


# --- ingest safety ------------------------------------------------------------

INGEST = os.path.join(ROOT, "ingest.py")
GROK_SESSION = os.path.join(".grok", "sessions",
                            "%2FUsers%2Falice%2FProjects%2Fdemo", "sess-1")


class TestIngestNeverDestroysTheCorpus(unittest.TestCase):
    """A run that finds nothing, or finds less, must not eat events.ndjson."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.out = os.path.join(self.home, "events.ndjson")
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("PREVIOUS CORPUS\n" * 20)
        with open(self.out, "rb") as fh:
            self.before = fh.read()

    def add_grok_session(self):
        d = os.path.join(self.home, GROK_SESSION)
        os.makedirs(d)
        with open(os.path.join(d, "chat_history.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"type": "user", "content": "refactor the parser"}\n')
            fh.write('{"type": "assistant", "content": "done, tests pass"}\n')

    def ingest(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        return subprocess.run([sys.executable, INGEST, "--out", self.out, *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def current(self):
        with open(self.out, "rb") as fh:
            return fh.read()

    def test_finding_nothing_fails_loudly_and_changes_nothing(self):
        proc = self.ingest()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("no session logs found", proc.stdout)
        self.assertIn("--demo", proc.stdout)
        self.assertEqual(self.current(), self.before,
                         "an empty run truncated the previous corpus")

    def test_finding_nothing_never_claims_there_were_no_errors(self):
        # The old contradiction: "FATAL ERRORS: none" and exit 0, then analyze.py
        # died on an empty events.ndjson.
        self.assertNotIn("FATAL ERRORS: none", self.ingest().stdout)

    def test_a_named_tool_with_no_logs_leaves_the_corpus_alone(self):
        proc = self.ingest("--tool", "grok")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("grok", proc.stdout)
        self.assertEqual(self.current(), self.before)

    def test_no_temp_file_is_left_behind(self):
        self.ingest()
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_a_successful_run_replaces_the_corpus_atomically(self):
        self.add_grok_session()
        proc = self.ingest("--tool", "grok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(self.out + ".tmp"))
        lines = [json.loads(x) for x in self.current().decode().splitlines() if x.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual({e["tool"] for e in lines}, {"grok"})

    def test_a_partial_run_says_it_is_replacing_a_full_corpus(self):
        self.add_grok_session()
        proc = self.ingest("--tool", "grok", "--limit", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("partial run", proc.stdout)
        self.assertIn("--limit 1", proc.stdout)

    def test_a_full_run_stays_quiet_about_partials(self):
        self.add_grok_session()
        self.assertNotIn("partial run", self.ingest().stdout)


if __name__ == "__main__":
    unittest.main()
