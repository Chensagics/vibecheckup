"""Regression tests for two launch bugs.

1. The tokenizer was ASCII-only. Accented Latin came back mangled ('función'
   -> 'funci') and a Hebrew, Arabic or Cyrillic corpus tokenized to nothing at
   all, which used to abort stage 2 outright.
2. Day, hour, weekday, streak and busiest-day were read out of the UTC ISO
   string while spend keyed its days on local time, so the dashboard put a UTC
   answer on a share card that claims to describe the user's own evening.

Every corpus here is built in a temp directory. Nothing touches real session
data, and the timezone is pinned per run so the numbers are the same on every
machine.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from analyze import local_stamp, vocab_path  # noqa: E402
from wcstats.spend import local_date  # noqa: E402
from wcstats.tokenize import raw_tokens, tokens  # noqa: E402

ANALYZE = os.path.join(ROOT, "analyze.py")

# Fixed points chosen so UTC and local disagree about both the hour and the
# day: 22:30 UTC on a Saturday is 01:30 Sunday in Jerusalem and 15:30 Saturday
# in Los Angeles.
LATE = "2026-08-01T22:30:00+00:00"
LATER = "2026-08-02T21:00:00+00:00"

PROMPTS = ["fix the failing payment retry",
           "profile the slow migration script",
           "rewrite the cache eviction policy"]


def event(ts, text, role="user", kind="prompt", **kw):
    ev = {"ts": ts, "tool": "claude_code", "session_id": "s1",
          "project": "demo", "role": role, "kind": kind, "text": text,
          "ts_exact": True, "confidence": "exact"}
    ev.update(kw)
    return ev


def run_analyze(events, tz, tmp):
    """Run stage 2 on a throwaway corpus under a pinned timezone.

    A subprocess rather than an in-process call: TZ has to be set before the C
    library reads it, and this keeps the parent test process's clock alone.
    """
    inp = os.path.join(tmp, "events.ndjson")
    out = os.path.join(tmp, "stats.json")
    with open(inp, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    env = dict(os.environ, TZ=tz)
    proc = subprocess.run([sys.executable, ANALYZE, "--in", inp, "--out", out],
                          capture_output=True, text=True, env=env)
    stats = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            stats = json.load(fh)
    return proc, stats, out


class ScratchCase(unittest.TestCase):
    """A throwaway directory per test, removed afterwards."""

    def scratch(self):
        tmp = tempfile.mkdtemp(prefix="vibecheck-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp


class TestUnicodeTokens(unittest.TestCase):
    """The bug that produced hero words like 'funci' and 'berpr'."""

    def test_accented_latin_survives_whole(self):
        got = tokens("revisa la función del módulo de pagos")
        self.assertIn("función", got)
        self.assertIn("módulo", got)
        for fragment in ("funci", "dulo", "func"):
            self.assertNotIn(fragment, got)

    def test_german_and_polish_are_not_truncated(self):
        self.assertIn("überprüfe", tokens("bitte überprüfe das Modul"))
        # 'błąd' carries no aeiouy at all, so the gibberish heuristic used to
        # drop it even once the regex let it through.
        got = tokens("to jest błąd w module płatności")
        self.assertIn("błąd", got)
        self.assertIn("płatności", got)

    def test_hebrew_corpus_is_not_empty(self):
        got = tokens("תקן את הבאג בקוד ותוסיף בדיקה")
        self.assertTrue(got, "Hebrew prose must produce tokens")
        self.assertIn("בדיקה", got)

    def test_cyrillic_corpus_is_not_empty(self):
        got = tokens("исправь ошибку в модуле оплаты")
        self.assertTrue(got, "Russian prose must produce tokens")
        self.assertIn("ошибку", got)

    def test_arabic_content_words_kept_function_words_dropped(self):
        got = tokens("افتح الرسم البياني على السعر إلى الأعلى")
        self.assertIn("السعر", got)      # "the price" -- real vocabulary
        self.assertNotIn("على", got)     # "on" -- a preposition, like "on"
        self.assertNotIn("إلى", got)     # "to"

    def test_scripts_do_not_glue_together(self):
        """An unescaped \\n leaves a bare 'n' against the next word; one
        Unicode class for every script would fuse them into 'nهذه'."""
        self.assertEqual(raw_tokens("nهذه"), ["n", "هذه"])

    def test_composed_and_decomposed_spellings_count_once(self):
        nfd = unicodedata.normalize("NFD", "la función del módulo")
        self.assertEqual(tokens(nfd), tokens("la función del módulo"))

    def test_vowel_heuristic_still_rejects_latin_gibberish(self):
        """The hash/identifier filter has to survive the i18n fix."""
        self.assertEqual(tokens("zzz tsk grrr hmmm"), [])


class TestAsciiBehaviourUnchanged(unittest.TestCase):
    """The shapes the existing corpus depends on, pinned against regression."""

    def test_known_token_shapes(self):
        got = raw_tokens("write it in c++ and read the .py file, "
                         "don't forget my e-mail")
        for shape in ("c++", "py", "don't", "e-mail"):
            self.assertIn(shape, got)

    def test_digits_and_underscores_cannot_open_a_token(self):
        self.assertEqual(tokens("a b 12 x9y"), [])
        self.assertEqual(raw_tokens("_tmp 12345"), ["tmp"])

    def test_stopwords_still_removed_domain_verbs_still_kept(self):
        got = tokens("Please just go and fix the failing deploy test now")
        self.assertNotIn("please", got)
        self.assertIn("deploy", got)


class TestLocalStamp(unittest.TestCase):
    """analyze.local_stamp must agree with spend.local_date, always."""

    def setUp(self):
        if not hasattr(time, "tzset"):
            self.skipTest("TZ pinning needs a POSIX tzset")
        self._old = os.environ.get("TZ")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old
        time.tzset()

    def _tz(self, name):
        os.environ["TZ"] = name
        time.tzset()

    def test_east_of_greenwich_rolls_the_day_forward(self):
        self._tz("Asia/Jerusalem")
        self.assertEqual(local_stamp(LATE), ("2026-08-02", 1, "Sun"))

    def test_west_of_greenwich_keeps_the_earlier_day(self):
        self._tz("America/Los_Angeles")
        self.assertEqual(local_stamp(LATE), ("2026-08-01", 15, "Sat"))

    def test_utc_matches_the_raw_string(self):
        self._tz("UTC")
        self.assertEqual(local_stamp(LATE), ("2026-08-01", 22, "Sat"))

    def test_day_never_disagrees_with_spend(self):
        """Two day boundaries in one dashboard is the whole bug."""
        for tz in ("Asia/Jerusalem", "America/Los_Angeles", "UTC",
                   "Pacific/Kiritimati"):
            self._tz(tz)
            for ts in (LATE, LATER, "2026-01-01T00:05:00+00:00"):
                self.assertEqual(local_stamp(ts)[0], local_date(ts),
                                 f"{tz} {ts}")

    def test_malformed_timestamp_is_skipped_not_fatal(self):
        """One bad record used to raise ValueError out of int(ts[11:13]) and
        take the entire stage down with it."""
        self._tz("Asia/Jerusalem")
        for junk in ("", None, "nope", "2026-13-99T00:00:00Z", 12345):
            self.assertEqual(local_stamp(junk), (None, None, None), repr(junk))

    def test_date_without_a_clock_still_counts_as_a_day(self):
        self._tz("Asia/Jerusalem")
        day, hour, weekday = local_stamp("2026-08-02")
        self.assertEqual((day, weekday), ("2026-08-02", "Sun"))
        self.assertIsNone(hour)


class TestAnalyzeUsesLocalTime(ScratchCase):
    """End to end: the same events, two timezones, two different peak hours."""

    @classmethod
    def setUpClass(cls):
        cls.events = [event(LATE, t) for t in PROMPTS]
        cls.events.append(event(LATER, "ship the release notes"))
        cls.events.append(event(LATE, "done", role="assistant", kind="reply",
                                model="claude-opus-5",
                                usage={"input": 10, "output": 20,
                                       "cache_read": 0, "cache_write": 0}))

    def _run(self, tz):
        tmp = self.scratch()
        proc, stats, _ = run_analyze(self.events, tz, tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return stats

    def test_peak_hour_is_local_not_utc(self):
        east = self._run("Asia/Jerusalem")
        west = self._run("America/Los_Angeles")
        # 22:30 UTC is the raw string, and the pre-fix answer in both places.
        self.assertEqual(east["wrapped"]["peak_hour"], 1)
        self.assertEqual(west["wrapped"]["peak_hour"], 15)
        self.assertNotEqual(east["wrapped"]["peak_hour"], 22)
        self.assertNotEqual(west["wrapped"]["peak_hour"], 22)

    def test_weekday_and_day_follow_the_same_boundary(self):
        east = self._run("Asia/Jerusalem")
        self.assertEqual(east["wrapped"]["peak_weekday"], "Sun")
        self.assertEqual(east["activity"]["per_day"],
                         {"2026-08-02": 3, "2026-08-03": 1})
        self.assertEqual(east["wrapped"]["busiest_day"],
                         {"date": "2026-08-02", "prompts": 3})
        west = self._run("America/Los_Angeles")
        self.assertEqual(west["wrapped"]["peak_weekday"], "Sat")
        self.assertEqual(west["activity"]["per_day"],
                         {"2026-08-01": 3, "2026-08-02": 1})

    def test_activity_days_and_spend_days_are_the_same_days(self):
        for tz in ("Asia/Jerusalem", "America/Los_Angeles"):
            stats = self._run(tz)
            spend_days = {row["date"] for row in stats["spend"]["by_day"]}
            self.assertTrue(spend_days)
            self.assertLessEqual(spend_days, set(stats["activity"]["per_day"]),
                                 f"{tz}: spend billed a day activity never saw")

    def test_streak_counts_consecutive_local_days(self):
        east = self._run("Asia/Jerusalem")
        self.assertEqual(east["wrapped"]["longest_streak_days"], 2)

    def test_one_malformed_timestamp_does_not_kill_the_stage(self):
        tmp = self.scratch()
        events = list(self.events) + [event("not-a-timestamp", "check the logs")]
        proc, stats, _ = run_analyze(events, "Asia/Jerusalem", tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stats["source_events"], len(events))
        self.assertNotIn("not-a-timestamp", json.dumps(stats["activity"]))


class TestTokenlessCorpusStillBuilds(unittest.TestCase):
    """Zero surviving prose is a warning, not a failure: spend, activity and
    the wrapped counts are all still real, and half a dashboard beats none."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vibecheck-test-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        events = [event("2026-08-01T22:30:00+00:00", "ok thanks"),
                  event("2026-08-01T23:10:00+00:00", "yes please"),
                  event("2026-08-02T21:00:00+00:00", "sure, thanks!"),
                  event("2026-08-01T22:30:00+00:00", "ok", role="assistant",
                        kind="reply", model="claude-opus-5",
                        usage={"input": 10, "output": 20, "cache_read": 0,
                               "cache_write": 0})]
        cls.proc, cls.stats, cls.out = run_analyze(events, "Asia/Jerusalem",
                                                   cls.tmp)

    def test_exits_zero_and_writes_stats(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIsNotNone(self.stats)

    def test_warns_and_says_what_will_be_empty(self):
        self.assertIn("WARNING", self.proc.stderr)
        self.assertNotIn("FATAL", self.proc.stderr)
        self.assertIn("zero user prose", self.proc.stderr)

    def test_lexical_views_are_empty_but_present(self):
        g = self.stats["clouds"]["global"]
        for key in ("prose_user", "prose_assistant", "phrases_user"):
            self.assertEqual(g[key], [], key)
        self.assertEqual(self.stats["trends"]["rising"], [])
        self.assertEqual(self.stats["wrapped"]["top_words"], [])
        self.assertEqual(self.stats["wrapped"]["top_phrase"], "")

    def test_the_non_lexical_dashboard_still_works(self):
        self.assertEqual(self.stats["spend"]["total_tokens"], 30)
        self.assertEqual(self.stats["activity"]["per_day"],
                         {"2026-08-02": 2, "2026-08-03": 1})
        self.assertEqual(len(self.stats["activity"]["hour_histogram"]), 24)
        self.assertEqual(len(self.stats["activity"]["weekday_histogram"]), 7)
        w = self.stats["wrapped"]
        self.assertEqual(w["days_active"], 2)
        self.assertEqual(w["politeness"], {"please": 1, "thanks": 2, "sorry": 0})
        self.assertEqual(w["busiest_day"], {"date": "2026-08-02", "prompts": 2})

    def test_the_page_can_still_be_assembled(self):
        """build_dashboard.py refuses to build without these keys."""
        for key in ("schema_version", "clouds", "coverage", "totals",
                    "activity"):
            self.assertIn(key, self.stats)


class TestVocabSidecarFollowsOut(ScratchCase):
    """The sidecar path used to be hard-coded, so any run with a scratch --out
    still overwrote the real data/vocab.json."""

    def test_sidecar_is_written_beside_out(self):
        tmp = self.scratch()
        proc, _, out = run_analyze([event(LATE, PROMPTS[0])], "UTC", tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(tmp, "vocab.json")))

    def test_a_scratch_out_never_resolves_into_the_repo(self):
        scratch = vocab_path(os.path.join(tempfile.gettempdir(), "stats.json"))
        self.assertNotEqual(os.path.dirname(scratch),
                            os.path.join(ROOT, "data"))
        # The default --out keeps the documented data/vocab.json name.
        self.assertEqual(vocab_path(os.path.join(ROOT, "data", "stats.json")),
                         os.path.join(ROOT, "data", "vocab.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
