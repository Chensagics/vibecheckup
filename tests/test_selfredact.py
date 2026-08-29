"""Self-redaction: masking the literals only this machine can recognise.

Every other rule in wcstats.clean spots a leak by its shape. These two cannot
be spotted that way at all:

  * The owner's own handle. `clean.RE_PATH` wants two slash segments, so an
    `owner/repo` slug written in a sentence -- `**chensagi/finn#**`,
    `CI_REPO="chensagi/finn"` -- survives cleaning and tokenizes into two
    ordinary-looking words. Ten hits reached the shipped stats.json that way,
    in prose_user for three projects and two months, in prose_assistant and
    the distinctive terms for a fourth, and as the phrase
    `chensagi academy-auto-open`.
  * Worktree BRANCH names. The label was folded onto its parent repo, but the
    branch is also something people write about, and `worktree-native-ota`,
    `worktree-art-direction`, `worktree-dyslexic-type`, `worktree-combat-engine`,
    `worktree-art-screens` and `worktree-ios-ready` all shipped in vocab.json,
    with `graphify` -- a live A/B test -- five more times in the stats clouds.

Both are fixed the only way they can be: by deriving the literals and masking
them. That makes a FALSE POSITIVE the real risk of the feature -- a login that
happens to be an English word would delete that word from every cloud in the
corpus -- so most of what follows is about the guard.

Synthetic throughout. Nothing here reads data/; the end-to-end case builds its
own corpus in a temp dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from adapters.base import OBSERVED, normalize_project, project_from_cwd  # noqa: E402
from wcstats.clean import (COMMON_WORDS, LABEL_PLACEHOLDER,  # noqa: E402
                           MIN_IDENTITY_LEN, Redactor,
                           branch_redaction_terms, build_redaction,
                           clean_prose, derive_identities, format_redaction,
                           git_config_pairs, identities_from_email,
                           identity_verdict, install_redactor,
                           literal_is_applicable, owner_from_remote_url,
                           owners_from_git_config, read_redact_file,
                           redact_file_path, unmatchable_literals,
                           UNMATCHABLE_REASON)
from wcstats.tokenize import keep, tokens  # noqa: E402


def fake_home(case, name="chensagi", gitconfig=None):
    """A HOME directory with a git config in it, torn down after the test."""
    root = tempfile.mkdtemp()
    case.addCleanup(_rmtree, root)
    home = os.path.join(root, "Users", name)
    os.makedirs(home)
    if gitconfig is not None:
        with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as fh:
            fh.write(gitconfig)
    return home


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class RedactorCase(unittest.TestCase):
    """Installs a redactor for the duration of one test and takes it back out.

    clean_prose() consults a module-level redactor, so a test that installed
    one and left it there would silently change what every later test sees.
    """

    def install(self, *literals):
        r = Redactor(literals)
        prev = install_redactor(r)
        self.addCleanup(install_redactor, prev)
        return r


# --- 1. deriving the identities ---------------------------------------------

class TestIdentityDerivation(unittest.TestCase):
    """Who is running this tool, from the sources that are actually reliable."""

    GITCONFIG = """
[user]
\tname = chensagics
\temail = 71827484+chensagi@users.noreply.github.com
[core]
\teditor = vim
"""

    def test_home_directory_basename_is_an_identity(self):
        """The cheapest and most reliable source: it is already in every
        ingest root the adapters walk."""
        got, _ = derive_identities(home=fake_home(self, "chensagi"))
        self.assertIn("chensagi", got)

    def test_git_name_and_email_are_identities(self):
        home = fake_home(self, "someone", gitconfig=self.GITCONFIG)
        got, _ = derive_identities(home=home)
        self.assertIn("chensagics", got)   # [user] name
        self.assertIn("chensagi", got)     # ...from inside the noreply address

    def test_github_noreply_address_yields_the_handle_not_the_id(self):
        """`<id>+<handle>@users.noreply.github.com` is the privacy address, and
        the handle in it is exactly the string that shows up in a slug."""
        got = identities_from_email("71827484+chensagi@users.noreply.github.com")
        self.assertEqual(got, ["chensagi"])
        self.assertNotIn("71827484", got)

    def test_ordinary_address_yields_the_local_part_and_its_pieces(self):
        got = identities_from_email("chen.sagi.cs@gmail.com")
        self.assertEqual(got[0], "chen.sagi.cs")
        self.assertIn("sagi", got)

    def test_remote_owner_is_an_identity(self):
        """A GitHub handle can be spelled differently from the local login, and
        the slug in prose uses the GitHub one."""
        for url, owner in (
                ("https://github.com/Chensagics/vibecheckup.git", "Chensagics"),
                ("git@github.com:Chensagics/vibecheckup.git", "Chensagics"),
                ("ssh://git@gitlab.com/acme-corp/thing.git", "acme-corp"),
                ("https://user@bitbucket.org/team/repo.git", "team"),
        ):
            with self.subTest(url=url):
                self.assertEqual(owner_from_remote_url(url), owner)

    def test_a_local_path_remote_names_no_owner(self):
        self.assertIsNone(owner_from_remote_url("/srv/mirrors/thing.git"))
        self.assertIsNone(owner_from_remote_url(""))

    def test_repo_configs_contribute_their_remote_owners(self):
        cfg = '[remote "origin"]\n\turl = git@github.com:Chensagics/finn.git\n'
        self.assertEqual(owners_from_git_config(cfg), ["Chensagics"])
        got, _ = derive_identities(home=fake_home(self, "nobody"),
                                   repo_configs=[cfg])
        self.assertIn("chensagics", got)

    def test_git_config_parser_handles_subsections_and_comments(self):
        cfg = ('# a comment\n[user]\n\tname = Jane Doe\n'
               '[remote "upstream"]\n\turl = https://github.com/acme/x.git\n')
        pairs = dict(git_config_pairs(cfg))
        self.assertEqual(pairs[("user", "", "name")], "Jane Doe")
        self.assertEqual(pairs[("remote", "upstream", "url")],
                         "https://github.com/acme/x.git")

    def test_a_multiword_display_name_contributes_each_word(self):
        home = fake_home(self, "nobody",
                         gitconfig="[user]\n\tname = Chensagi Berkowitz\n")
        got, _ = derive_identities(home=home)
        self.assertIn("chensagi", got)
        self.assertIn("berkowitz", got)

    def test_derivation_is_lowercased_and_deduplicated(self):
        home = fake_home(self, "Chensagi",
                         gitconfig="[user]\n\tname = CHENSAGI\n")
        got, _ = derive_identities(home=home)
        self.assertEqual(got.count("chensagi"), 1)


# --- 2. the common-word guard -----------------------------------------------

class TestCommonWordGuard(unittest.TestCase):
    """The one way this feature can do real damage.

    A login that is also an English word would, if auto-redacted, delete that
    word from every cloud in the corpus in exchange for no privacy at all. The
    guard is deliberately blunt and deliberately over-inclusive: anything it
    refuses is still redactable by hand with --redact, while anything it wrongly
    accepts is gone silently.
    """

    def test_a_login_called_mark_does_not_nuke_the_word_mark(self):
        home = fake_home(self, "mark", gitconfig="[user]\n\tname = mark\n")
        got, skipped = derive_identities(home=home)
        self.assertNotIn("mark", got)
        self.assertIn("mark", [lit for lit, _why in skipped])
        r = Redactor(got)
        install_redactor(r)
        self.addCleanup(install_redactor, Redactor())
        self.assertIn("mark", tokens(clean_prose(
            "please mark the failing spec and rerun it")))

    def test_short_and_common_logins_are_all_refused(self):
        for login in ("will", "sam", "art", "max", "rob", "bill", "jack",
                      "grace", "hunter", "river", "summer", "docs", "main",
                      "staging", "release", "dashboard", "telemetry"):
            with self.subTest(login=login):
                ok, why = identity_verdict(login)
                self.assertFalse(ok, f"{login} would have been masked")
                self.assertTrue(why)

    def test_a_distinctive_handle_is_accepted(self):
        for login in ("chensagi", "chensagics", "octocat", "torvalds"):
            with self.subTest(login=login):
                self.assertTrue(identity_verdict(login)[0])

    def test_length_is_the_first_gate(self):
        self.assertLess(len("mark"), MIN_IDENTITY_LEN)
        ok, why = identity_verdict("abcd")
        self.assertFalse(ok)
        self.assertIn("shorter", why)

    def test_the_word_list_covers_the_vocabulary_the_clouds_care_about(self):
        """The words a previous blunt fix was measured to have deleted."""
        for word in ("docs", "scripts", "assets", "telemetry", "spec"):
            with self.subTest(word=word):
                self.assertIn(word, COMMON_WORDS)

    def test_the_guard_never_silently_swallows_a_refusal(self):
        home = fake_home(self, "sam", gitconfig="[user]\n\tname = sam\n")
        _got, skipped = derive_identities(home=home)
        report = {"identities": [], "branches": [], "decorated": [],
                  "explicit": [], "skipped": skipped, "branch_mode": "full"}
        text = format_redaction(report)
        self.assertIn("NOT masked", text)
        self.assertIn("--redact", text)


# --- 3. how a literal is matched --------------------------------------------

class TestMaskingPositions(RedactorCase):
    """Bare, in a slug, and inside a compound -- the three shapes the leak
    actually took in the shipped data."""

    def test_bare_token(self):
        self.install("chensagi")
        self.assertNotIn("chensagi", tokens(clean_prose(
            "chensagi should own the rollout")))

    def test_owner_repo_slug_loses_the_owner_and_keeps_the_repo(self):
        """RE_PATH needs two slash segments, so this is what survived cleaning.
        `finn` is a project name the dashboard already publishes as a label;
        the handle is the half that has to go."""
        self.install("chensagi")
        got = tokens(clean_prose("see chensagi/finn for the branch"))
        self.assertNotIn("chensagi", got)
        self.assertIn("finn", got)
        # ...and in the markdown spelling the leak actually took.
        self.assertNotIn("chensagi",
                         clean_prose("see **chensagi/finn#** for the branch"))

    def test_slug_inside_a_shell_assignment(self):
        self.install("chensagi")
        self.assertNotIn("chensagi", clean_prose('set CI_REPO="chensagi/finn"'))

    def test_compound_token_where_the_literal_is_a_whole_word(self):
        self.install("combat-engine")
        for text in ("rebase worktree-combat-engine onto main",
                     "read combat-engine-handoff.md first",
                     "combat-engine's tests are red"):
            with self.subTest(text=text):
                self.assertNotIn("combat-engine", clean_prose(text))

    def test_a_run_on_suffix_is_masked_too(self):
        """`chensagics` and `graphifyignore` are the same leak with a letter
        stuck on, and a plain \\b word boundary misses both."""
        self.install("chensagi", "graphify")
        got = clean_prose("chensagics added graphifyignore and graphifyy")
        self.assertNotIn("chensagi", got)
        self.assertNotIn("graphify", got)

    def test_the_literal_is_never_matched_mid_word(self):
        """The whole reason the match is anchored to a token edge: an identity
        that happens to be a substring of a real word must not eat it."""
        self.install("river")
        got = tokens(clean_prose("the driver and the screwdriver both work"))
        self.assertIn("driver", got)
        self.assertIn("screwdriver", got)

    def test_a_literal_matches_whatever_separator_it_is_written_with(self):
        """`chen.sagi.cs` and `chen_sagi_cs` are one identity spelled two ways,
        and the corpus had both."""
        self.install("chen.sagi.cs", "combat-engine")
        got = clean_prose("chen_sagi_cs owns combat_engine and chen-sagi-cs")
        self.assertNotIn("sagi", got)
        self.assertNotIn("combat_engine", got)

    def test_matching_is_case_insensitive(self):
        self.install("chensagi")
        for text in ("CHENSAGI owns it", "Chensagi owns it", "ChEnSaGi owns it"):
            with self.subTest(text=text):
                self.assertNotIn("chensagi", clean_prose(text).lower())

    def test_surrounding_prose_survives(self):
        self.install("chensagi", "combat-engine")
        got = tokens(clean_prose(
            "chensagi please rewrite the combat-engine deploy pipeline docs"))
        for word in ("rewrite", "deploy", "pipeline", "docs"):
            self.assertIn(word, got)

    def test_a_masked_token_leaves_a_word_gap_not_a_join(self):
        """Replacing with nothing would fuse the neighbours into one term.

        The shipped phrase was `chensagi academy-auto-open`; the handle goes
        and its neighbour stays a separate word rather than becoming
        `chensagiacademy-auto-open`.
        """
        self.install("chensagi")
        got = tokens(clean_prose("chensagi academy-auto-open"))
        self.assertEqual(got, ["academy-auto-open"])
        self.assertNotIn("chensagiacademy", clean_prose("chensagi academy"))

    def test_the_tokenizer_is_a_second_boundary(self):
        """vocab.json is built out of the counters keep() fills, so a token
        that reaches the tokenizer without going through clean_prose stops
        here rather than being published."""
        self.install("chensagi")
        self.assertFalse(keep("chensagi"))
        self.assertTrue(keep("chensaki"))

    def test_an_empty_redactor_changes_nothing(self):
        install_redactor(Redactor())
        self.addCleanup(install_redactor, Redactor())
        text = "please rebuild the deploy pipeline for chensagi today"
        self.assertEqual(clean_prose(text), text)

    def test_a_facet_key_collapses_to_a_placeholder_not_to_nothing(self):
        r = Redactor(["combat-engine"])
        self.assertEqual(r.scrub_label("combat-engine"), LABEL_PLACEHOLDER)
        self.assertEqual(r.scrub_label("keep"), "keep")

    def test_a_signature_takes_a_visible_marker(self):
        """Error signatures already read PATH / HOST / EMAIL; a silent gap in
        the middle of a failure message reads as a bug."""
        r = Redactor(["combat-engine"])
        self.assertEqual(r.scrub("HERD_LANE=combat-engine failed", "REDACTED"),
                         "HERD_LANE=REDACTED failed")


# --- 3b. a two-word display name --------------------------------------------

class TestTwoWordDisplayName(RedactorCase):
    """The tool said the name was masked, printed it as masked, and published
    it on the share card.

    Three things conspired. `derive_identities` emitted the whole display name
    as a literal; `_pattern` split it on `[._-]` only, so "jane doe" compiled
    to `jane\\ doe`; and `scrub` substitutes one RE_TOKENISH run at a time, and
    a run can never contain a space. So the literal could not match ANYTHING:

        Redactor(['jane doe']).hits('jane')                -> False
        Redactor(['jane doe']).scrub('signed by Jane Doe') -> unchanged

    The two halves were then refused separately by MIN_IDENTITY_LEN, and the
    terminal printed

        identities     jane doe, janedoe
        NOT masked     jane (shorter than 5 characters), doe (shorter ...)

    while stats.json carried wrapped.top_words = [['jane', 2], ['doe', 2]] --
    the forename and surname as the #1 and #2 words on the Agent Wrapped share
    card, the one artifact designed for public posting.
    """

    GITCONFIG = "[user]\n\tname = Jane Doe\n\temail = jane.doe@example.com\n"

    def derive(self, name="Jane Doe", email="jane.doe@example.com",
               home_name="janedoe"):
        cfg = "[user]\n\tname = %s\n\temail = %s\n" % (name, email)
        return derive_identities(home=fake_home(self, home_name, gitconfig=cfg))

    def test_the_multiword_literal_actually_matches(self):
        r = Redactor(["jane doe"])
        self.assertTrue(r.hits("jane doe"))
        self.assertEqual(r.scrub("signed by Jane Doe"), "signed by  ")

    def test_a_multiword_literal_matches_every_separator(self):
        """A display name written with a space and the same identity written
        as an address local part are one person."""
        r = Redactor(["jane doe"])
        for text in ("jane doe", "jane.doe", "jane-doe", "jane_doe",
                     "Jane  Doe"):
            with self.subTest(text=text):
                self.assertNotIn("jane", r.scrub("by " + text + " here").lower())

    def test_the_whole_token_still_goes(self):
        """Same promise as the single-token pass: the match is grown out to
        the token edges before it is replaced."""
        self.assertEqual(Redactor(["jane doe"]).scrub("see foo-jane doe.md now"),
                         "see   now")

    def test_a_branch_name_does_not_gain_the_whitespace_spelling(self):
        """The widening is one-directional. `combat-engine` must not start
        eating the two ordinary words "combat engine" out of prose."""
        r = Redactor(["combat-engine"])
        got = r.scrub("the combat engine is fine but worktree-combat-engine is not")
        self.assertIn("combat engine", got)
        self.assertNotIn("worktree-combat-engine", got)

    def test_both_halves_of_the_name_are_masked(self):
        """`user.name` is the person, typed by the person, so the length gate
        does not apply to it -- only the common-word guard does."""
        got, skipped = self.derive()
        for lit in ("jane", "doe", "jane doe", "jane.doe", "janedoe"):
            self.assertIn(lit, got, lit)
        self.assertEqual(skipped, [])

    def test_the_name_really_is_gone_from_prose(self):
        got, _ = self.derive()
        self.install(*got)
        text = clean_prose("Hey Jane, tell Jane Doe that jane.doe owns it. "
                           "Jane's review is Doe's problem too.")
        for leak in ("jane", "doe"):
            self.assertNotIn(leak, text.lower(), text)
        self.assertIn("review", tokens(text))

    def test_the_report_matches_what_is_masked(self):
        """The whole point: every literal the terminal calls masked has to be
        one the matcher can actually apply."""
        r, report = build_redaction(
            home=fake_home(self, "janedoe", gitconfig=self.GITCONFIG),
            read_file=False)
        text = format_redaction(report)
        self.assertIn("identities", text)
        for lit in report["identities"]:
            with self.subTest(lit=lit):
                self.assertTrue(literal_is_applicable(lit), lit)
                self.assertTrue(r.hits(lit) or r.scrub("x " + lit + " x")
                                != "x " + lit + " x", lit)
        self.assertNotIn("shorter than", text)

    def test_the_real_corroboration_from_this_machine(self):
        """`user.name = Chen Sagi` is one token per word and both were refused,
        so vocab.json shipped chen x21, chen's x2, sagi x3 and sagi's x4 out of
        "Hey Chen", "tell Chen the handoff line" and "Chen Sagi's projects"."""
        got, _ = self.derive(name="Chen Sagi", email="chen.sagi.cs@gmail.com",
                             home_name="chensagi")
        self.install(*got)
        text = clean_prose("Hey Chen, tell Chen the handoff line about "
                           "Chen Sagi's projects and sagi's review")
        for leak in ("chen", "sagi"):
            self.assertNotIn(leak, text.lower(), text)
        self.assertIn("handoff", tokens(text))

    def test_a_short_name_that_is_a_word_is_still_refused(self):
        """The common-word guard is what carries the risk now, so it has to
        hold on its own: a person called Art keeps the word `art`."""
        got, skipped = self.derive(name="Art Vandelay", email="art@vandelay.com",
                                   home_name="artv")
        self.assertNotIn("art", got)
        self.assertIn("art", [lit for lit, _why in skipped])
        self.assertIn("art vandelay", got)      # the full name still goes
        self.install(*got)
        kept = tokens(clean_prose(
            "the art of good design is art direction and art history"))
        self.assertEqual(kept.count("art"), 3)
        self.assertNotIn("vandelay", clean_prose("signed Art Vandelay").lower())

    def test_a_one_letter_middle_initial_is_refused(self):
        got, skipped = self.derive(name="Jane Q Doe")
        self.assertNotIn("q", got)
        self.assertIn("q", [lit for lit, _why in skipped])

    def test_a_short_literal_is_anchored_at_both_edges(self):
        """The run-on rule is right for a distinctive handle and catastrophic
        for a four-letter forename: unanchored, `li` eats list/line/link/
        library, `cs` eats csv and `ana` eats analysis."""
        for lit, victim in (("li", "the list of lines and links in the library"),
                            ("cs", "read the csv file"),
                            ("ana", "run the analysis and analytics"),
                            ("chen", "chencorp shipped it")):
            with self.subTest(lit=lit):
                self.assertEqual(Redactor([lit]).scrub(victim), victim)

    def test_a_short_literal_still_masks_every_form_a_name_takes(self):
        r = Redactor(["chen"])
        for text in ("Hey Chen", "chen's review", "chen-sagi owns it",
                     "ping chen.", "(chen)", "CHEN did"):
            with self.subTest(text=text):
                self.assertNotIn("chen", r.scrub(text).lower())

    def test_a_long_literal_keeps_the_run_on_rule(self):
        self.assertEqual(Redactor(["chensagi"]).scrub("chensagics added it"),
                         "  added it")

    def test_remote_owners_keep_the_length_gate(self):
        """A remote owner can be an ORGANISATION -- `npm`, `expo`, `mae` --
        rather than a human, so the weaker source keeps the second guard."""
        cfg = ('[remote "origin"]\n'
               '\turl = https://github.com/abcd/thing.git\n')
        got, skipped = derive_identities(home=fake_home(self, "nobody"),
                                         repo_configs=[cfg])
        self.assertNotIn("abcd", got)
        self.assertIn("abcd", [lit for lit, _why in skipped])


# --- 3c. the report may not lie ----------------------------------------------

class TestTheReportCannotClaimWhatItCannotMask(unittest.TestCase):
    """A report that lies is worse than no report.

    `format_redaction` listed `jane doe` under `identities` while the matcher
    could not apply it anywhere. The self-check runs both code paths -- hits(),
    which tokenize.keep() calls, and scrub(), which clean_prose() calls --
    because that literal passed the first and silently failed the second.
    """

    UNMATCHABLE = "///"

    def report(self, **kw):
        base = {"identities": [], "branches": [], "decorated": [],
                "explicit": [], "skipped": [], "branch_mode": "full"}
        base.update(kw)
        return base

    def test_an_unmatchable_literal_is_detected(self):
        self.assertFalse(literal_is_applicable(self.UNMATCHABLE))
        self.assertFalse(literal_is_applicable(""))
        self.assertFalse(literal_is_applicable("   "))

    def test_an_ordinary_literal_passes(self):
        for lit in ("chensagi", "combat-engine", "jane doe", "chen",
                    "worktree-combat-engine", "chen.sagi.cs"):
            with self.subTest(lit=lit):
                self.assertTrue(literal_is_applicable(lit), lit)

    def test_the_formatter_refuses_to_claim_it(self):
        rep = self.report(identities=["chensagi", self.UNMATCHABLE])
        text = format_redaction(rep)
        self.assertIn("chensagi", text)
        identity_line = [ln for ln in text.splitlines() if "identities" in ln][0]
        self.assertNotIn(self.UNMATCHABLE, identity_line)
        self.assertIn("NOT masked", text)
        self.assertIn(self.UNMATCHABLE, text)

    def test_every_bucket_is_checked_not_just_identities(self):
        for bucket in ("identities", "branches", "decorated", "explicit"):
            with self.subTest(bucket=bucket):
                rep = self.report(**{bucket: [self.UNMATCHABLE]})
                self.assertEqual(unmatchable_literals(rep),
                                 [(self.UNMATCHABLE, bucket)])

    def test_the_formatter_does_not_mutate_the_caller_s_report(self):
        rep = self.report(identities=["chensagi", self.UNMATCHABLE])
        format_redaction(rep)
        self.assertIn(self.UNMATCHABLE, rep["identities"])

    def test_build_redaction_never_produces_one(self):
        r, report = build_redaction(
            home=fake_home(self, "janedoe",
                           gitconfig="[user]\n\tname = Jane Doe\n"),
            branch_pairs=[("combat-engine", "finn"), ("main", "finn")],
            extra=["acme-corp", self.UNMATCHABLE], read_file=False)
        self.assertEqual(unmatchable_literals(report), [])
        self.assertIn((self.UNMATCHABLE, UNMATCHABLE_REASON), report["skipped"])
        self.assertTrue(r.hits("jane doe"))

    def test_the_short_form_limitation_is_stated(self):
        """A short literal masks whole words only, so a repo or org built on
        the owner's name survives. The user is told, because the F1 lesson is
        that a silent gap is worse than a stated one."""
        rep = self.report(identities=["chen", "chensagi"])
        text = format_redaction(rep)
        self.assertIn("whole word only", text)
        self.assertIn("chen", text)
        self.assertIn("--redact", text)
        self.assertNotIn("whole word only", format_redaction(
            self.report(identities=["chensagi"])))


# --- 4. branch names ---------------------------------------------------------

class TestBranchRedaction(unittest.TestCase):
    """Ingest identifies the branch on the way to folding a worktree onto its
    repo. That identification is what makes the prose leak fixable."""

    def setUp(self):
        OBSERVED.clear()
        self.addCleanup(OBSERVED.clear)

    def test_a_worktree_path_records_its_branch(self):
        self.assertEqual(
            project_from_cwd("/Users/alice/Projects/keep/.claude/worktrees/combat-engine"),
            "keep")
        self.assertEqual(OBSERVED.branches.get("combat-engine"), "keep")

    def test_a_subdirectory_of_a_worktree_records_the_branch_not_the_subdir(self):
        project_from_cwd(
            "/Users/alice/Projects/finn/.claude/worktrees/native-ota/packages/api")
        self.assertIn("native-ota", OBSERVED.branches)
        self.assertNotIn("packages", OBSERVED.branches)

    def test_a_codex_pool_records_no_branch(self):
        """~/.codex/worktrees/<id>/<repo>/src: the segment below the repo is an
        ordinary subdirectory, and calling `src` a branch would put a real word
        on the redaction list."""
        self.assertEqual(project_from_cwd("/Users/alice/.codex/worktrees/1860/finn/src"),
                         "finn")
        self.assertEqual(OBSERVED.branches, {})

    def test_an_encoded_label_records_its_branch(self):
        """analyze.py can recover the branch names from a corpus that was
        ingested before any of this existed: the label is still in the stream."""
        self.assertEqual(normalize_project("keep--claude-worktrees-combat-engine"),
                         "keep")
        self.assertEqual(OBSERVED.branches.get("combat-engine"), "keep")

    def test_a_repo_named_after_the_pool_records_nothing(self):
        self.assertEqual(normalize_project("my-workspaces-thing"),
                         "my-workspaces-thing")
        self.assertEqual(OBSERVED.branches, {})

    def test_a_compound_branch_name_is_masked_bare_and_decorated(self):
        bare, decorated, _ = branch_redaction_terms([("combat-engine", "keep")])
        self.assertIn("combat-engine", bare)
        self.assertIn("worktree-combat-engine", decorated)

    def test_a_branch_that_is_a_common_word_is_only_masked_decorated(self):
        """A branch called `docs` or `main` must not delete that word, but
        `worktree-docs` is not a word in any language and always goes."""
        bare, decorated, skipped = branch_redaction_terms(
            [("docs", "finn"), ("main", "finn"), ("staging", "finn")])
        self.assertEqual(bare, [])
        for name in ("docs", "main", "staging"):
            self.assertIn(f"worktree-{name}", decorated)
        self.assertEqual({lit for lit, _ in skipped}, {"docs", "main", "staging"})

    def test_a_shared_stem_across_sibling_branches_is_the_experiment_name(self):
        """`graphify-ab-control` / `-ab-graph` / `-ab2-control` / `-ab2-graph`
        are four arms of one A/B test; the thing that must not be published is
        the test. Two siblings is the bar -- a leading segment of a LONE branch
        is not evidence of anything, and that rule would offer up `native`,
        `combat` and `ios`."""
        pairs = [(f"graphify-ab{n}-{arm}", "finn")
                 for n in ("", "2") for arm in ("control", "graph")]
        bare, _dec, _skip = branch_redaction_terms(pairs)
        self.assertIn("graphify", bare)

    def test_a_lone_branch_contributes_no_stem(self):
        bare, _dec, _skip = branch_redaction_terms([("native-ota", "finn")])
        self.assertIn("native-ota", bare)
        self.assertNotIn("native", bare)

    def test_a_stem_shared_across_two_different_repos_is_not_a_stem(self):
        bare, _dec, _skip = branch_redaction_terms(
            [("payment-retry", "finn"), ("payment-audit", "keep")])
        self.assertNotIn("payment", bare)

    def test_a_common_word_stem_is_refused(self):
        """`art-direction` and `art-screens` share `art`, which is a word."""
        bare, _dec, skipped = branch_redaction_terms(
            [("art-direction", "keep"), ("art-screens", "keep")])
        self.assertIn("art-direction", bare)
        self.assertIn("art-screens", bare)
        self.assertNotIn("art", bare)
        self.assertIn("art", [lit for lit, _ in skipped])

    def test_the_pool_directory_itself_is_never_a_branch(self):
        bare, decorated, _ = branch_redaction_terms([("worktrees", ""),
                                                     ("workspaces", "")])
        self.assertEqual(bare, [])
        self.assertEqual(decorated, [])

    def test_branch_mode_decorated_keeps_the_bare_name(self):
        r, report = build_redaction(home=fake_home(self, "nobody"),
                                    branch_pairs=[("combat-engine", "keep")],
                                    branch_mode="decorated", read_file=False)
        self.assertEqual(report["branches"], [])
        self.assertTrue(r.hits("worktree-combat-engine"))
        self.assertFalse(r.hits("combat-engine"))

    def test_the_ingest_sidecar_is_merged_into_the_bucket_it_came_from(self):
        """ingest.py knows the git remotes because it is the only stage that
        sees the filesystem; those are identities, not things the user typed."""
        r, report = build_redaction(
            home=fake_home(self, "nobody"), read_file=False,
            sidecar={"identities": ["Octocat"], "branches": ["combat-engine"],
                     "decorated": ["worktree-docs"], "explicit": ["acme"]})
        self.assertIn("octocat", report["identities"])
        self.assertIn("combat-engine", report["branches"])
        self.assertIn("worktree-docs", report["decorated"])
        self.assertEqual(report["explicit"], ["acme"])
        self.assertTrue(r.hits("octocat"))

    def test_branch_mode_still_governs_a_sidecar(self):
        _r, report = build_redaction(
            home=fake_home(self, "nobody"), read_file=False,
            branch_mode="off",
            sidecar={"branches": ["combat-engine"],
                     "decorated": ["worktree-combat-engine"]})
        self.assertEqual(report["branches"], [])
        self.assertEqual(report["decorated"], [])

    def test_branch_mode_off_masks_no_branch_at_all(self):
        r, report = build_redaction(home=fake_home(self, "nobody"),
                                    branch_pairs=[("combat-engine", "keep")],
                                    branch_mode="off", read_file=False)
        self.assertEqual(report["branches"], [])
        self.assertEqual(report["decorated"], [])
        self.assertFalse(r.hits("worktree-combat-engine"))


# --- 5. the escape hatch -----------------------------------------------------

class TestExplicitRedact(unittest.TestCase):
    """--redact is what makes the guard safe to be blunt: everything it refuses
    is still one flag away."""

    def test_an_explicit_literal_bypasses_the_common_word_guard(self):
        r, report = build_redaction(home=fake_home(self, "nobody"),
                                    extra=["mark"], read_file=False)
        self.assertEqual(report["explicit"], ["mark"])
        self.assertTrue(r.hits("mark"))

    def test_explicit_literals_are_masked_in_prose(self):
        r, _ = build_redaction(home=fake_home(self, "nobody"),
                               extra=["acme-corp"], read_file=False)
        install_redactor(r)
        self.addCleanup(install_redactor, Redactor())
        got = clean_prose("ship the acme-corp integration on friday")
        self.assertNotIn("acme-corp", got)
        self.assertIn("integration", got)

    def test_the_config_file_is_read_and_comments_are_ignored(self):
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        path = os.path.join(root, "redact")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# my private strings\nacme-corp\n\n  bluesky  # inline\n")
        self.assertEqual(read_redact_file(path), ["acme-corp", "bluesky"])

    def test_the_config_file_location_is_overridable(self):
        self.assertEqual(redact_file_path({"VIBECHECKUP_REDACT_FILE": "/x/y"}),
                         "/x/y")
        self.assertTrue(redact_file_path({}).endswith("vibecheckup/redact"))

    def test_a_missing_config_file_is_not_an_error(self):
        self.assertEqual(read_redact_file("/nope/does/not/exist"), [])

    def test_both_cli_help_texts_document_the_flag(self):
        for script in ("ingest.py", "analyze.py"):
            with self.subTest(script=script):
                proc = subprocess.run(
                    [sys.executable, os.path.join(ROOT, script), "--help"],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("--redact", proc.stdout)
                self.assertIn("vibecheckup/redact", proc.stdout)


# --- 6. end to end at the publication boundary -------------------------------

IDENTITY = "chensagi"
BRANCH = "combat-engine"
STEMS = ["graphify-ab-control", "graphify-ab-graph", "graphify-ab2-control"]
# Ordinary words sitting next to every literal, which must all survive.
KEEPWORDS = ["pipeline", "rewrite", "telemetry", "scripts", "assets", "docs"]

PROSE = [
    f"{IDENTITY} please rewrite the deploy pipeline docs",
    f"see **{IDENTITY}/finn#** and the {BRANCH} worktree for telemetry",
    f"rebase worktree-{BRANCH} and check the scripts and assets",
    "the graphify rollout needs a pipeline rewrite",
    f'set CI_REPO="{IDENTITY}/finn" before the docs build',
]


class TestEndToEnd(unittest.TestCase):
    """Feed analyze.py a corpus that leaks and read the two files it writes.

    stats.json and vocab.json are what a user hands to somebody else, so this
    is the assertion that actually matters: neither may contain the literal,
    and the prose around it has to still be there.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = os.path.join(cls.tmp, "events.ndjson")
        labels = ([f"keep--claude-worktrees-{BRANCH}"]
                  + [f"finn--claude-worktrees-{s}" for s in STEMS]
                  + ["finn", "keep"])
        with open(src, "w", encoding="utf-8") as fh:
            for i, label in enumerate(labels):
                for j, text in enumerate(PROSE):
                    for role, kind in (("user", "prompt"), ("assistant", "reply")):
                        fh.write(json.dumps({
                            "ts": f"2026-08-{i % 28 + 1:02d}T{j:02d}:00:00+00:00",
                            "tool": "claude_code", "session_id": f"s{i}-{j}",
                            "project": label, "role": role, "kind": kind,
                            "text": text, "ts_exact": True,
                            "confidence": "exact",
                        }) + "\n")
        cls.out = os.path.join(cls.tmp, "stats.json")
        cls.vocab = os.path.join(cls.tmp, "vocab.json")
        env = dict(os.environ)
        # A real user's redact file must not change the outcome of a test.
        env["VIBECHECKUP_REDACT_FILE"] = os.path.join(cls.tmp, "no-such-file")
        env["HOME"] = os.path.join(cls.tmp, "Users", IDENTITY)
        os.makedirs(env["HOME"])
        cls.proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "analyze.py"), "--in", src,
             "--out", cls.out, "--vocab", cls.vocab],
            capture_output=True, text=True, timeout=180, env=env)
        cls.raw_stats = _read_or_empty(cls.out)
        cls.raw_vocab = _read_or_empty(cls.vocab)

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    def test_the_run_succeeded(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_no_identity_literal_reaches_stats_json(self):
        self.assertNotIn(IDENTITY, self.raw_stats)

    def test_no_identity_literal_reaches_vocab_json(self):
        self.assertNotIn(IDENTITY, self.raw_vocab)

    def test_no_branch_name_reaches_either_file(self):
        for lit in [BRANCH, "graphify"] + STEMS:
            with self.subTest(lit=lit):
                self.assertNotIn(lit, self.raw_stats)
                self.assertNotIn(lit, self.raw_vocab)

    def test_the_redaction_list_is_never_written_into_the_shared_files(self):
        """A list of exactly the strings somebody wanted hidden, shipped inside
        the file they are hidden from, is the leak spelled out."""
        for key in ("redact", "identities", "literals"):
            self.assertNotIn(f'"{key}"', self.raw_stats)

    def test_the_surrounding_prose_survives(self):
        vocab = dict(json.loads(self.raw_vocab)["prose_user"])
        for word in KEEPWORDS:
            with self.subTest(word=word):
                self.assertGreater(vocab.get(word, 0), 0)

    def test_the_projects_are_the_two_repos(self):
        stats = json.loads(self.raw_stats)
        self.assertEqual(sorted(stats["clouds"]["by_project"]), ["finn", "keep"])

    def test_the_terminal_report_names_what_was_masked(self):
        self.assertIn("redaction", self.proc.stdout)
        self.assertIn(IDENTITY, self.proc.stdout)


def _read_or_empty(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


if __name__ == "__main__":
    unittest.main()
