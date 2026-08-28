"""Project-label tests: a worktree branch name must never become a project.

Claude Code parks worktrees in <repo>/.claude/worktrees/<branch>, and the
branch is where unshipped features and live A/B arms get their names. Labelling
a session by its leaf directory published those names verbatim and split one
repo into a dozen fake projects, so both halves are pinned here: the label is
the repo, and the branch is gone.

Mostly filesystem-independent -- the label has to come out the same on the
machine that wrote the logs and on one that has never seen those directories,
so the cases go through project_from_cwd() with literal paths. The dash decoder
is the exception: it resolves against the disk by design, so those cases build
a throwaway tree under a temp directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from adapters import gemini_cli  # noqa: E402
from adapters.base import (HOME_LABEL, TMP_LABEL, UNKNOWN,  # noqa: E402
                           decode_dash_path, normalize_project,
                           project_from_cwd, project_from_encoded_dir)

HOME = os.path.expanduser("~").rstrip("/")

# Verbatim from the owner's shipped stats.json. Every one of these was a
# top-level project with its own rendered word cloud; every one of them is a
# branch name. They are the regression, so they are quoted rather than
# generated.
LEAKED = [
    "finn--claude-worktrees-native-ota",
    "finn--claude-worktrees-graphify-ab-control",
    "finn--claude-worktrees-graphify-ab2-control",
    "keep--claude-worktrees-combat-engine",
    "keep--claude-worktrees-dyslexic-type",
    "keep--claude-worktrees-art-direction",
    "finn--claude-worktrees-whimsical-sparking-wilkes",
]

# Every worktree/workspace convention found in the real corpus, as
# (cwd, expected label). The leaf of each is a branch or workspace name.
WORKTREE_PATHS = [
    # Claude Code, pooled inside the checkout.
    ("/Users/alice/Projects/finn/.claude/worktrees/native-ota", "finn"),
    ("/Users/alice/Projects/keep/.claude/worktrees/combat-engine", "keep"),
    ("/Users/alice/Projects/finn/.claude/worktrees/graphify-ab2-control", "finn"),
    # Plain git.
    ("/Users/alice/Projects/finn/.worktrees/agent-alpha-test-skill", "finn"),
    ("/Users/alice/Projects/finn/.git/worktrees/native-ota", "finn"),
    # Codex, pooled in its own home and keyed by an opaque id.
    ("/Users/alice/.codex/worktrees/1860/finn", "finn"),
    ("/Users/alice/.codex/worktrees/935a/mae", "mae"),
    # Conductor, pooled in the user's home and keyed by repo.
    ("/Users/alice/conductor/workspaces/finn/tripoli", "finn"),
    ("/Users/alice/conductor/workspaces/SomoneEr/chengdu", "SomoneEr"),
    # A session started in a subdirectory of a worktree still belongs to the repo.
    ("/Users/alice/Projects/finn/.claude/worktrees/native-ota/packages/api", "finn"),
    ("/Users/alice/.codex/worktrees/a368/finn/src", "finn"),
    ("/Users/alice/conductor/workspaces/finn/stockholm/app", "finn"),
    # Repos living straight in the home directory still resolve upward.
    ("/Users/alice/mae/.claude/worktrees/secret-branch", "mae"),
]

BRANCH_WORDS = ("native-ota", "combat-engine", "graphify", "dyslexic",
                "art-direction", "agent-alpha", "tripoli", "chengdu",
                "whimsical", "secret")


def dash_encode(path):
    """A path the way Claude Code names its transcript directory: both "/" and
    "." collapse to "-"."""
    return path.replace("/", "-").replace(".", "-")


class TestWorktreeCollapse(unittest.TestCase):
    """Every worktree convention in the corpus folds onto its parent repo."""

    def test_every_convention_collapses(self):
        for cwd, repo in WORKTREE_PATHS:
            with self.subTest(cwd=cwd):
                self.assertEqual(project_from_cwd(cwd), repo)

    def test_branch_name_never_survives(self):
        for cwd, _ in WORKTREE_PATHS:
            label = project_from_cwd(cwd)
            for word in BRANCH_WORDS:
                with self.subTest(cwd=cwd, word=word):
                    self.assertNotIn(word, label)

    def test_sibling_branches_share_one_project(self):
        """The whole point: seven worktrees of finn are one project, not seven."""
        labels = {project_from_cwd(f"/Users/alice/Projects/finn/.claude/worktrees/{b}")
                  for b in ("native-ota", "graphify-ab-control", "graphify-ab-graph",
                            "graphify-ab2-control", "graphify-ab2-graph",
                            "h2h-graph", "h2h-nograph")}
        self.assertEqual(labels, {"finn"})

    def test_pool_directory_itself(self):
        # A pool inside a checkout still belongs to that checkout; a pool in a
        # tool's own home names nothing at all.
        self.assertEqual(
            project_from_cwd("/Users/alice/Projects/finn/.claude/worktrees"), "finn")
        self.assertEqual(project_from_cwd("/Users/alice/.codex/worktrees"), UNKNOWN)

    def test_label_does_not_depend_on_who_runs_the_analysis(self):
        """A corpus written by one account and read by another normalizes the
        same way -- the old check keyed off the local $HOME."""
        for user in ("alice", "bob", os.path.basename(HOME)):
            with self.subTest(user=user):
                self.assertEqual(
                    project_from_cwd(f"/Users/{user}/.codex/worktrees/1860/finn"),
                    "finn")
                self.assertEqual(
                    project_from_cwd(f"/home/{user}/.codex/worktrees/1860/finn"),
                    "finn")


class TestAdapterEntryPoints(unittest.TestCase):
    """Each adapter reaches the label a different way; all five must collapse.

    codex and antigravity hand over a raw cwd, grok a URL-encoded directory
    name, claude_code a dash-encoded one, gemini_cli a cwd recovered from a
    sidecar file.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_codex_and_antigravity_raw_cwd(self):
        # Both call project_from_cwd directly with the path from the log.
        self.assertEqual(
            project_from_cwd("/Users/alice/Projects/keep/.claude/worktrees/combat-engine"),
            "keep")
        self.assertEqual(
            project_from_cwd("/Users/alice/Projects/finn/.worktrees/agent-alpha-test-skill"),
            "finn")

    def test_grok_url_encoded_dir(self):
        enc = ("%2FUsers%2Falice%2FProjects%2Ffinn%2F.claude%2Fworktrees"
               "%2Fcompiled-humming-blanket")
        self.assertEqual(project_from_encoded_dir(enc), "finn")

    def test_claude_code_dash_encoded_dir(self):
        repo = os.path.join(self.tmp, "myrepo")
        os.makedirs(os.path.join(repo, ".claude", "worktrees", "secret-branch"))
        enc = dash_encode(os.path.join(repo, ".claude", "worktrees", "secret-branch"))
        self.assertEqual(project_from_encoded_dir(enc), "myrepo")

    def test_claude_code_dash_encoded_dir_after_the_worktree_is_pruned(self):
        """Worktrees get deleted; the transcript directory outlives them."""
        repo = os.path.join(self.tmp, "myrepo")
        os.makedirs(os.path.join(repo, ".claude", "worktrees"))
        enc = dash_encode(os.path.join(repo, ".claude", "worktrees", "secret-branch"))
        self.assertEqual(project_from_encoded_dir(enc), "myrepo")

    def test_claude_code_dash_encoded_dir_that_resolves_nowhere(self):
        """A corpus read on a machine that never had those directories."""
        enc = "-Users-nobody-Projects-myrepo--claude-worktrees-secret-branch"
        self.assertEqual(project_from_encoded_dir(enc), "myrepo")

    def test_gemini_cli_sidecar_cwd(self):
        base = os.path.join(self.tmp, "hash")
        os.makedirs(os.path.join(base, "chats"))
        with open(os.path.join(base, "cwd"), "w", encoding="utf-8") as fh:
            fh.write("/Users/alice/Projects/finn/.claude/worktrees/native-ota\n")
        label = gemini_cli._project_label(
            os.path.join(base, "chats", "session-1.json"), "hash")
        self.assertEqual(label, "finn")


class TestNeutralLabels(unittest.TestCase):
    """A session with no repo behind it gets a neutral label, not a directory
    name, and never invents a project."""

    def test_temp_directories(self):
        for cwd in ("/tmp/scratch",
                    "/private/tmp/fresh-mac",
                    "/var/tmp/build",
                    "/var/folders/bk/xl8f8lws41bc/T/finn-vendor-meter",
                    "/private/var/folders/bk/xl8f8lws41bc/T/finn-vendor-meter"):
            with self.subTest(cwd=cwd):
                self.assertEqual(project_from_cwd(cwd), TMP_LABEL)

    def test_the_two_spellings_of_tmp_are_one_project(self):
        self.assertEqual(project_from_cwd("/tmp/x"),
                         project_from_cwd("/private/tmp/x"))

    def test_home_directory(self):
        self.assertEqual(project_from_cwd(HOME), HOME_LABEL)
        self.assertEqual(project_from_cwd(HOME + "/"), HOME_LABEL)

    def test_home_directory_name_is_not_published(self):
        self.assertNotIn(os.path.basename(HOME), project_from_cwd(HOME))

    def test_root_and_missing(self):
        for cwd in ("/", "", None, "   "):
            with self.subTest(cwd=cwd):
                self.assertEqual(project_from_cwd(cwd), UNKNOWN)


class TestOrdinaryPathsUnchanged(unittest.TestCase):
    """Regression: the fix must not touch a plain checkout."""

    def test_plain_repositories(self):
        for cwd, want in [
            ("/Users/alice/Projects/finn", "finn"),
            ("/Users/alice/Projects/finn-loop-writer", "finn-loop-writer"),
            ("/Users/alice/Projects/session-lexicon", "session-lexicon"),
            ("/Users/alice/Projects/my-web-app/", "my-web-app"),
            ("/Users/alice/mae", "mae"),
            ("/Users/alice/Documents/Codex/2026-08-05/mu", "mu"),
            ("/Users/alice/Projects/someoneer-main/SomoneEr", "SomoneEr"),
        ]:
            with self.subTest(cwd=cwd):
                self.assertEqual(project_from_cwd(cwd), want)

    def test_repo_named_after_the_marker_is_left_alone(self):
        self.assertEqual(project_from_cwd("/Users/alice/Projects/my-workspaces-thing"),
                         "my-workspaces-thing")
        self.assertEqual(normalize_project("my-workspaces-thing"),
                         "my-workspaces-thing")

    def test_dash_decoder_still_keeps_a_dashed_repo_name(self):
        self.assertTrue(decode_dash_path("-Users-x-Projects-my-cool-repo")
                        .endswith("my-cool-repo"))
        self.assertEqual(project_from_encoded_dir("-Users-x-Projects-my-cool-repo"),
                         "my-cool-repo")

    def test_ordinary_labels_pass_through_the_repair(self):
        for label in ("finn", "finn-loop-writer", "session-lexicon", "mae",
                      "gemini:6401db6f", "ad-manager"):
            with self.subTest(label=label):
                self.assertEqual(normalize_project(label), label)


class TestLabelRepair(unittest.TestCase):
    """events.ndjson stores the label, not the path, so an already-ingested
    corpus has to be repairable without a re-ingest."""

    def test_the_seven_shipped_labels(self):
        want = {"finn--claude-worktrees-native-ota": "finn",
                "finn--claude-worktrees-graphify-ab-control": "finn",
                "finn--claude-worktrees-graphify-ab2-control": "finn",
                "keep--claude-worktrees-combat-engine": "keep",
                "keep--claude-worktrees-dyslexic-type": "keep",
                "keep--claude-worktrees-art-direction": "keep",
                "finn--claude-worktrees-whimsical-sparking-wilkes": "finn"}
        for label in LEAKED:
            with self.subTest(label=label):
                self.assertEqual(normalize_project(label), want[label])

    def test_repair_is_idempotent(self):
        for label in LEAKED:
            once = normalize_project(label)
            self.assertEqual(normalize_project(once), once)

    def test_pool_only_label_names_nothing(self):
        for label in (".claude-worktrees-secret-branch", "worktrees-secret",
                      "workspaces-SomoneEr-paris", "", None):
            with self.subTest(label=label):
                self.assertEqual(normalize_project(label), UNKNOWN)


class TestNoWorktreeLabelEscapes(unittest.TestCase):
    """The guard: nothing that reaches stats.json says "worktrees"."""

    def _labels(self):
        out = []
        for cwd, _ in WORKTREE_PATHS:
            out.append(project_from_cwd(cwd))
            out.append(project_from_encoded_dir(dash_encode(cwd)))
        for label in LEAKED:
            out.append(normalize_project(label))
        return out

    def test_no_generated_label_mentions_a_worktree(self):
        for label in self._labels():
            with self.subTest(label=label):
                self.assertNotIn("worktree", label.lower())
                self.assertNotIn("workspaces", label.lower())

    def test_no_generated_label_is_one_of_the_leaked_strings(self):
        self.assertFalse(set(self._labels()) & set(LEAKED))

    def test_analyze_strips_worktrees_from_stats(self):
        """End to end at the publication boundary: feed analyze.py a corpus
        that still carries the leaked labels and read the stats it writes."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "events.ndjson")
        with open(src, "w", encoding="utf-8") as fh:
            for i, label in enumerate(LEAKED + ["finn", "keep"]):
                for role, kind in (("user", "prompt"), ("assistant", "reply")):
                    fh.write(json.dumps({
                        "ts": f"2026-08-0{i % 9 + 1}T10:00:00+00:00",
                        "tool": "claude_code", "session_id": f"s{i}",
                        "project": label, "role": role, "kind": kind,
                        "text": "please rebuild the deploy pipeline today",
                        "ts_exact": True, "confidence": "exact",
                    }) + "\n")
        out = os.path.join(tmp, "stats.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "analyze.py"), "--in", src,
             "--out", out, "--vocab", os.path.join(tmp, "vocab.json")],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("worktrees", raw)
        for label in LEAKED:
            self.assertNotIn(label, raw)
        stats = json.loads(raw)
        self.assertEqual(sorted(stats["clouds"]["by_project"]), ["finn", "keep"])
        self.assertEqual(stats["totals"]["projects"], 2)


if __name__ == "__main__":
    unittest.main()
