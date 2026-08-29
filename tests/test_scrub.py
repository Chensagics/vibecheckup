"""build_dashboard.py --scrub: the shareable copy.

Runs on a synthetic stats blob only -- it never reads data/stats.json, and it
writes nothing outside a temporary directory.

The blob is seeded with the exact classes of string the audit found inlined in
dashboard.html: an account name, project and worktree directory names, raw
error text, shell commands and the names of the MCP servers this machine is
wired to. Every test that matters here asks the same question -- can any of
them still be found in the built page.
"""
from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import build_dashboard as bd  # noqa: E402

USER = "zorbax"
PROJECTS = ["quillfish", "quillfish--claude-worktrees-native-ota",
            "graphify-ab-control", "combat-engine", "dyslexic-type", "keep"]
ERRORS = ["Traceback (most recent call last):",
          "Failed to create project 'X' for zorbaxPATH: Vercel API error N",
          "fix(paywall): drop the trial gate before the Q3 launch"]
COMMANDS = ["herdr", "herd.sh", "heartbeat.sh", "rotate.sh", "git"]
SECRETS = [USER, "zorbaxics", "mejanreteam.zorbax.com", "com.acme.quillfish",
           "quillfish", "graphify-ab-control", "combat-engine",
           "dyslexic-type", "native-ota"]
# The MCP servers this machine is wired to: a vendor list, read out of the
# agent's configuration rather than typed by anybody. `meta-ads` on its own
# asserts that its owner runs Meta ad accounts.
MCP_TOOLS = ["mcp__meta-ads__ads_get_ad_entities",
             "mcp__ios-simulator__ui_tap",
             "mcp__ios-simulator__screenshot",
             "mcp__claude-in-chrome__screenshot",
             "mcp__claude_ai_Supabase__execute_sql"]
MCP_SERVERS = ["meta-ads", "ios-simulator", "claude-in-chrome",
               "claude_ai_Supabase"]


def bucket(extra=None):
    """One facet bucket shaped like analyze.py writes them."""
    b = {
        "prose_user": [{"t": "refactor", "n": 9}, {"t": "quillfish", "n": 8},
                       {"t": "keep", "n": 7}, {"t": "zorbaxics", "n": 6},
                       {"t": "com.acme.quillfish", "n": 5},
                       {"t": "combat-engine", "n": 4}],
        "prose_user_raw": [{"t": "refactor", "n": 11},
                           {"t": "mejanreteam.zorbax.com", "n": 3}],
        "prose_assistant": [{"t": "done", "n": 4}, {"t": "zorbax staff", "n": 2}],
        "phrases_user": [{"t": "dev server", "n": 5, "pmi": 8.1},
                         {"t": "quillfish app", "n": 4, "pmi": 7.0},
                         {"t": "combat engine", "n": 3, "pmi": 6.2}],
        "phrases_assistant": [{"t": "word cloud", "n": 3, "pmi": 5.5}],
        "distinctive_user": [{"t": "refactor", "n": 9, "z": 3.1},
                             {"t": "dyslexic-type", "n": 2, "z": 2.2}],
        "distinctive_assistant": [{"t": "done", "n": 4, "z": 1.4}],
        "tools": ([{"t": "Bash", "n": 20}]
                  + [{"t": t, "n": n} for n, t in
                     zip((9, 8, 7, 6, 5), MCP_TOOLS)]),
        "extensions": [{"t": "py", "n": 12}, {"t": "quillfish", "n": 1}],
        "commands": [{"t": c, "n": 3} for c in COMMANDS],
        "errors": [{"t": e, "n": 2} for e in ERRORS],
        "counts": {"events": 100, "prompts": 10, "sessions": 4, "words": 900},
    }
    if extra:
        b.update(extra)
    return b


def synthetic():
    return {
        "schema_version": 2,
        "generated_at": "2026-08-28T22:20:29Z",
        "source_events": 100,
        "coverage": {"claude_code": {"first": "2026-07-01T00:00:00+00:00",
                                     "last": "2026-08-01T00:00:00+00:00",
                                     "sessions": 4, "events": 100,
                                     "months": {"2026-07": 100},
                                     "heuristic_events": 0}},
        "totals": {"sessions": 4, "events": 100, "prompts": 10, "words": 900,
                   "llm_tokens": 1000, "projects": len(PROJECTS), "tools": 1},
        "clouds": {
            "global": bucket(),
            "by_tool": {"claude_code": bucket()},
            "by_project": {p: bucket() for p in PROJECTS},
            "by_month": {"2026-07": bucket()},
        },
        "trends": {"rising": [{"t": "loop", "early": 1.0, "late": 9.0,
                               "lift": 3.0, "n": 30},
                              {"t": "quillfish", "early": 1.0, "late": 4.0,
                               "lift": 2.0, "n": 8}],
                   "fading": [{"t": "keep", "early": 5.0, "late": 1.0,
                               "lift": -2.0, "n": 6}],
                   "early_months": ["2026-07"], "late_months": ["2026-08"]},
        "spend": {"currency": "USD", "estimates_note": "list-price estimates",
                  "total_cost": 12.5, "total_tokens": 1000,
                  "by_day": [{"date": "2026-07-01", "cost": 12.5,
                              "tokens": 1000}],
                  "by_model": [], "by_tool": [], "cache_hit_rate": 0.5,
                  "unpriced_models": [], "tools_without_usage": [],
                  "tokens": {"input": 1, "output": 1, "cache_read": 1,
                             "cache_write": 1},
                  "events": 10},
        "wrapped": {"window": {"start": "2026-07-01", "end": "2026-08-01"},
                    "year": 2026, "words_to_ai": 900, "prompts": 10,
                    "sessions": 4, "days_active": 3, "longest_streak_days": 2,
                    "top_words": [["refactor", 9], ["quillfish", 8],
                                  ["keep", 7]],
                    "top_phrase": "quillfish app", "rising_word": "loop",
                    "peak_hour": 1, "peak_weekday": "Tue",
                    "busiest_day": {"date": "2026-07-02", "prompts": 6},
                    "politeness": {"please": 2, "thanks": 1, "sorry": 0},
                    "top_tool": {"name": "claude_code", "share": 1.0},
                    "tools_used": 1, "projects_count": len(PROJECTS),
                    "spend": {"total": 12.5,
                              "priciest_day": {"date": "2026-07-01",
                                               "cost": 12.5}}},
        "activity": {"per_day": {"2026-07-01": 4, "2026-07-02": 6},
                     "hour_histogram": {"1": 10},
                     "weekday_histogram": {"Tue": 10},
                     "session_lengths": [
                         {"t": "claude_code:5b78b15b-d3b7-4269-bbcc-7e6e", "n": 60}],
                     "top_projects": [{"t": p, "n": 5, "events": 20,
                                       "sessions": 2,
                                       "tools": {"claude_code": 20}}
                                      for p in PROJECTS],
                     "sessions_by_tool": {"claude_code": 4}},
    }


def build(argv, stats=None):
    """Run the real builder against the real template, into a temp dir."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "stats.json")
        out = os.path.join(d, "dashboard.html")
        scrub = os.path.join(d, "dashboard-shareable.html")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(stats if stats is not None else synthetic(), fh)
        keep = (bd.STATS, bd.OUT, bd.SCRUB_OUT)
        bd.STATS, bd.OUT, bd.SCRUB_OUT = src, out, scrub
        def read(p):
            if not os.path.exists(p):
                return None
            with open(p, encoding="utf-8") as fh:
                return fh.read()

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bd.main(argv)
            return rc, read(out), read(scrub)
        finally:
            bd.STATS, bd.OUT, bd.SCRUB_OUT = keep


def payload(html):
    m = re.search(r'<script id="statsdata" type="application/json">(.*?)</script>',
                  html, re.S)
    assert m, "inlined stats block missing"
    return m.group(1)


def inlined(html):
    return json.loads(payload(html).replace("\\u003c", "<"))


class TestScrubRemovesTheHarvestedFacets(unittest.TestCase):
    """The three facets the audit named are not filtered, they are gone."""

    @classmethod
    def setUpClass(cls):
        cls.scrubbed, _ = bd.scrub_stats(synthetic(), [USER])

    def test_no_project_cloud_survives(self):
        keys = list(self.scrubbed["clouds"]["by_project"])
        for p in PROJECTS:
            self.assertNotIn(p, keys)
        # Emptied outright. An earlier version kept one placeholder entry so the
        # tab was not a heading over nothing; the template now reads share_mode
        # and says so in its own sub-line, so a placeholder would say it twice.
        self.assertEqual(keys, [])
        self.assertIs(self.scrubbed["share_mode"], True)

    def test_every_errors_and_commands_list_is_replaced(self):
        notice = [{"t": bd.NOTICE, "n": 0}]
        clouds = self.scrubbed["clouds"]
        buckets = [clouds["global"]]
        for group in ("by_tool", "by_project", "by_month"):
            buckets += list(clouds[group].values())
        seen = 0
        for b in buckets:
            for facet in ("errors", "commands"):
                if facet in b:
                    seen += 1
                    self.assertEqual(b[facet], notice)
        self.assertGreater(seen, 0, "no errors/commands facets were examined")

    def test_project_names_are_replaced_not_dropped_in_activity(self):
        rows = self.scrubbed["activity"]["top_projects"]
        self.assertEqual(len(rows), len(PROJECTS), "the shape must survive")
        self.assertEqual([r["t"] for r in rows],
                         ["project %02d" % i for i in range(1, len(rows) + 1)])
        self.assertEqual(rows[0]["n"], 5, "the counts must survive")

    def test_session_ids_are_replaced(self):
        rows = self.scrubbed["activity"]["session_lengths"]
        self.assertEqual([r["t"] for r in rows], ["session 01"])
        self.assertEqual(rows[0]["n"], 60)

    def test_the_prose_clouds_survive(self):
        """A scrub that removes everything is not a share mode."""
        g = self.scrubbed["clouds"]["global"]
        self.assertIn({"t": "refactor", "n": 9}, g["prose_user"])
        self.assertIn("dev server", [d["t"] for d in g["phrases_user"]])
        self.assertTrue(self.scrubbed["trends"]["rising"])
        self.assertEqual(self.scrubbed["spend"]["total_cost"], 12.5)
        self.assertEqual(self.scrubbed["wrapped"]["prompts"], 10)
        self.assertEqual(self.scrubbed["activity"]["hour_histogram"], {"1": 10})


class TestScrubRemovesIdentifiersFromTheKeptClouds(unittest.TestCase):
    """Dropping the facets is not enough: the prose clouds carry the account
    name (it reaches them through quoted `ls -l` output) and the repo names
    (the owner typed them)."""

    @classmethod
    def setUpClass(cls):
        cls.scrubbed, cls.report = bd.scrub_stats(synthetic(), [USER])
        cls.g = cls.scrubbed["clouds"]["global"]

    def terms(self, facet):
        return [d["t"] for d in self.g[facet]]

    def test_account_name_is_gone_including_its_variants(self):
        for facet in ("prose_user", "prose_user_raw", "prose_assistant"):
            for t in self.terms(facet):
                self.assertNotIn(USER, t.lower(), f"{facet}: {t}")

    def test_repo_names_are_gone_as_words_and_inside_phrases(self):
        self.assertNotIn("quillfish", self.terms("prose_user"))
        self.assertNotIn("combat-engine", self.terms("prose_user"))
        self.assertNotIn("dyslexic-type", self.terms("distinctive_user"))
        self.assertNotIn("quillfish app", self.terms("phrases_user"))
        # "combat engine" is how the tokenizer spells combat-engine.
        self.assertNotIn("combat engine", self.terms("phrases_user"))
        self.assertNotIn("quillfish", self.terms("extensions"))

    def test_filenames_hostnames_and_bundle_ids_are_gone(self):
        self.assertNotIn("com.acme.quillfish", self.terms("prose_user"))
        self.assertNotIn("mejanreteam.zorbax.com", self.terms("prose_user_raw"))

    def test_a_repo_name_is_not_split_into_its_generic_parts(self):
        """dyslexic-type contains "type", the most common word in the corpus.
        Dropping segments would gut every cloud."""
        keep, _ = bd.scrub_stats(
            {"clouds": {"global": {"prose_user": [{"t": "type", "n": 1},
                                                  {"t": "native", "n": 1},
                                                  {"t": "control", "n": 1}]},
                        "by_project": {"dyslexic-type": {},
                                       "graphify-ab-control": {}}}}, [])
        self.assertEqual([d["t"] for d in keep["clouds"]["global"]["prose_user"]],
                         ["type", "native", "control"])

    def test_trends_and_wrapped_are_filtered_too(self):
        self.assertNotIn("quillfish",
                         [d["t"] for d in self.scrubbed["trends"]["rising"]])
        self.assertNotIn("quillfish",
                         [w[0] for w in self.scrubbed["wrapped"]["top_words"]])
        # The deck skips a slide whose stat is missing rather than faking it.
        self.assertNotIn("top_phrase", self.scrubbed["wrapped"])
        self.assertEqual(self.scrubbed["wrapped"]["rising_word"], "loop")

    def test_the_local_account_is_stripped_without_being_named(self):
        """--scrub-name is a convenience; the login has to be found anyway."""
        me = getpass.getuser()
        if len(me) < 3:
            self.skipTest("login name too short to match on")
        self.assertIn(me.lower(), bd.account_names())


class TestScrubRemovesTheMcpServerNames(unittest.TestCase):
    """The tools facet is kept, and it used to keep `mcp__<server>__<tool>`
    whole -- a complete inventory of the third-party servers the machine is
    wired to, which is configuration read off the machine and not vocabulary
    anybody typed. The server half goes; the verb stays, because dropping it
    too would empty the Tools view on any machine that leans on MCP."""

    @classmethod
    def setUpClass(cls):
        cls.scrubbed, cls.report = bd.scrub_stats(synthetic(), [USER])
        cls.tools = cls.scrubbed["clouds"]["global"]["tools"]

    def test_no_server_name_survives_anywhere(self):
        clouds = self.scrubbed["clouds"]
        buckets = [clouds["global"]] + [b for g in ("by_tool", "by_month")
                                        for b in clouds[g].values()]
        for b in buckets:
            for d in b.get("tools", []):
                for server in MCP_SERVERS:
                    self.assertNotIn(server.lower(), d["t"].lower())
                self.assertFalse(d["t"].startswith("mcp__"), d["t"])

    def test_the_verb_survives_so_the_view_still_says_something(self):
        terms = [d["t"] for d in self.tools]
        self.assertIn("Bash", terms)
        self.assertIn("mcp:ui_tap", terms)
        self.assertIn("mcp:execute_sql", terms)

    def test_two_servers_exposing_the_same_verb_fold_into_one_entry(self):
        """A cloud with the same word in it twice draws it twice."""
        rows = [d for d in self.tools if d["t"] == "mcp:screenshot"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n"], 13, "counts add: 7 + 6")

    def test_the_facet_is_still_sorted_by_count(self):
        """The fold can move a term up, and the template renders in order."""
        counts = [d["n"] for d in self.tools]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_the_report_counts_what_it_stripped(self):
        self.assertGreaterEqual(self.report["servers_stripped"],
                                len(MCP_TOOLS))

    def test_a_plain_tool_name_is_untouched(self):
        self.assertEqual(bd.strip_mcp_server("Bash"), "Bash")
        self.assertEqual(bd.strip_mcp_server("mcp"), "mcp")
        self.assertEqual(bd.strip_mcp_server("mcp__onlytwoparts"),
                         "mcp__onlytwoparts")

    def test_the_tool_half_is_still_checked_for_names(self):
        """Stripping the server must not smuggle anything past the filter."""
        out, _ = bd.scrub_stats(
            {"clouds": {"global": {"tools": [
                {"t": "mcp__vendor__deploy_quillfish", "n": 3},
                {"t": "mcp__vendor__ui_tap", "n": 2}]},
                "by_project": {"quillfish": {}}}}, [])
        self.assertEqual([d["t"] for d in out["clouds"]["global"]["tools"]],
                         ["mcp:ui_tap"])


class TestScrubbedPageCarriesNoneOfIt(unittest.TestCase):
    """The end-to-end claim: grep the built file, find nothing."""

    @classmethod
    def setUpClass(cls):
        rc, cls.plain, cls.shared = build(["--scrub", "--scrub-name", USER])
        assert rc == 0, rc

    def test_the_unscrubbed_page_really_does_leak(self):
        """Without this the zero below could mean the fixture is empty."""
        blob = payload(self.plain).lower()
        for s in SECRETS + ERRORS + MCP_SERVERS + ["herd.sh", "rotate.sh"]:
            self.assertIn(s.lower(), blob, s)

    def test_the_scrubbed_page_leaks_nothing(self):
        blob = payload(self.shared).lower()
        for s in SECRETS:
            self.assertNotIn(s.lower(), blob, s)
        for s in ERRORS:
            self.assertNotIn(s.lower(), blob, s)
        for s in MCP_SERVERS:
            self.assertNotIn(s.lower(), blob, s)
        for s in ("herd.sh", "heartbeat.sh", "rotate.sh", "herdr"):
            self.assertNotIn(s, blob, s)
        for s in ("5b78b15b", "vercel"):
            self.assertNotIn(s, blob, s)

    def test_both_pages_are_stamped_with_the_redaction_schema(self):
        """A snapshot outlives the code that built it; snapshot.py --list
        reads this back to say which pages predate today's rules."""
        for page in (self.plain, self.shared):
            self.assertEqual(bd.marker_version(page), bd.REDACTION_SCHEMA)

    def test_the_stamp_is_outside_the_data_block(self):
        """dashboard.html has to stay a verbatim copy of stats.json."""
        self.assertNotIn(bd.MARKER, payload(self.plain))

    def test_the_scrubbed_page_still_parses_and_still_says_something(self):
        data = inlined(self.shared)
        self.assertEqual(data["schema_version"], 2)
        self.assertIn("refactor",
                      [d["t"] for d in data["clouds"]["global"]["prose_user"]])
        self.assertEqual(data["spend"]["total_cost"], 12.5)

    def test_the_page_says_out_loud_that_it_is_a_share_copy(self):
        """A recipient must be able to tell. generated_at and the spend note
        are the two fields the template prints as free text."""
        data = inlined(self.shared)
        self.assertIn("SHARE COPY", data["generated_at"])
        self.assertIn("SHARE COPY", data["spend"]["estimates_note"])

    def test_no_external_hosts_at_all(self):
        hosts = set(re.findall(r'https?://([a-z0-9.\-]+)', self.shared))
        hosts.discard("www.w3.org")
        self.assertEqual(hosts, set(), f"unexpected external hosts: {hosts}")


class TestTheNormalBuildIsUntouched(unittest.TestCase):

    def test_without_scrub_nothing_extra_is_written(self):
        rc, plain, shared = build([])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(plain)
        self.assertIsNone(shared, "--scrub was not asked for")

    def test_scrub_does_not_overwrite_the_private_dashboard(self):
        rc, plain, shared = build(["--scrub", "--scrub-name", USER])
        self.assertEqual(rc, 0)
        _, plain_only, _ = build([])
        self.assertEqual(plain, plain_only,
                         "dashboard.html must not change when --scrub is on")
        self.assertNotEqual(plain, shared)

    def test_the_inlined_payload_is_still_the_file_verbatim(self):
        """The unscrubbed page inlines stats.json byte for byte (bar the "<"
        escape), which is what lets a snapshot be re-analysed later."""
        stats = synthetic()
        rc, plain, _ = build([], stats)
        self.assertEqual(rc, 0)
        raw = json.dumps(stats)
        self.assertIn(raw.replace("<", "\\u003c"), plain)

    def test_missing_keys_are_still_fatal(self):
        rc, plain, _ = build([], {"schema_version": 2})
        self.assertEqual(rc, 1)
        self.assertIsNone(plain)


class TestScrubToleratesThinData(unittest.TestCase):
    """--demo data, a v1 stats.json and a half-written one all reach here."""

    def test_empty_input(self):
        out, _ = bd.scrub_stats({}, [])
        self.assertIn("SHARE COPY", out["generated_at"])

    def test_missing_sections(self):
        out, _ = bd.scrub_stats({"schema_version": 1, "clouds": {}}, [])
        self.assertEqual(out["clouds"], {})

    def test_the_input_is_not_mutated(self):
        stats = synthetic()
        bd.scrub_stats(stats, [USER])
        self.assertIn("quillfish", stats["clouds"]["by_project"])
        self.assertEqual(stats["generated_at"], "2026-08-28T22:20:29Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
