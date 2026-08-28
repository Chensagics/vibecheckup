"""Bootstrap tests: the demo corpus generator and vibecheck.sh.

Never touches real session data or the repo's data/ directory -- the generator
is always pointed at a temporary file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
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
EVENT_FIELDS = set(Event.__slots__) | {"model", "usage"}
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


if __name__ == "__main__":
    unittest.main()
