"""vocab.json is stats.json with the top-N cut taken OFF, and it shipped ungated.

That made it the most exposed file the tool writes, not the least. Everything
below rank 300 is invisible to stats.json and to the dashboard, and that is
precisely where the identifying tail lives. Found in the owner's real
data/vocab.json, all of it absent from stats.json:

  * the owner's forename and surname, and their possessives;
  * a laptop model identifier, out of a `Model Identifier: ...` line in a crash
    report somebody pasted into a prompt;
  * a private repository name, once bare and once with a trailing `#`;
  * opaque high-entropy blobs out of thinking events;
  * ~3,075 dotted filenames -- a source-tree inventory of unreleased products,
    and not one of them a word anybody used.

Three mechanisms catch those, and every class needs a different one, which is
why all three are pinned here: the redaction list knows the owner by name (and
so also catches a repo named after them), `looks_harvested` -- the same shape
rule the shareable dashboard filters on -- knows a filename from a word, and
the frequency floor catches what has no shape at all. A laptop model and a
random blob read as ordinary rare words; what gives them away is a count of
one, which is also what makes them worthless as vocabulary.

The literals below are STAND-INS of the same shape as the real ones. Quoting
the originals would put a private repository name and a source-tree listing
into a tracked file, which is the exact thing this gate exists to prevent --
and unlike the labels test_projects.py quotes, none of these ever reached a
file that was shared.

Synthetic throughout: nothing here reads data/.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from analyze import (VOCAB_FLOOR_MIN_TERMS, VOCAB_MIN_COUNT,  # noqa: E402
                     gate_vocab)
from build_dashboard import _leaks, _name_patterns, account_names  # noqa: E402
from wcstats.clean import Redactor  # noqa: E402
from wcstats.facets import Bucket  # noqa: E402

IDENTITY = "zephyrine"           # the owner, as far as this corpus knows
PROJECT = "quicksilver"          # a repo name, which is a machine string

# One stand-in per class that shipped, grouped by what has to catch it.
FILENAMES = ["tradedesk.tsx", "orderstore.ts", "campaignview.ts",
             "sim_qa.py", "collect_vendor.py"]          # a shape
OWNED = [IDENTITY + "site", IDENTITY + "site#"]         # the redaction list
SHAPELESS = ["laptoppro18",                             # only the floor
             "eup6arapc42ikdupn6krqa0", "qer6arjwh56jnsepidjeoa4"]
LEAKED_TERMS = FILENAMES + OWNED + SHAPELESS


def bucket_from(user=(), assistant=(), tools=(), commands=(), errors=()):
    """A global Bucket with the counters gate_vocab() actually reads."""
    b = Bucket()
    b.prose_user = Counter(dict(user))
    b.prose_asst = Counter(dict(assistant))
    b.tools = Counter(dict(tools))
    b.commands = Counter(dict(commands))
    b.errors = Counter(dict(errors))
    return b


def filler(n, count=9):
    """`n` distinct ordinary words, each frequent enough to clear the floor."""
    return {"word%04d" % i: count for i in range(n)}


class TestTheGateItself(unittest.TestCase):
    """gate_vocab() in isolation, with no corpus and no subprocess."""

    def gate(self, bucket, literals=(), projects=()):
        return gate_vocab(bucket, Redactor(literals), list(projects))

    def test_a_redacted_literal_never_reaches_the_file(self):
        b = bucket_from(user={"combat-engine": 40, "pipeline": 40})
        out, report = self.gate(b, literals=["combat-engine"])
        self.assertEqual(out["prose_user"], [["pipeline", 40]])
        self.assertEqual(report["redacted"], 1)

    def test_a_redacted_literal_goes_with_the_whole_token(self):
        """`graphify-out` and `graphify's` are the branch too."""
        b = bucket_from(user={"graphify-out": 9, "graphifyignore": 9,
                              "graph": 9})
        out, _ = self.gate(b, literals=["graphify"])
        self.assertEqual([t for t, _n in out["prose_user"]], ["graph"])

    def test_dotted_identifiers_are_not_vocabulary(self):
        b = bucket_from(user={"tradedesk.tsx": 30, "orderstore.ts": 30,
                              "trade": 30})
        out, report = self.gate(b)
        self.assertEqual([t for t, _n in out["prose_user"]], ["trade"])
        self.assertEqual(report["harvested"], 2)

    def test_paths_handles_and_urls_are_not_vocabulary(self):
        b = bucket_from(user={"src/components": 8, "@octocat": 8,
                              "https:": 8, "components": 8})
        out, _ = self.gate(b)
        self.assertEqual([t for t, _n in out["prose_user"]], ["components"])

    def test_account_and_project_names_go(self):
        accounts = account_names()
        self.assertFalse([a for a in accounts if a in "deploy"],
                         "the control word collides with this machine's login")
        b = bucket_from(user={sorted(accounts)[0]: 12, PROJECT: 12,
                              "deploy": 12})
        out, _ = self.gate(b, projects=[PROJECT])
        self.assertEqual([t for t, _n in out["prose_user"]], ["deploy"])

    def test_the_floor_catches_what_has_no_shape(self):
        """A laptop model and a base32 blob read as ordinary rare words. The
        only thing that marks them is that nobody ever said them twice."""
        user = filler(VOCAB_FLOOR_MIN_TERMS)
        user.update({t: 1 for t in SHAPELESS})
        out, report = self.gate(bucket_from(user=user))
        kept = {t for t, _n in out["prose_user"]}
        self.assertEqual(len(kept), VOCAB_FLOOR_MIN_TERMS)
        for term in SHAPELESS:
            with self.subTest(term=term):
                self.assertNotIn(term, kept)
        self.assertEqual(report["rare"], len(SHAPELESS))

    def test_everything_at_or_above_the_floor_is_kept(self):
        user = filler(VOCAB_FLOOR_MIN_TERMS)
        user["borderline"] = VOCAB_MIN_COUNT
        out, _ = self.gate(bucket_from(user=user))
        self.assertIn("borderline", {t for t, _n in out["prose_user"]})

    def test_a_small_corpus_keeps_its_hapax(self):
        """In forty words a hapax is not the tail, it is the corpus. Emptying
        the file would be a worse answer than gating it."""
        user = filler(20, count=1)
        out, report = self.gate(bucket_from(user=user))
        self.assertEqual(len(out["prose_user"]), 20)
        self.assertEqual(report["rare"], 0)
        self.assertEqual(report["floored"], [])

    def test_the_floor_is_reported_when_it_is_applied(self):
        out, report = self.gate(bucket_from(user=filler(VOCAB_FLOOR_MIN_TERMS)))
        self.assertIn("prose_user", report["floored"])
        self.assertTrue(out["prose_user"])

    def test_the_floor_does_not_apply_to_tools_or_commands(self):
        """Those are closed vocabularies, not a tail: a tool used once is a
        real fact about the corpus and carries nothing off the machine."""
        b = bucket_from(tools={"NotebookEdit": 1}, commands={"jq": 1})
        out, report = self.gate(b)
        self.assertEqual(out["tools"], [["NotebookEdit", 1]])
        self.assertEqual(out["commands"], [["jq", 1]])
        self.assertEqual(report["rare"], 0)

    def test_a_locally_written_script_is_still_not_a_command(self):
        b = bucket_from(commands={"herd.sh": 40, "git": 40})
        out, _ = self.gate(b)
        self.assertEqual([t for t, _n in out["commands"]], ["git"])

    def test_error_signatures_are_not_in_the_file_at_all(self):
        b = bucket_from(errors={"Exit code N": 300,
                                "BLOCKED: 'X' while checked out on main_x": 4})
        out, _ = self.gate(b)
        self.assertNotIn("errors", out)
        self.assertNotIn("main_x", json.dumps(out))

    def test_the_file_says_what_it_is(self):
        out, _ = self.gate(bucket_from(user={"pipeline": 9}))
        self.assertIn("_about", out)
        self.assertIn("GATED", out["_about"])

    def test_nothing_that_survives_would_be_cut_from_the_shareable_dashboard(self):
        """The gate is the shareable filter plus a floor, so by construction
        no surviving term may fail the shareable filter."""
        user = dict(filler(VOCAB_FLOOR_MIN_TERMS))
        user.update({t: 30 for t in LEAKED_TERMS})
        user[PROJECT] = 30
        out, _ = self.gate(bucket_from(user=user, assistant=user),
                           projects=[PROJECT])
        pats = _name_patterns([PROJECT])
        accounts = account_names()
        for facet in ("prose_user", "prose_assistant", "tools", "commands"):
            for term, _n in out[facet]:
                with self.subTest(facet=facet, term=term):
                    self.assertFalse(_leaks(term, accounts, pats))

    def test_the_gate_does_not_empty_a_real_vocabulary(self):
        """A privacy fix that hands back nothing is not a fix. Ordinary words
        at ordinary frequencies all survive."""
        user = dict(filler(VOCAB_FLOOR_MIN_TERMS))
        user.update({t: 1 for t in LEAKED_TERMS})
        out, report = self.gate(bucket_from(user=user))
        self.assertEqual(len(out["prose_user"]), VOCAB_FLOOR_MIN_TERMS)
        self.assertGreater(report["kept"], report["rare"])


# --- end to end at the publication boundary ----------------------------------

# Words standing next to every leak, which all have to survive.
KEEPWORDS = ["pipeline", "telemetry", "rewrite", "harness", "scripts",
             "assets", "rollout", "docs"]

# One prompt each. A leak that is said once is the case the floor exists for,
# and it is also how these actually occurred: a pasted crash report, a thinking
# event, one sentence naming a repo.
LEAK_PROSE = [
    "the %s and %s screens need a rewrite" % (FILENAMES[0], FILENAMES[1]),
    "%s drives %s and %s inside the harness" % tuple(FILENAMES[2:5]),
    "Model Identifier: %s crashed while running the scripts" % SHAPELESS[0],
    "thinking about %s and %s for the assets" % (SHAPELESS[1], SHAPELESS[2]),
    "open the %s repo and the %s tracker for the rollout" % tuple(OWNED),
    "%s owns the %s docs and the telemetry pipeline" % (IDENTITY, PROJECT),
]


def synth_words(n, prefix="lex"):
    """`n` distinct all-alphabetic tokens.

    Alphabetic on purpose: the cleaner strips numbers out of prose before it
    tokenizes, so a filler spelled with digits would collapse every prompt onto
    one near-duplicate key and leave a vocabulary of five words.
    """
    a = "abcdefghijklmnopqrstuvwxyz"
    return [prefix + a[i // 676 % 26] + a[i // 26 % 26] + a[i % 26]
            for i in range(n)]


class TestEndToEnd(unittest.TestCase):
    """Run analyze.py over a corpus built to leak and read the vocab.json."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = os.path.join(cls.tmp, "events.ndjson")
        words = synth_words(VOCAB_FLOOR_MIN_TERMS + 100)
        rows = []

        def add(i, text, role, kind):
            rows.append({
                "ts": "2026-08-%02dT%02d:00:00+00:00" % (i % 28 + 1, i % 24),
                "tool": "claude_code", "session_id": "s%d" % i,
                "project": PROJECT, "role": role, "kind": kind,
                "text": text, "ts_exact": True, "confidence": "exact"})

        # A real vocabulary: every filler said twice, so the floor keeps it and
        # only the leaks fall through. Below VOCAB_FLOOR_MIN_TERMS distinct
        # terms the floor does not engage at all, which is what a toy corpus
        # would have tested instead.
        tail = " ".join(KEEPWORDS)
        for i, word in enumerate(words):
            text = "%s %s rebuild the deploy %s" % (
                word, words[(i + 1) % len(words)], tail)
            add(i, text, "user", "prompt")
            add(i, text, "assistant", "reply")
        for j, text in enumerate(LEAK_PROSE):
            add(len(words) + j, text, "user", "prompt")
            add(len(words) + j, text, "assistant", "reply")

        with open(src, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        cls.out = os.path.join(cls.tmp, "stats.json")
        cls.vocab_path = os.path.join(cls.tmp, "vocab.json")
        env = dict(os.environ)
        env["VIBECHECKUP_REDACT_FILE"] = os.path.join(cls.tmp, "no-such-file")
        env["HOME"] = os.path.join(cls.tmp, "Users", IDENTITY)
        os.makedirs(env["HOME"])
        cls.proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "analyze.py"), "--in", src,
             "--out", cls.out, "--vocab", cls.vocab_path, "--redact", IDENTITY],
            capture_output=True, text=True, timeout=300, env=env)
        try:
            with open(cls.vocab_path, encoding="utf-8") as fh:
                cls.raw = fh.read()
        except OSError:
            cls.raw = ""
        cls.vocab = json.loads(cls.raw) if cls.raw else {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_run_succeeded(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_none_of_the_shipped_leaks_reach_vocab_json(self):
        for term in LEAKED_TERMS:
            with self.subTest(term=term):
                self.assertNotIn(term, self.raw)

    def test_the_owner_and_the_repo_do_not_reach_vocab_json(self):
        self.assertNotIn(IDENTITY, self.raw)
        self.assertNotIn(PROJECT, self.raw)

    def test_no_dotted_identifier_survives_anywhere_in_the_file(self):
        """The source-tree inventory was the bulk of it: 3,075 entries."""
        for facet, items in self.vocab.items():
            if not isinstance(items, list):
                continue
            for term, _n in items:
                with self.subTest(facet=facet, term=term):
                    self.assertNotRegex(term, r"\w\.\w")

    def test_the_prose_around_the_leaks_survives(self):
        words = dict(self.vocab["prose_user"])
        for word in KEEPWORDS:
            with self.subTest(word=word):
                self.assertGreater(words.get(word, 0), 0)

    def test_the_file_is_still_the_long_tail_and_not_the_top_n(self):
        """The point of vocab.json is what stats.json cuts off. Gating must
        not quietly turn it into a second copy of the top 300."""
        with open(self.out, encoding="utf-8") as fh:
            stats = json.load(fh)
        top = stats["clouds"]["global"]["prose_assistant"]
        self.assertGreater(len(self.vocab["prose_assistant"]), len(top))

    def test_no_error_signature_key(self):
        self.assertNotIn("errors", self.vocab)

    def test_the_terminal_says_what_was_cut(self):
        self.assertIn("vocab gate", self.proc.stdout)

    def test_the_gate_is_reported_without_naming_what_it_dropped(self):
        """A list of exactly the strings somebody wanted hidden is the leak,
        spelled out -- in the terminal as much as in the file."""
        line = [ln for ln in self.proc.stdout.splitlines()
                if ln.startswith("vocab gate")]
        self.assertTrue(line)
        for term in LEAKED_TERMS:
            self.assertNotIn(term, line[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
