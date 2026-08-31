"""Test suite. Runs on fixtures only -- never touches real session data."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)

from adapters import (ADAPTERS, antigravity, claude_code, codex,  # noqa: E402
                      cursor, gemini_cli, grok)
from adapters.base import argv_head, decode_dash_path, iso, usage  # noqa: E402
from adapters.protoscan import extract_strings  # noqa: E402
from analyze import SCHEMA_VERSION  # noqa: E402
from wcstats.clean import (STRUCT_AMBIGUOUS, STRUCT_GLUE,  # noqa: E402
                           STRUCT_PROSE, STRUCT_STRONG, clean_prose,
                           drop_structured_lines, is_injected, looks_like_code,
                           prose_text, struct_class)
from wcstats.facets import error_signature  # noqa: E402
from wcstats.score import log_odds, pmi_phrases, trends  # noqa: E402
from wcstats.spend import (Spend, event_cost, load_prices,  # noqa: E402
                           match_price)
from wcstats.tokenize import (cluster_prompts, phrase_candidates,  # noqa: E402
                              shingle_key, tokens)
from wcstats.wrapped import Wrapped, longest_streak  # noqa: E402


class FakeReport:
    def __init__(self):
        self.unknowns = Counter()
        self.ignored_c = Counter()
        self.bad = 0

    def bad_line(self, tool): self.bad += 1
    def bad_file(self, tool): self.bad += 1
    def unknown(self, tool, name): self.unknowns[name] += 1
    def ignored(self, tool, name): self.ignored_c[name] += 1


def collect(mod, path):
    r = FakeReport()
    return list(mod.iter_events(path, r)), r


class TestClaudeAdapter(unittest.TestCase):
    def setUp(self):
        self.ev, self.rep = collect(claude_code,
                                    os.path.join(FIX, "claude_sample.jsonl"))

    def test_real_prompt_captured(self):
        prompts = [e for e in self.ev if e.kind == "prompt"]
        self.assertIn("Refactor the payment retry logic and add a regression test",
                      [p.text for p in prompts])

    def test_attachment_is_meta_never_prose(self):
        atts = [e for e in self.ev if e.role == "system"]
        self.assertTrue(atts, "attachment record should still be emitted")
        for a in atts:
            self.assertEqual(a.kind, "meta")
            self.assertEqual(prose_text(a.__dict__ if hasattr(a, "__dict__")
                                        else {"kind": a.kind, "role": a.role,
                                              "text": a.text}), "")

    def test_thinking_text_and_tool_call(self):
        kinds = {e.kind for e in self.ev}
        self.assertTrue({"thinking", "reply", "tool_call", "tool_result",
                         "error"} <= kinds)
        call = next(e for e in self.ev if e.kind == "tool_call")
        self.assertEqual(call.tool_name, "Bash")
        self.assertEqual(call.cmd, "git status --short")

    def test_error_result_flagged(self):
        errs = [e for e in self.ev if e.kind == "error"]
        self.assertEqual(len(errs), 1)
        self.assertIn("fatal", errs[0].text)

    def test_tool_result_labelled_with_tool_name(self):
        res = [e for e in self.ev if e.kind == "tool_result"]
        self.assertTrue(all(r.tool_name == "Bash" for r in res))

    def test_housekeeping_ignored_not_unknown(self):
        self.assertEqual(dict(self.rep.unknowns), {})
        self.assertIn("mode", self.rep.ignored_c)

    def test_skill_injection_never_becomes_prose(self):
        skill = [e for e in self.ev if e.text.startswith("Base directory")]
        self.assertEqual(len(skill), 1)
        self.assertEqual(prose_text({"kind": skill[0].kind, "role": skill[0].role,
                                     "text": skill[0].text}), "")

    def test_usage_extracted_with_cache_buckets_kept_apart(self):
        u = [e for e in self.ev if e.usage]
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].model, "claude-opus-5")
        # input_tokens EXCLUDES both cache figures upstream; adding them
        # together here would bill 21,512 input tokens instead of 12.
        self.assertEqual(u[0].usage, {"input": 12, "output": 340,
                                      "cache_read": 20000, "cache_write": 1500})

    def test_repeated_block_records_billed_once(self):
        """One API response is written as one record per content block, each
        repeating the full usage. Billing every record doubles the invoice."""
        blocks = [e for e in self.ev if e.tool_name == "Bash"
                  and e.kind == "tool_call"]
        self.assertEqual(len(blocks), 2, "both tool_use blocks should survive")
        self.assertEqual(sum(1 for e in self.ev if e.usage), 1)

    def test_synthetic_zero_usage_is_not_a_model(self):
        """<synthetic> records carry an all-zero usage block; counting them
        would park a phantom model in unpriced_models forever."""
        self.assertNotIn("<synthetic>", [e.model for e in self.ev])


class TestCodexAdapter(unittest.TestCase):
    def setUp(self):
        self.ev, self.rep = collect(codex, os.path.join(FIX, "codex_sample.jsonl"))

    def test_prefers_clean_user_message(self):
        prompts = [e.text for e in self.ev if e.kind == "prompt"]
        self.assertEqual(prompts, ["Profile the slow migration script"])

    def test_duplicate_user_copy_dropped(self):
        self.assertFalse(any("INJECTED CONTEXT" in e.text for e in self.ev))

    def test_developer_message_is_meta(self):
        devs = [e for e in self.ev if e.role == "system"]
        self.assertTrue(devs)
        self.assertTrue(all(d.kind == "meta" for d in devs))

    def test_shell_command_unwrapped(self):
        call = next(e for e in self.ev if e.kind == "tool_call")
        self.assertEqual(call.cmd, "git log --oneline -5")

    def test_project_from_session_meta(self):
        self.assertTrue(all(e.project == "demo" for e in self.ev))

    def test_token_count_captured(self):
        self.assertIn(4242, [e.tokens for e in self.ev])

    def test_no_unknown_types(self):
        self.assertEqual(dict(self.rep.unknowns), {})


class TestCodexCumulativeTrap(unittest.TestCase):
    """`token_count` re-fires on idle and `total_token_usage` is cumulative for
    the whole session. On a long rollout, summing `last_token_usage` overcounts
    input tokens by roughly a third. Differencing the cumulative counter is the
    only aggregation that survives repeats."""

    def setUp(self):
        self.ev, self.rep = collect(codex, os.path.join(FIX, "codex_usage.jsonl"))
        self.u = [e for e in self.ev if e.usage]

    def test_deltas_telescope_to_the_final_cumulative_total(self):
        got = {k: sum(e.usage[k] for e in self.u)
               for k in ("input", "output", "cache_read")}
        # Final total_token_usage: input 5000 (1800 of it cached), output 400.
        self.assertEqual(got, {"input": 3200, "cache_read": 1800, "output": 400})

    def test_repeated_identical_events_add_nothing(self):
        """Three token_count events carry the same cumulative total; only the
        first of them may bill. A naive sum of last_token_usage reads 9000."""
        self.assertEqual(len(self.u), 3)
        self.assertNotEqual(sum(e.usage["input"] + e.usage["cache_read"]
                                for e in self.u), 9000)

    def test_reasoning_tokens_are_not_added_to_output(self):
        """reasoning_output_tokens is a SUBSET of output_tokens -- verified on
        real rollouts where total_tokens == input_tokens + output_tokens."""
        self.assertEqual(sum(e.usage["output"] for e in self.u), 400)

    def test_model_resolved_before_the_first_turn_context(self):
        """The first token_count precedes session_meta here, as it does in
        real rollouts; the look-ahead keeps those tokens off `unknown`."""
        self.assertEqual(self.u[0].model, "gpt-5.6-terra")

    def test_model_switch_mid_session_rebills(self):
        self.assertEqual(self.u[-1].model, "gpt-5.4-mini")

    def test_no_unknown_types(self):
        self.assertEqual(dict(self.rep.unknowns), {})


class TestGrokAdapter(unittest.TestCase):
    def setUp(self):
        p = os.path.join(FIX, "grok", "%2FUsers%2Fx%2FProjects%2Fdemo",
                         "sess1", "chat_history.jsonl")
        self.ev, self.rep = collect(grok, p)

    def test_prompt_timestamped_from_prompt_history(self):
        p = next(e for e in self.ev if e.kind == "prompt")
        self.assertTrue(p.ts_exact, "prompt_history join should give an exact ts")
        self.assertTrue(p.ts.startswith("2026-08-03"))

    def test_synthetic_message_excluded_from_prose(self):
        self.assertFalse(any(e.kind == "prompt" and "INJECTED" in e.text
                             for e in self.ev))

    def test_non_prompt_events_marked_inexact(self):
        for e in self.ev:
            if e.kind != "prompt":
                self.assertFalse(e.ts_exact)

    def test_backend_tool_call_query_captured(self):
        c = [e for e in self.ev if e.tool_name == "web_search"]
        self.assertEqual(len(c), 1)
        self.assertIn("pluralization", c[0].text)

    def test_project_decoded_from_url_encoding(self):
        self.assertTrue(all(e.project == "demo" for e in self.ev))

    def test_carries_no_usage(self):
        """Verified across ~125 real chat_history.jsonl files: Grok records
        model_id and reasoning_effort but never a token count."""
        self.assertFalse(any(e.usage or e.model for e in self.ev))

    def test_no_unknown_types(self):
        self.assertEqual(dict(self.rep.unknowns), {})


class TestGeminiAdapter(unittest.TestCase):
    def setUp(self):
        p = os.path.join(FIX, "gemini", "hash1", "chats", "session-1.json")
        self.ev, self.rep = collect(gemini_cli, p)

    def test_prompt_and_reply(self):
        self.assertIn("Summarise this company's debt position",
                      [e.text for e in self.ev if e.kind == "prompt"])
        self.assertIn("Leverage is high relative to the sector",
                      [e.text for e in self.ev if e.kind == "reply"])

    def test_tool_call_recorded(self):
        c = next(e for e in self.ev if e.kind == "tool_call")
        self.assertEqual(c.tool_name, "read_file")

    def test_usage_folds_thoughts_into_output_and_cached_out_of_input(self):
        u = [e for e in self.ev if e.usage]
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].model, "gemini-2.5-pro")
        # total = input + output + thoughts, so `cached` is inside `input`
        # (8000 - 6000) and `thoughts` is outside `output` (200 + 300).
        self.assertEqual(u[0].usage, {"input": 2000, "output": 500,
                                      "cache_read": 6000, "cache_write": 0})

    def test_no_unknown_types(self):
        self.assertEqual(dict(self.rep.unknowns), {})


# --- Cursor -----------------------------------------------------------------
#
# Cursor keeps every chat in one SQLite key/value store, so there is no small
# file to check in as a fixture -- and a real one would be somebody's actual
# prompts. The store is built here instead, from invented text, in the shape
# the adapter documents.

CUR_CONVO = "c0000000-0000-4000-8000-000000000001"
CUR_ORPHAN = "c0000000-0000-4000-8000-000000000002"
CUR_JOINED = "c0000000-0000-4000-8000-000000000003"
CUR_BLANK = "c0000000-0000-4000-8000-000000000004"


def _cursor_store(tmp, rows, workspaces=()):
    """Write a Cursor-shaped globalStorage store and return its db path.

    `rows` is (key, value) straight into cursorDiskKV, so a test can put a NULL
    or a torn string in a slot the adapter expects JSON in. `workspaces` is
    (hash, folder-uri, [composerId, ...]) for the workspaceStorage join.
    """
    import sqlite3
    user = os.path.join(tmp, "User")
    gs = os.path.join(user, "globalStorage")
    os.makedirs(gs, exist_ok=True)
    db = os.path.join(gs, "state.vscdb")
    con = sqlite3.connect(db)
    con.execute("create table ItemTable (key text primary key, value blob)")
    con.execute("create table cursorDiskKV (key text primary key, value blob)")
    con.executemany("insert into cursorDiskKV (key, value) values (?, ?)", rows)
    con.commit()
    con.close()

    for whash, folder, cids in workspaces:
        wdir = os.path.join(user, "workspaceStorage", whash)
        os.makedirs(wdir, exist_ok=True)
        with open(os.path.join(wdir, "workspace.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"folder": folder}, fh)
        wcon = sqlite3.connect(os.path.join(wdir, "state.vscdb"))
        wcon.execute("create table ItemTable (key text primary key, value blob)")
        wcon.execute(
            "insert into ItemTable (key, value) values ('composer.composerData', ?)",
            (json.dumps({"allComposers": [{"composerId": c} for c in cids]}),))
        wcon.commit()
        wcon.close()
    return db


def _bubble(cid, bid, **kw):
    d = {"bubbleId": bid, "createdAt": "2026-01-28T22:52:05.309Z"}
    d.update(kw)
    return (f"bubbleId:{cid}:{bid}", json.dumps(d))


class TestCursorAdapter(unittest.TestCase):
    """One conversation, every bubble shape the store actually holds."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        composer = {
            "composerId": CUR_CONVO,
            "createdAt": 1769640725000,
            "modelConfig": {"modelName": "gpt-5.2-codex"},
            # Rendered order, and deliberately NOT the createdAt order: the
            # reply below is stamped earlier than the tool call it follows.
            "fullConversationHeadersOnly": [
                {"bubbleId": "b1", "type": 1},
                {"bubbleId": "b2", "type": 2},
                {"bubbleId": "b3", "type": 2},
                {"bubbleId": "b4", "type": 2},
                {"bubbleId": "b5", "type": 2},
            ],
        }
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps(composer)),
            _bubble(CUR_CONVO, "b1", type=1,
                    text="how do i point this at a second staging bucket",
                    workspaceUris=["file:///Users/x/Projects/harbour%20gate"]),
            _bubble(CUR_CONVO, "b2", type=2, capabilityType=30,
                    thinking={"text": "They want a second bucket, not a rename.",
                              "signature": "opaque"}),
            _bubble(CUR_CONVO, "b3", type=2, capabilityType=15,
                    toolFormerData={
                        "name": "run_terminal_cmd", "status": "completed",
                        "rawArgs": json.dumps({"command": "npm run deploy -- --dry"}),
                        "result": json.dumps({"exitCodeV2": 0}),
                    }),
            _bubble(CUR_CONVO, "b4", type=2, capabilityType=15,
                    toolFormerData={
                        "name": "search_replace", "status": "error",
                        "rawArgs": json.dumps({"file_path": "/Users/x/Projects/harbour gate/deploy.ts"}),
                        "error": {"modelVisibleErrorMessage":
                                  "The string to replace was not found in the file."},
                    }),
            _bubble(CUR_CONVO, "b5", type=2,
                    createdAt="2026-01-28T22:52:04.000Z",
                    text="Added a second bucket rather than renaming the first.",
                    tokenCount={"inputTokens": 11687, "outputTokens": 254}),
        ]
        db = _cursor_store(self.tmp, rows)
        self.ev, self.rep = collect(cursor, db)

    def test_type_one_is_the_user_and_type_two_the_assistant(self):
        self.assertEqual([(e.role, e.kind) for e in self.ev],
                         [("user", "prompt"), ("assistant", "thinking"),
                          ("assistant", "tool_call"), ("tool", "tool_result"),
                          ("assistant", "tool_call"), ("tool", "error"),
                          ("assistant", "reply")])

    def test_header_order_beats_created_at(self):
        """fullConversationHeadersOnly is what Cursor renders. b5 is stamped a
        second BEFORE b1 and still belongs last; sorting by createdAt put the
        reply ahead of the prompt it answers in 3 of 7 real conversations."""
        self.assertEqual(self.ev[-1].text,
                         "Added a second bucket rather than renaming the first.")

    def test_prompt_text_recovered(self):
        p = next(e for e in self.ev if e.kind == "prompt")
        self.assertEqual(p.text,
                         "how do i point this at a second staging bucket")

    def test_thinking_comes_from_the_nested_text_not_the_signature(self):
        t = next(e for e in self.ev if e.kind == "thinking")
        self.assertEqual(t.text, "They want a second bucket, not a rename.")

    def test_tool_call_carries_command_and_exit_code(self):
        c = next(e for e in self.ev if e.tool_name == "run_terminal_cmd")
        self.assertEqual(c.cmd, "npm run deploy -- --dry")
        r = next(e for e in self.ev
                 if e.kind == "tool_result" and e.tool_name == "run_terminal_cmd")
        self.assertEqual(r.exit_code, 0)

    def test_failed_tool_becomes_an_error_with_the_message_the_model_saw(self):
        e = next(x for x in self.ev if x.kind == "error")
        self.assertEqual(e.text,
                         "The string to replace was not found in the file.")
        self.assertEqual(e.tool_name, "search_replace")

    def test_tool_call_text_is_the_path_it_touched(self):
        c = next(e for e in self.ev if e.tool_name == "search_replace"
                 and e.kind == "tool_call")
        self.assertTrue(c.text.endswith("deploy.ts"), c.text)

    def test_usage_attaches_once_with_the_composer_model(self):
        u = [e for e in self.ev if e.usage]
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].model, "gpt-5.2-codex")
        # Cursor reports one undifferentiated input figure -- no cached subset
        # is broken out -- so the two cache slots stay 0.
        self.assertEqual(u[0].usage, {"input": 11687, "output": 254,
                                      "cache_read": 0, "cache_write": 0})

    def test_timestamps_are_exact_and_per_bubble(self):
        self.assertTrue(all(e.ts_exact for e in self.ev))
        self.assertTrue(self.ev[0].ts.startswith("2026-01-28T22:52:05"))

    def test_confidence_is_exact(self):
        """Named JSON fields and a type discriminator Cursor repeats in its own
        header index -- nothing here was read off a payload the way
        antigravity's protobuf step types had to be."""
        self.assertTrue(all(e.confidence == "exact" for e in self.ev))

    def test_percent_escape_in_the_workspace_uri_is_decoded(self):
        self.assertTrue(all(e.project == "harbour gate" for e in self.ev))

    def test_no_unknown_types(self):
        self.assertEqual(dict(self.rep.unknowns), {})


class TestCursorProjectResolution(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, rows, workspaces=()):
        db = _cursor_store(self.tmp, rows, workspaces)
        return collect(cursor, db)

    def test_composer_workspace_identifier_wins(self):
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({
                "workspaceIdentifier": {
                    "id": "deadbeef",
                    "uri": {"fsPath": "/Users/x/Projects/lighthouse"}}})),
            _bubble(CUR_CONVO, "b1", type=1, text="ship it"),
        ]
        ev, _ = self._run(rows)
        self.assertEqual(ev[0].project, "lighthouse")

    def test_workspace_storage_join_when_the_bubbles_carry_no_path(self):
        """The composer and its bubbles name no folder; the only link is that
        this workspace's own composer.composerData lists the id."""
        rows = [
            (f"composerData:{CUR_JOINED}", json.dumps({})),
            _bubble(CUR_JOINED, "b1", type=1, text="rerun the migration"),
        ]
        ev, _ = self._run(rows, [("9f42f69f", "file:///Users/x/Projects/tideway",
                                  [CUR_JOINED])])
        self.assertEqual(ev[0].project, "tideway")

    def test_unresolvable_conversation_never_leaks_the_workspace_hash(self):
        rows = [
            (f"composerData:{CUR_BLANK}", json.dumps({})),
            _bubble(CUR_BLANK, "b1", type=1, text="what changed here"),
        ]
        ev, _ = self._run(rows, [("9f42f69f", "file:///Users/x/Projects/tideway",
                                  ["some-other-composer"])])
        self.assertEqual(ev[0].project, "unknown")
        self.assertNotIn("9f42f69f", [e.project for e in ev])

    def test_worktree_path_folds_onto_its_repo(self):
        """A Cursor window opened on a worktree must be attributed the same way
        every other adapter attributes one -- by repo, never by branch."""
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({})),
            _bubble(CUR_CONVO, "b1", type=1, text="rebase this",
                    workspaceUris=["file:///Users/x/finn/.claude/worktrees/native-ota"]),
        ]
        ev, _ = self._run(rows)
        self.assertEqual(ev[0].project, "finn")


class TestCursorDamage(unittest.TestCase):
    """Cursor is very likely running while this reads, and its store holds
    NULLs and binary blobs beside the JSON. None of that may be fatal."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, rows):
        return collect(cursor, _cursor_store(self.tmp, rows))

    def test_torn_and_null_values_are_counted_not_raised(self):
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({})),
            (f"composerData:{CUR_ORPHAN}", None),
            ("composerData:empty-state-draft", "{not json at all"),
            _bubble(CUR_CONVO, "b1", type=1, text="still here"),
            (f"bubbleId:{CUR_CONVO}:b2", "}}garbage"),
            (f"bubbleId:{CUR_CONVO}:b3", None),
            (f"bubbleId:{CUR_CONVO}:b4", b"\x00\xbc\xff binary"),
        ]
        ev, rep = self._run(rows)
        self.assertEqual([e.text for e in ev], ["still here"])
        self.assertEqual(rep.bad, 5)

    def test_unknown_bubble_type_is_reported_and_dropped(self):
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({})),
            _bubble(CUR_CONVO, "b1", type=1, text="keep me"),
            _bubble(CUR_CONVO, "b2", type=7, text="a type that did not exist yet"),
        ]
        ev, rep = self._run(rows)
        self.assertEqual([e.text for e in ev], ["keep me"])
        self.assertEqual(dict(rep.unknowns), {"type/7": 1})

    def test_unknown_capability_is_reported_but_its_prose_is_kept(self):
        """A new capabilityType must be visible in the run report -- and its
        text must still reach the corpus rather than vanishing silently."""
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({})),
            _bubble(CUR_CONVO, "b1", type=2, capabilityType=99,
                    text="something a later Cursor learned to do"),
        ]
        ev, rep = self._run(rows)
        self.assertEqual([(e.role, e.kind) for e in ev], [("assistant", "reply")])
        self.assertEqual(dict(rep.unknowns), {"capabilityType/99": 1})

    def test_bubbles_survive_a_pruned_composer(self):
        """Cursor drops composerData long before the bubbles go with it: 50 of
        57 composers here had no messages left, and the reverse must not lose
        the messages."""
        rows = [_bubble(CUR_ORPHAN, "b1", type=1, text="the composer is gone"),
                _bubble(CUR_ORPHAN, "b2", type=2, text="the messages are not")]
        ev, rep = self._run(rows)
        self.assertEqual([e.text for e in ev],
                         ["the composer is gone", "the messages are not"])
        self.assertEqual(dict(rep.unknowns), {"composerData/missing": 1})

    def test_textless_bubbles_emit_nothing(self):
        """376 of 399 assistant bubbles here carry no text at all, and 76 of
        the thinking ones hold only the provider's encrypted signature."""
        rows = [
            (f"composerData:{CUR_CONVO}", json.dumps({})),
            _bubble(CUR_CONVO, "b1", type=2, text=""),
            _bubble(CUR_CONVO, "b2", type=2, capabilityType=30,
                    thinking={"text": "", "signature": "encrypted"}),
            _bubble(CUR_CONVO, "b3", type=2, capabilityType=15,
                    toolFormerData={"additionalData": {"status": "error"}}),
        ]
        ev, rep = self._run(rows)
        self.assertEqual(ev, [])
        self.assertEqual(dict(rep.unknowns), {})

    def test_a_missing_store_is_not_discovered(self):
        self.assertNotIn(os.path.join(self.tmp, "nope"), cursor.discover())

    def test_unreadable_database_is_one_bad_file(self):
        gs = os.path.join(self.tmp, "User", "globalStorage")
        os.makedirs(gs, exist_ok=True)
        db = os.path.join(gs, "state.vscdb")
        with open(db, "wb") as fh:
            fh.write(b"this is not a database")
        ev, rep = collect(cursor, db)
        self.assertEqual(ev, [])
        self.assertEqual(rep.bad, 1)


class TestUsageNormalization(unittest.TestCase):
    def test_all_zero_usage_is_none(self):
        self.assertIsNone(usage())
        self.assertIsNone(usage(input=0, output=0, cache_read=0, cache_write=0))

    def test_negative_counts_clamped(self):
        self.assertEqual(usage(input=-5, output=3)["input"], 0)

    def test_usage_omitted_from_serialized_event(self):
        """events.ndjson carries ~500k events; null usage on every one of them
        would cost megabytes for nothing."""
        from adapters.base import Event
        plain = json.loads(Event(None, "t", "s", "p", "user", "prompt", "hi").to_json())
        self.assertNotIn("usage", plain)
        self.assertNotIn("model", plain)
        billed = json.loads(Event(None, "t", "s", "p", "user", "prompt", "hi",
                                  model="m", usage=usage(input=1)).to_json())
        self.assertEqual(billed["usage"]["input"], 1)


class TestProtoScan(unittest.TestCase):
    """The Antigravity walker has no schema, so verify it on bytes we build."""

    @staticmethod
    def _varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _field(self, num, payload):
        return self._varint((num << 3) | 2) + self._varint(len(payload)) + payload

    def test_extracts_nested_utf8(self):
        inner = self._field(1, "translate this file into Hebrew please".encode())
        blob = self._field(3, inner)
        got = [s for _, s in extract_strings(blob)]
        self.assertIn("translate this file into Hebrew please", got)

    def test_ignores_binary_noise(self):
        blob = self._field(1, bytes(range(0, 40)))
        self.assertEqual([s for _, s in extract_strings(blob)], [])

    def test_survives_garbage(self):
        self.assertIsInstance(extract_strings(b"\xff\xff\xff\xff"), list)

    def test_step_type_map_is_total_over_roles(self):
        for st, (role, kind) in antigravity.STEP_TYPES.items():
            self.assertIn(role, {"user", "assistant", "tool", "system"})
            self.assertIn(kind, {"prompt", "reply", "thinking", "tool_call",
                                 "tool_result", "error", "meta"})


class TestClean(unittest.TestCase):
    def test_system_reminder_stripped(self):
        self.assertNotIn("secret",
                         clean_prose("keep <system-reminder>secret</system-reminder> this"))

    def test_fenced_code_stripped(self):
        self.assertNotIn("printf", clean_prose("explain\n```c\nprintf(1);\n```\nthanks"))

    def test_injected_prefixes(self):
        for s in ["Caveat: The messages below were generated by the user",
                  "Base directory for this skill: /x",
                  "The following is the Codex agent history added since",
                  "<task-notification>done</task-notification>"]:
            self.assertTrue(is_injected(s), s)

    def test_authored_prompt_not_injected(self):
        self.assertFalse(is_injected("Fix the flaky test in the payments module"))

    def test_code_detected(self):
        self.assertTrue(looks_like_code('<!doctype html>\n<html>\n<body>\n<div/>'))
        self.assertTrue(looks_like_code('/**\n * Doc\n */\nconst a = 1;\nexport default a;'))

    def test_prose_not_code(self):
        self.assertFalse(looks_like_code(
            "Please read the design spec and tell me whether the retention "
            "handling is sound, then fix anything that is wrong."))

    def test_non_prose_kinds_yield_nothing(self):
        for kind in ("tool_call", "tool_result", "error", "meta"):
            self.assertEqual(prose_text({"kind": kind, "role": "tool",
                                         "text": "lots of output"}), "")

    def test_system_role_yields_nothing(self):
        self.assertEqual(prose_text({"kind": "prompt", "role": "system",
                                     "text": "hello"}), "")

    def test_paths_and_numbers_removed(self):
        out = clean_prose("open /Users/me/Projects/app/main.py at line 42")
        self.assertNotIn("Users", out)
        self.assertNotIn("42", out)


SCHEMA_BLOB = '''{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "type": "object",
  "description": "JSON Schema reference for Claude Code settings",
  "properties": {
    "apiKeyHelper": {
      "description": "Path to a script that outputs authentication values",
      "type": "string"
    },
    "verbose": { "type": "boolean" }
  },
  "required": ["apiKeyHelper"]
}'''


class TestStructuredData(unittest.TestCase):
    """JSON / YAML / TOML must not become vocabulary.

    The bug this class exists for: a settings schema pasted into a prompt is
    not fenced, is not markup, and arrives INSIDE a user turn, so neither
    looks_like_code() nor the role gate could see it. The shipped card told the
    owner his signature phrase was "type string" -- 435 occurrences of
    `"type": "string"`.
    """

    def test_quoted_key_line_is_decisive(self):
        for line in ('"type": "string",', "  '$schema': 'x'",
                     '{"winner": "A|B|tie"},', '"apiKeyHelper": {'):
            self.assertEqual(struct_class(line), STRUCT_STRONG, line)

    def test_sentence_key_is_a_json_key_after_all(self):
        """Reversed, on the evidence of the people who got the card.

        This used to assert the opposite: a translation file's sentence-shaped
        keys were left as prose on the theory that the Arabic values behind
        them were the most unmistakably-his thing on the card. They were not
        his at all. They are machine-generated UI copy that he pasted in to be
        translated, and readers said so unprompted -- `السعر` came tenth in his
        most-used words, ahead of `data` and `files`, off two pasted tables.

        A quoted key against a quoted value is a data row whatever the key
        looks like. What the owner actually types in another script is a
        sentence, and that still survives -- see the test below."""
        line = '"When the chart turns red": "عندما يتحول الرسم البياني",'
        self.assertEqual(struct_class(line), STRUCT_STRONG)

    def test_typed_arabic_sentence_still_survives(self):
        """The cost of the rule above must not be the owner's own languages."""
        line = "اجعل الرسم البياني أحمر عندما ينخفض السهم"
        self.assertEqual(struct_class(line), STRUCT_PROSE)
        self.assertIn("الرسم", clean_prose(line))

    def test_labelled_sentence_is_ambiguous_not_glue(self):
        for line in ("Note: read this first", "Deliverable: add ONE action",
                     "IMPORTANT: do not trigger alerts", "Examples:"):
            self.assertEqual(struct_class(line), STRUCT_AMBIGUOUS, line)

    def test_scalar_assignment_is_glue(self):
        for line in ("line-length = 88", "name: update-config",
                     'target-version = "py39"', '"acceptEdits",', "}", "---"):
            self.assertEqual(struct_class(line), STRUCT_GLUE, line)

    def test_prose_then_pasted_schema_keeps_the_prose(self):
        """The exact shape the corpus has: a real typed question, then a blob.
        Dropping the message would drop the question."""
        got = clean_prose("Have a look at the settings loader and tell me why "
                          "it hangs.\n\n" + SCHEMA_BLOB +
                          "\n\nThen write the migration note.")
        self.assertIn("settings loader", got)
        self.assertIn("migration note", got)
        for gone in ("apiKeyHelper", "boolean", "JSON Schema reference"):
            self.assertNotIn(gone, got, gone)

    def test_the_signature_phrase_bug_itself(self):
        """`type string` came from `"type": "string"` 435 times."""
        self.assertNotIn("type string", phrase_candidates(clean_prose(SCHEMA_BLOB)))
        self.assertEqual(tokens(clean_prose(SCHEMA_BLOB)), [])

    def test_ordinary_colon_in_prose_survives(self):
        got = clean_prose("Note: read this first. The problem: it hangs on "
                          "startup, and the fix: restart the worker.")
        self.assertIn("Note: read this first", got)
        self.assertIn("The problem: it hangs", got)

    def test_the_owners_labelled_run_survives(self):
        """Runs of `Key: value` in this corpus are how the owner writes, not
        YAML: 595 tokens of "Deliverable: / TDD: / Run:" and "Fix: / METHOD: /
        TESTS:". A bare YAML rule would have deleted exactly that."""
        text = ("Deliverable: add ONE action to campaignStore\n"
                "TDD: write the failing test first\n"
                "METHOD: reproduce the leak with a small script\n"
                "Examples:\n"
                "TESTS: add a regression test for each fix")
        self.assertEqual(clean_prose(text), text)

    def test_a_label_at_the_edge_of_a_block_is_still_prose(self):
        """Ambiguous filler only goes when structure stands on BOTH sides of
        it: where prose meets config, the prose wins."""
        above = clean_prose('Note: read this first\n"type": "string"\n}')
        self.assertEqual(above, "Note: read this first")
        below = clean_prose('{\n"type": "string"\n}\nIMPORTANT: check the loader')
        self.assertEqual(below, "IMPORTANT: check the loader")

    def test_frontmatter_block_goes_whole(self):
        got = clean_prose("---\nname: update-config\n"
                          "description: Configure the harness\n---\n\n"
                          "Actually just tell me what the hook does.")
        self.assertEqual(got, "Actually just tell me what the hook does.")

    def test_a_horizontal_rule_mid_paragraph_is_not_frontmatter(self):
        text = ("the first thought here\n---\nthe second thought here")
        self.assertIn("second thought", clean_prose(text))

    def test_toml_block_goes_and_the_question_stays(self):
        got = clean_prose('[tool.black]\nline-length = 88\n'
                          'target-version = "py39"\n\nDoes that look right?')
        self.assertEqual(got, "Does that look right?")

    def test_enum_members_go_with_their_key(self):
        got = clean_prose('Which of these should I allow?\n'
                          '"permissions": [\n  "acceptEdits",\n'
                          '  "bypassPermissions",\n  "plan"\n]')
        self.assertEqual(got, "Which of these should I allow?")
        self.assertNotIn("acceptEdits", got)

    def test_drop_structured_lines_keeps_input_order(self):
        lines = ["first", '"k": 1', "second", "third"]
        self.assertEqual(drop_structured_lines(lines),
                         ["first", "second", "third"])

    def test_no_structure_changes_nothing(self):
        lines = ["a real sentence", "and another one"]
        self.assertEqual(drop_structured_lines(lines), lines)


class TestPhraseAdjacency(unittest.TestCase):
    """A phrase is two words with a space between them.

    phrase_candidates() used to run over a flat token list, which had thrown
    every separator away, so a bigram formed across any gap -- including the
    `": "` between a JSON key and its value.
    """

    def test_a_phrase_does_not_cross_a_colon(self):
        self.assertEqual(phrase_candidates('"type": "string"'), [])
        self.assertEqual(phrase_candidates("kind: string"), [])

    def test_a_phrase_does_not_cross_a_newline(self):
        self.assertEqual(phrase_candidates("deploy\nserver"), [])

    def test_a_phrase_does_not_cross_a_comma(self):
        self.assertEqual(phrase_candidates("fast, cheap"), [])

    def test_a_real_phrase_still_forms(self):
        self.assertIn("dev server", phrase_candidates("the dev server is up"))

    def test_multiple_spaces_are_still_a_space(self):
        self.assertEqual(phrase_candidates("dev   server"), ["dev server"])

    def test_stopwords_still_break_a_pair(self):
        self.assertNotIn("word the", phrase_candidates("word the cloud"))

    def test_non_latin_phrases_survive(self):
        self.assertEqual(phrase_candidates("الرسم البياني"),
                         ["الرسم البياني"])

    def test_offsets_track_the_folded_text(self):
        """NFC can change the length of the string, so the gap has to be read
        off the same text finditer walked."""
        self.assertEqual(phrase_candidates("función rota"),
                         ["función rota"])


class TestTokenize(unittest.TestCase):
    def test_stopwords_removed_domain_verbs_kept(self):
        t = tokens("Please just go and fix the failing deploy test now")
        self.assertNotIn("please", t)
        self.assertNotIn("just", t)
        self.assertIn("fix", t)
        self.assertIn("deploy", t)

    def test_short_and_numeric_dropped(self):
        self.assertEqual(tokens("a b 12 x9y"), [])

    def test_phrases_are_content_pairs(self):
        p = phrase_candidates("build the word cloud dashboard")
        self.assertIn("word cloud", p)
        self.assertNotIn("the word", p)

    def test_shingle_key_stable_across_variants(self):
        a = shingle_key("NEW TASK: read and execute the brief at /a/b.md")
        b = shingle_key("NEW TASK: read and execute the brief at /c/d.md")
        self.assertEqual(a, b)

    def test_distinct_prompts_not_clustered(self):
        self.assertNotEqual(shingle_key("build the dashboard"),
                            shingle_key("delete the database"))

    def test_cluster_counts(self):
        got = dict((r[:8], n) for r, n in cluster_prompts(
            ["run the loop now", "run the loop now", "write the docs"]))
        self.assertEqual(sorted(got.values()), [1, 2])


class TestScore(unittest.TestCase):
    def test_log_odds_ranks_distinctive_term_first(self):
        bg = Counter({"code": 100, "test": 80, "ship": 10, "design": 40})
        tg = Counter({"ship": 9, "code": 5, "design": 3})
        self.assertEqual(log_odds(tg, bg, min_count=3)[0]["t"], "ship")

    def test_log_odds_empty_input(self):
        self.assertEqual(log_odds(Counter(), Counter({"a": 1})), [])

    def test_pmi_prefers_true_collocation(self):
        out = pmi_phrases(Counter({"word cloud": 10, "the code": 30}),
                          Counter({"word": 12, "cloud": 11, "the": 500,
                                   "code": 100}), min_count=4)
        self.assertEqual(out[0]["t"], "word cloud")

    def test_trend_direction(self):
        t = trends({"2026-01": Counter({"a": 20, "b": 2}),
                    "2026-02": Counter({"a": 2, "b": 20})},
                   ["2026-01", "2026-02"])
        self.assertEqual(t["rising"][0]["t"], "b")
        self.assertEqual(t["fading"][0]["t"], "a")

    def test_trends_need_two_months(self):
        self.assertEqual(trends({"2026-01": Counter({"a": 5})}, ["2026-01"]),
                         {"rising": [], "fading": []})


class TestHelpers(unittest.TestCase):
    def test_argv_head_skips_env_assignment(self):
        self.assertEqual(argv_head("FOO=1 /usr/bin/git commit"), "git")

    def test_dash_path_keeps_dashed_repo_name(self):
        self.assertTrue(decode_dash_path("-Users-x-Projects-my-cool-repo")
                        .endswith("my-cool-repo"))

    def test_iso_normalises(self):
        self.assertTrue(iso("2026-08-01T10:00:00Z").startswith("2026-08-01T10:00:00"))
        self.assertIsNone(iso("not a date"))
        self.assertIsNone(iso(None))

    def test_error_signature_masks_volatile_parts(self):
        a = error_signature("Error: cannot open /Users/a/x.py line 42")
        b = error_signature("Error: cannot open /Users/b/y.py line 99")
        self.assertEqual(a, b)


class TestPricing(unittest.TestCase):
    """Hand-checked arithmetic on tiny inputs -- the whole spend section is
    one multiplication repeated, so an error here is invisible at scale."""

    def setUp(self):
        self.prices = load_prices()

    def test_longest_prefix_wins(self):
        self.assertEqual(match_price("gpt-5.4-mini-2026-01-01", self.prices)["pattern"],
                         "gpt-5.4-mini")
        self.assertEqual(match_price("gpt-5.6-terra", self.prices)["pattern"],
                         "gpt-5.6-terra")
        self.assertEqual(match_price("claude-haiku-4-5-20251001", self.prices)["pattern"],
                         "claude-haiku-4-5")

    def test_unknown_model_costs_null_never_zero(self):
        self.assertIsNone(match_price("llama-9-turbo", self.prices))
        self.assertIsNone(event_cost({"input": 1_000_000}, "llama-9-turbo",
                                     "codex", self.prices))
        self.assertIsNone(event_cost({"input": 1}, None, "codex", self.prices))

    def test_anthropic_buckets_are_disjoint(self):
        # opus 5: $5 in / $25 out / $0.50 cache read / $6.25 cache write.
        cost = event_cost({"input": 1_000_000, "output": 1_000_000,
                           "cache_read": 1_000_000, "cache_write": 1_000_000},
                          "claude-opus-5", "claude_code", self.prices)
        self.assertAlmostEqual(cost, 5 + 25 + 0.5 + 6.25, places=6)

    def test_subset_cache_providers_use_the_cached_input_rate(self):
        """Codex and Gemini discount a subset of input rather than billing a
        separate cache bucket, so the cached tokens take `cached_input`."""
        row = match_price("gpt-5.6-terra", self.prices)
        self.assertEqual(row["cache_write"], 0.0)
        cost = event_cost({"input": 1_000_000, "cache_read": 1_000_000,
                           "output": 1_000_000}, "gpt-5.6-terra", "codex",
                          self.prices)
        self.assertAlmostEqual(cost, 1.25 + 0.125 + 10.0, places=6)

    def test_half_a_million_tokens_is_half_the_rate(self):
        self.assertAlmostEqual(
            event_cost({"input": 500_000}, "claude-sonnet-5", "claude_code",
                       self.prices), 1.5, places=6)

    def test_every_locally_observed_model_is_priced(self):
        """The ids this machine's logs actually contain, from a full scan."""
        for m in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5",
                  "claude-haiku-4-5-20251001",
                  "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5",
                  "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2-codex",
                  "gpt-5.2", "gpt-5.1-codex-mini", "codex-auto-review",
                  "gemini-2.5-pro", "gemini-2.5-flash"):
            self.assertIsNotNone(match_price(m, self.prices), m)

    def test_price_rows_are_complete(self):
        for row in self.prices:
            for k in ("input", "output", "cache_read", "cache_write",
                      "cached_input"):
                self.assertIsInstance(row.get(k), (int, float),
                                      f"{row['pattern']} missing {k}")


class TestSpendAggregation(unittest.TestCase):
    def _ev(self, **kw):
        base = {"tool": "claude_code", "session_id": "s1",
                "ts": "2026-08-01T12:00:00+00:00", "model": "claude-opus-5"}
        base.update(kw)
        return base

    def test_unpriced_model_reported_but_never_zeroed(self):
        s = Spend()
        s.add(self._ev(model="mystery-1", usage={"input": 1_000_000}))
        out = s.render()
        self.assertEqual(out["unpriced_models"], ["mystery-1"])
        self.assertIsNone(out["by_model"][0]["cost"])
        self.assertEqual(out["total_tokens"], 1_000_000)

    def test_tools_without_usage_derived_from_the_data(self):
        s = Spend()
        s.add(self._ev(usage={"input": 1000}))
        s.add({"tool": "grok", "session_id": "g1", "ts": None})
        s.add({"tool": "antigravity", "session_id": "a1", "ts": None})
        self.assertEqual(s.render()["tools_without_usage"],
                         ["antigravity", "grok"])

    def test_cache_hit_rate_is_prompt_side_only(self):
        s = Spend()
        s.add(self._ev(usage={"input": 250, "cache_read": 750, "output": 9999}))
        self.assertEqual(s.render()["cache_hit_rate"], 0.75)

    def test_events_without_usage_are_skipped(self):
        s = Spend()
        s.add(self._ev(usage=None))
        s.add(self._ev(usage={"input": 0, "output": 0}))
        out = s.render()
        self.assertEqual((out["total_cost"], out["total_tokens"], out["events"]),
                         (0.0, 0, 0))

    def test_by_day_uses_local_dates(self):
        """"What did I spend on Tuesday" is a local question -- a 23:30 UTC
        turn belongs to the next calendar day for anyone east of Greenwich."""
        from wcstats.spend import local_date
        self.assertRegex(local_date("2026-08-01T23:30:00+00:00"),
                         r"^\d{4}-\d{2}-\d{2}$")
        s = Spend()
        s.add(self._ev(ts="2026-08-01T12:00:00+00:00", usage={"input": 10}))
        self.assertEqual(len(s.render()["by_day"]), 1)

    def test_unparseable_timestamp_never_becomes_a_day_key(self):
        from wcstats.spend import local_date
        for junk in ("nope", "", None, "2026-13-99T00:00:00Z"):
            self.assertIsNone(local_date(junk), junk)
        s = Spend()
        s.add(self._ev(ts=None, usage={"input": 10}))
        self.assertEqual(s.render()["by_day"], [])
        self.assertEqual(s.render()["total_tokens"], 10)

    def test_token_totals_have_a_stable_shape_when_empty(self):
        self.assertEqual(Spend().render()["tokens"],
                         {"input": 0, "output": 0, "cache_read": 0,
                          "cache_write": 0})

    def test_usage_key_tuples_stay_in_sync(self):
        """spend.py duplicates the key list to keep wcstats free of an
        adapters import; nothing else stops the two drifting apart."""
        from adapters.base import USAGE_KEYS as adapter_keys
        from wcstats.spend import USAGE_KEYS as spend_keys
        self.assertEqual(adapter_keys, spend_keys)

    def test_sessions_counted_per_tool(self):
        s = Spend()
        for sid in ("a", "b", "a"):
            s.add(self._ev(session_id=sid, usage={"input": 10}))
        self.assertEqual(s.render()["by_tool"][0]["sessions"], 2)


class TestWrapped(unittest.TestCase):
    def test_streak_needs_consecutive_days(self):
        self.assertEqual(longest_streak(
            ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-09"]), 3)
        self.assertEqual(longest_streak(["2026-08-01", "2026-08-03"]), 1)
        self.assertEqual(longest_streak([]), 0)

    def test_streak_spans_month_and_year_boundaries(self):
        self.assertEqual(longest_streak(
            ["2025-12-30", "2025-12-31", "2026-01-01"]), 3)

    def test_politeness_counted_on_cleaned_prose(self):
        w = Wrapped()
        w.add_user_prose("claude_code", "please fix it, thanks", "2026-08-01")
        w.add_user_prose("claude_code", "sorry, thank you for the patch", "2026-08-01")
        self.assertEqual(dict(w.politeness),
                         {"please": 1, "thanks": 2, "sorry": 1})

    def test_thank_you_counts_once(self):
        w = Wrapped()
        w.add_user_prose("codex", "thank you", "2026-08-01")
        self.assertEqual(w.politeness["thanks"], 1)

    def test_injected_text_can_never_be_polite(self):
        """The counter runs on prose_text output, which drops injected
        context wholesale -- a skill payload full of "please" scores zero."""
        ev = {"kind": "prompt", "role": "user",
              "text": "Base directory for this skill: /x\nplease please please"}
        cleaned = prose_text(ev)
        w = Wrapped()
        if cleaned:
            w.add_user_prose("claude_code", cleaned, "2026-08-01")
        self.assertEqual(w.politeness["please"], 0)

    def test_top_tool_share(self):
        w = Wrapped()
        for _ in range(3):
            w.add_user_prose("codex", "ship it", "2026-08-01")
        w.add_user_prose("claude_code", "ship it", "2026-08-01")
        self.assertEqual(w.top_tool(), {"name": "codex", "share": 0.75})

    def test_words_to_ai_counts_every_word_not_just_content_tokens(self):
        w = Wrapped()
        w.add_user_prose("codex", "please just fix the failing test", "2026-08-01")
        self.assertEqual(w.words_to_ai, 6)


class TestStatsContract(unittest.TestCase):
    """Validate the emitted stats.json against the documented shape."""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "data", "stats.json")
        if not os.path.exists(cls.path):
            raise unittest.SkipTest("data/stats.json not built yet")
        with open(cls.path, encoding="utf-8") as fh:
            cls.S = json.load(fh)

    def test_schema_version(self):
        self.assertEqual(self.S["schema_version"], SCHEMA_VERSION)

    def test_no_absolute_paths_anywhere(self):
        """stats.json is what the README tells you to hand to an agent, and the
        dashboard renders the commands cloud, so a home directory reaching this
        file leaks a username into every screenshot. `git -C /Users/me/proj-x`
        used to land here whole."""
        import re
        # Matched against the JSON *text*, where one real backslash is written
        # as two. An earlier version of this test only knew `C:\Users\` with an
        # uppercase drive, so `c:\users\` and every UNC path passed it.
        blob = json.dumps(self.S)
        # The drive-letter arm needs both guards: `(?<!\w)` so the `s:` in
        # "Errors:\nManualFetchError" is not read as a drive, and a required
        # second separator so a path must actually have two segments.
        found = set(re.findall(
            r"(?:/Users/|/home/"                                  # posix homes
            r"|(?<!\w)[A-Za-z]:\\\\[^\"\\\\ ,]{1,60}\\\\"         # C:\x\  c:\x\
            r"|\\\\\\\\[^\"\\\\ ,]+\\\\)"                         # \\host\share\
            r"[^\"\\\\ ,]{2,80}", blob))
        self.assertEqual(found, set(), f"absolute paths in stats.json: {found}")

    def test_required_top_level_keys(self):
        for k in ("generated_at", "coverage", "totals", "clouds", "trends",
                  "activity", "source_events", "spend", "wrapped"):
            self.assertIn(k, self.S)

    def test_cloud_facets_present(self):
        c = self.S["clouds"]
        for k in ("global", "by_tool", "by_project", "by_month"):
            self.assertIn(k, c)
        for k in ("prose_user", "prose_assistant", "tools", "commands",
                  "errors", "counts"):
            self.assertIn(k, c["global"])

    def test_each_tool_has_distinctive(self):
        for tool, b in self.S["clouds"]["by_tool"].items():
            self.assertIn("distinctive_user", b, tool)

    def test_size_budget(self):
        mb = os.path.getsize(self.path) / 1e6
        self.assertLess(mb, 4.0, f"stats.json is {mb:.2f} MB; budget is 4 MB")

    def test_activity_shape(self):
        a = self.S["activity"]
        self.assertEqual(len(a["hour_histogram"]), 24)
        self.assertEqual(len(a["weekday_histogram"]), 7)

    def test_no_injected_boilerplate_in_top_terms(self):
        """Regression guard: the words that poisoned the first pass."""
        banned = {"sessionid", "toolsummary", "toolaction", "execute_url",
                  "read_url", "escalate_admin", "hookspecificoutput"}
        top = {d["t"] for d in self.S["clouds"]["global"]["prose_user"][:120]}
        self.assertEqual(top & banned, set())

    def test_top_terms_are_words(self):
        for d in self.S["clouds"]["global"]["prose_user"][:50]:
            self.assertGreaterEqual(len(d["t"]), 3)
            self.assertTrue(any(ch.isalpha() for ch in d["t"]))

    def test_spend_shape(self):
        s = self.S["spend"]
        for k in ("currency", "estimates_note", "total_cost", "total_tokens",
                  "by_day", "by_model", "by_tool", "cache_hit_rate",
                  "unpriced_models", "tools_without_usage"):
            self.assertIn(k, s)
        self.assertEqual(s["currency"], "USD")
        for row in s["by_day"]:
            self.assertEqual(set(row), {"date", "cost", "tokens"})
            self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")
        for row in s["by_model"]:
            self.assertEqual(set(row), {"model", "tool", "cost", "input",
                                        "output", "cache_read", "cache_write"})
        for row in s["by_tool"]:
            self.assertEqual(set(row), {"tool", "cost", "tokens", "sessions"})
        self.assertLessEqual(s["cache_hit_rate"], 1.0)

    def test_only_the_expected_sources_lack_usage(self):
        """A source that used to bill and suddenly reports nothing means an
        upstream format changed -- surface it instead of reading as $0."""
        from wcstats.spend import TOOLS_WITHOUT_USAGE
        surprises = set(self.S["spend"]["tools_without_usage"]) - set(TOOLS_WITHOUT_USAGE)
        self.assertEqual(surprises, set())

    def test_spend_never_silently_zeroes_an_unknown_model(self):
        for row in self.S["spend"]["by_model"]:
            if row["model"] in self.S["spend"]["unpriced_models"]:
                self.assertIsNone(row["cost"], row["model"])

    def test_wrapped_shape(self):
        w = self.S["wrapped"]
        for k in ("window", "year", "words_to_ai", "prompts", "sessions",
                  "days_active", "longest_streak_days", "top_words",
                  "top_phrase", "rising_word", "peak_hour", "peak_weekday",
                  "busiest_day", "politeness", "top_tool", "tools_used",
                  "projects_count", "spend"):
            self.assertIn(k, w)
        self.assertEqual(set(w["politeness"]), {"please", "thanks", "sorry"})
        self.assertEqual(set(w["window"]), {"start", "end"})
        self.assertIn(w["peak_weekday"],
                      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertTrue(0 <= w["peak_hour"] <= 23)
        for pair in w["top_words"]:
            self.assertEqual(len(pair), 2)
            self.assertIsInstance(pair[0], str)
        self.assertLessEqual(w["longest_streak_days"], w["days_active"])

    def test_wrapped_leaks_no_project_names_or_paths(self):
        """The share card is public by design: counts and vocabulary only."""
        blob = json.dumps(self.S["wrapped"])
        self.assertNotIn("/Users", blob)
        for p in [d["t"] for d in self.S["activity"]["top_projects"][:10]]:
            if len(p) > 6:
                self.assertNotIn(p, blob, f"project name {p!r} leaked")


class TestDashboardBuild(unittest.TestCase):
    """The built page is a standalone file:// document -- it must declare its
    own encoding and carry its data inline."""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "dashboard.html")
        if not os.path.exists(cls.path):
            raise unittest.SkipTest("dashboard.html not built yet")
        with open(cls.path, encoding="utf-8") as fh:
            cls.html = fh.read()

    def test_charset_declared_early(self):
        """Without this the browser falls back to windows-1252 and every
        middot, em dash and arrow renders as mojibake."""
        head = self.html[:1024]
        self.assertIn('charset="utf-8"', head)

    def test_viewport_declared(self):
        self.assertIn("width=device-width", self.html[:1024])

    def test_placeholder_substituted(self):
        self.assertNotIn("__STATS__", self.html)

    def test_inlined_json_parses(self):
        import re
        m = re.search(r'<script id="statsdata" type="application/json">(.*?)</script>',
                      self.html, re.S)
        self.assertIsNotNone(m, "inlined stats block missing")
        payload = m.group(1)
        self.assertNotIn("</script", payload)
        data = json.loads(payload.replace("<\\/", "</"))
        # Not pinned to SCHEMA_VERSION: dashboard.html is a build artifact and
        # legitimately lags stats.json until build_dashboard.py runs again.
        self.assertIsInstance(data["schema_version"], int)
        self.assertGreaterEqual(data["schema_version"], 1)

    def test_all_views_present(self):
        for v in ("overview", "time", "tools", "projects", "activity"):
            self.assertIn(f'id="v-{v}"', self.html)

    def test_no_external_hosts_at_all(self):
        """The page must fetch nothing: no fonts, no scripts, no images."""
        import re
        hosts = set(re.findall(r'https?://([a-z0-9.\-]+)', self.html))
        hosts.discard("www.w3.org")          # SVG namespace, not a fetch
        self.assertEqual(hosts, set(), f"unexpected external hosts: {hosts}")


class TestInlinedDataCannotEscapeTheScriptBlock(unittest.TestCase):
    """A tool error, a shell command or a project name can carry an HTML
    snippet, and the browser's script-data state machine is unforgiving: an
    "<!--" puts the parser in escaped state, a following "<script" in
    double-escaped state, and from there the template's own "</script>" closes
    nothing. The data block swallows the rest of the document, no tab renders,
    and there is no error anywhere -- just a header above a blank page.

    Escaping "</" alone does not stop it. Escaping every "<" does."""

    PAYLOAD = '<!--<script>alert(1)</script> Error: cannot open thing'

    @staticmethod
    def _stats(payload):
        return {"schema_version": 2,
                "coverage": {}, "activity": {},
                "totals": {"sessions": 1, "prompts": 1},
                "clouds": {"by_project": {},
                           "global": {"errors": [{"t": payload, "n": 1}],
                                      "commands": [{"t": payload, "n": 1}]}}}

    def _build(self, payload):
        """Run the real builder against the real template, into a temp dir."""
        import contextlib
        import io
        import tempfile
        import build_dashboard as bd
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "stats.json")
            out = os.path.join(d, "dashboard.html")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(self._stats(payload), fh)
            keep = (bd.STATS, bd.OUT)
            bd.STATS, bd.OUT = src, out
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(bd.main(), 0)
                with open(out, encoding="utf-8") as fh:
                    return fh.read()
            finally:
                bd.STATS, bd.OUT = keep

    @staticmethod
    def _payload_block(html):
        import re
        m = re.search(r'<script id="statsdata" type="application/json">'
                      r'(.*?)</script>', html, re.S)
        assert m, "inlined stats block missing"
        return m.group(1)

    def test_comment_open_before_a_script_tag_never_reaches_the_page(self):
        html = self._build(self.PAYLOAD)
        self.assertNotIn("<!--<script", html)
        self.assertNotIn("<!--", html[html.index("statsdata"):])

    def test_no_raw_angle_bracket_survives_in_the_data_block(self):
        """One rule, no exceptions: "<" cannot appear in the payload at all,
        so no sequence of it can move the parser out of script-data."""
        self.assertNotIn("<", self._payload_block(self._build(self.PAYLOAD)))

    def test_the_page_still_owns_exactly_its_own_two_script_blocks(self):
        """document.scripts.length was 1 with the old escaping: the data block
        never closed, so the code block was never parsed as script."""
        html = self._build(self.PAYLOAD)
        self.assertEqual(html.count("<script"), 2)
        self.assertEqual(html.count("</script>"), 2)

    def test_payload_round_trips_through_json_parse(self):
        """The escaping uses the one JSON already defines for "<", and "<"
        only ever occurs inside a JSON string, so the reader gets the text
        back byte for byte."""
        block = self._payload_block(self._build(self.PAYLOAD))
        data = json.loads(block)                    # no un-escaping first
        self.assertEqual(data["clouds"]["global"]["errors"][0]["t"],
                         self.PAYLOAD)
        self.assertEqual(data["clouds"]["global"]["commands"][0]["t"],
                         self.PAYLOAD)

    def test_the_older_closing_tag_break_out_is_still_covered(self):
        payload = 'fine until </script><script>alert(2)</script>'
        html = self._build(payload)
        self.assertNotIn("</script><script>", html)
        self.assertEqual(
            json.loads(self._payload_block(html))["clouds"]["global"]["errors"][0]["t"],
            payload)

    def test_built_page_is_not_in_quirks_mode(self):
        """No doctype means compatMode "BackCompat" -- a different box model
        under the same CSS."""
        html = self._build("ordinary text")
        self.assertTrue(html.startswith("<!DOCTYPE html>"), html[:40])
        self.assertIn('<html lang="en">', html[:80])
        self.assertIn('charset="utf-8"', html[:1024])


class TestTemplateContract(unittest.TestCase):
    """Properties of the template itself, so they hold on a fresh clone --
    before anything has been ingested, analysed or built."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "dashboard_template.html"),
                  encoding="utf-8") as fh:
            cls.t = fh.read()

    def test_doctype_and_language_lead_the_file(self):
        self.assertTrue(self.t.startswith('<!DOCTYPE html>\n<html lang="en">'),
                        self.t[:40])

    def test_share_card_footnote_asks_for_a_read_not_reassurance(self):
        """The card carries no project names -- but the words on it are the
        reader's own prose, so the footnote must prompt a check."""
        self.assertIn("Read them before you post", self.t)
        self.assertNotIn("counts and vocabulary only", self.t)

    def test_card_words_can_be_dropped(self):
        for probe in ("wrEx", "wrCardWords", "wr-chip", "Tap to drop"):
            self.assertIn(probe, self.t, probe)

    def test_the_card_draws_only_the_words_that_were_kept(self):
        """wrDrawCard reading wrWords() again would put a dropped word
        straight back onto the canvas -- and into the downloaded PNG."""
        body = self.t[self.t.index("function wrDrawCard("):
                      self.t.index("\nlet wrPainted")]
        self.assertIn("wrCardWords()", body)
        self.assertNotIn("wrWords()", body)

    def test_spend_cell_says_what_the_estimate_is_made_of(self):
        """"SPEND (EST.)" alone reads as a bill once the card is a JPEG on
        somebody else's timeline."""
        self.assertIn("list prices · not a bill", self.t)

    def _tool_map(self, name):
        body = self.t[self.t.index(f"const {name} = {{") + len(f"const {name} = "):]
        return dict(re.findall(r"(\w+)\s*:\s*'([^']+)'",
                               body[:body.index("};")]))

    def test_every_source_has_its_own_colour_and_a_display_name(self):
        """A source with no TOOL_COLOR slot falls through to --ink-3, so it
        draws in the same grey as every other unmapped one and the chart
        silently stops distinguishing them."""
        colors, labels = self._tool_map("TOOL_COLOR"), self._tool_map("TOOL_LABEL")
        self.assertEqual(set(colors), set(ADAPTERS))
        self.assertEqual(set(labels), set(ADAPTERS))
        self.assertEqual(len(set(colors.values())), len(ADAPTERS),
                         "two sources share a slot: " + repr(colors))

    def test_every_tool_slot_is_defined_in_light_and_in_both_darks(self):
        """Dark is written twice -- once under prefers-color-scheme and once
        under [data-theme="dark"] -- so a slot added to only one of them is
        empty for either the OS setting or the toggle, and cssv() then returns
        "" and the mark draws transparent."""
        blocks = {
            "light": self.t[self.t.index(":root{"):self.t.index("@media (prefers")],
            "media dark": self.t[self.t.index("@media (prefers"):
                                 self.t.index(':root[data-theme="dark"]')],
            "toggle dark": self.t[self.t.index(':root[data-theme="dark"]'):
                                  self.t.index("*{box-sizing")],
        }
        for slot in sorted(set(self._tool_map("TOOL_COLOR").values())):
            for where, block in blocks.items():
                self.assertRegex(block, re.escape(slot) + r"\s*:\s*#[0-9a-f]{6}",
                                 f"{slot} missing from the {where} palette")

    def test_nothing_claims_the_user_typed_it(self):
        """A prompt event is whatever arrived in the user's turn -- typed,
        pasted, or folded in by a skill -- and the pipeline cannot tell those
        apart. So the copy may say what appeared in the prompts and may not say
        whose fingers put it there. This is the string the owner caught:
        "YOUR SIGNATURE PHRASE ... You typed it often enough"."""
        for claim in ("You typed", "you typed", "WORDS TYPED", "MOST-TYPED",
                      "Most-typed", "words typed", "prompts typed",
                      "really typed"):
            self.assertNotIn(claim, self.t, claim)

    def test_the_honest_replacements_are_there(self):
        for kept in ("WORDS SENT TO AI", "MOST-USED WORDS",
                     "It came up in your prompts often enough",
                     "Everything you sent to an AI on this machine"):
            self.assertIn(kept, self.t, kept)

    def test_the_card_footnote_owns_up_to_pasted_text(self):
        """The one place a plain sentence about provenance earns its keep: the
        small print under the thing the reader is about to post."""
        self.assertIn("anything you pasted into them included", self.t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
