"""Regression tests for the four defects users reported on a shared Wrapped.

1. Top words carried text the user never typed (pasted data payloads, and any
   single document able to dominate the cloud).
2. The "signature phrase" was a domain noun pair, because phrase candidates are
   drawn from a stopword-filtered content-word pool that cannot express a
   habit.
3. Manners covered please/thanks/sorry only -- no profanity, frustration,
   praise, urgency.
4. The assistant's own vocabulary never reached the Wrapped at all.
"""
import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wcstats import clean, wrapped as W  # noqa: E402
from wcstats.facets import Bucket  # noqa: E402
from wcstats.tokenize import discourse_ngrams  # noqa: E402


# --- 1. pasted data payloads and single-document dominance -------------------

TRANSLATION_PASTE = '''import json

data = {
  "src/data/academy/compiled.ar.json.10.script.4.chat": "انتقل إلى عرض",
  "src/constants/infoGlossary.ar.json.w52.body": "نطاق أسعار آخر",
  "src/campaign/archetypes.ar.json.stockPick.name": "اختيار سهم",
}
'''


class PastedDataPayloads(unittest.TestCase):
    def test_path_like_json_keys_are_structure_not_prose(self):
        """The key that broke it: long, and containing slashes."""
        line = '  "src/data/academy/compiled.ar.json.10.script.4.chat": "انتقل",'
        self.assertEqual(clean.struct_class(line), clean.STRUCT_STRONG)

    def test_sentence_json_keys_are_structure_not_prose(self):
        line = '"When the chart turns red": "عندما يتحول",'
        self.assertEqual(clean.struct_class(line), clean.STRUCT_STRONG)

    def test_translation_paste_yields_no_prose(self):
        ev = {"role": "user", "kind": "prompt", "text": TRANSLATION_PASTE}
        self.assertEqual(clean.prose_text(ev), "")

    def test_ordinary_labelled_prose_still_survives(self):
        """The cost of the rule above must not be normal writing."""
        ev = {"role": "user", "kind": "prompt",
              "text": "Note: read this first.\nDeliverable: add one action to "
                      "the settings screen and tell me what broke."}
        out = clean.prose_text(ev)
        self.assertIn("settings", out)
        self.assertIn("Deliverable", out)


class SingleDocumentDominance(unittest.TestCase):
    """No one document may set the headline, however many times it says a word."""

    def test_one_document_cannot_outweigh_many(self):
        b = Bucket()
        # One paste screaming a word 500 times.
        b.add_prompt(["payload"] * 500, [], "dup-a")
        # Ten real prompts that each used another word once.
        for i in range(10):
            b.add_prompt(["refactor"], [], f"real-{i}")
        top = b.prose_user.most_common(1)[0][0]
        self.assertEqual(top, "refactor")

    def test_raw_counter_stays_uncapped(self):
        """The toggle that shows unfiltered counts must still be unfiltered."""
        b = Bucket()
        b.add_prompt(["payload"] * 500, [], "dup-a")
        self.assertEqual(b.prose_user_raw["payload"], 500)


# --- 2. the signature phrase -------------------------------------------------

class SignaturePhrase(unittest.TestCase):
    def test_discourse_ngrams_keep_function_words(self):
        """The old candidate pool dropped every word a habit is made of."""
        grams = set(discourse_ngrams("make sure you read the file first"))
        self.assertIn("make sure", grams)

    def test_pure_glue_is_not_a_phrase(self):
        grams = set(discourse_ngrams("one of the things in the list"))
        self.assertNotIn("of the", grams)
        self.assertNotIn("in the", grams)

    def test_signature_prefers_breadth_over_volume(self):
        """A topic said constantly in one project loses to a habit said everywhere."""
        s = W.Signature()
        # A domain phrase, hammered inside a single project.
        for i in range(60):
            s.add(["day trading"], "finn", f"s{i}")
        # A verbal habit, used less often but across the whole year's work.
        for i, proj in enumerate(["finn", "cv", "ads", "lexicon", "keep"] * 4):
            s.add(["make sure"], proj, f"t{i}")
        self.assertEqual(s.best(), "make sure")

    def test_ngram_pool_keeps_the_identity_guards(self):
        """Skipping keep() to reach function words must not skip its guards.

        It did, once: `izs.me grok.com` and `mail about izs.me` shipped into
        stats.json as phrases the model characteristically uses.
        """
        grams = set(discourse_ngrams("mail about izs.me grok.com today"))
        self.assertEqual([g for g in grams if "." in g], [])

    def test_ngram_pool_rejects_filenames_and_versions(self):
        grams = set(discourse_ngrams("run gemini.md and v1.2.3 now"))
        for g in grams:
            self.assertNotIn(".", g)

    def test_signature_needs_repetition(self):
        s = W.Signature()
        s.add(["said once"], "finn", "s1")
        self.assertEqual(s.best(), "")


# --- 3. the full emotional range --------------------------------------------

class Emotions(unittest.TestCase):
    def test_profanity_is_counted(self):
        c = W.emotion_counts("wtf is this, the build is fucked again")
        self.assertGreaterEqual(c["profanity"], 2)

    def test_frustration_is_counted(self):
        c = W.emotion_counts("ugh, this is STILL broken, seriously")
        self.assertGreaterEqual(c["frustration"], 2)

    def test_praise_is_counted(self):
        c = W.emotion_counts("perfect, that works beautifully, love it")
        self.assertGreaterEqual(c["praise"], 2)

    def test_urgency_is_counted(self):
        c = W.emotion_counts("i need this asap, right now")
        self.assertGreaterEqual(c["urgency"], 2)

    def test_politeness_still_counted(self):
        c = W.emotion_counts("please fix it, thanks, sorry about that")
        self.assertEqual(c["please"], 1)
        self.assertEqual(c["thanks"], 1)
        self.assertEqual(c["sorry"], 1)

    def test_ordinary_prose_scores_nothing(self):
        c = W.emotion_counts("add a column to the settings table")
        self.assertEqual(sum(c.values()), 0)

    def test_substrings_do_not_fire(self):
        """'classic' is not a curse; 'shitake' is not either."""
        c = W.emotion_counts("a classic massachusetts shitake analysis")
        self.assertEqual(c["profanity"], 0)

    def test_wrapped_accumulates_emotions(self):
        w = W.Wrapped()
        w.add_user_prose("claude_code", "wtf, please fix this", "2026-01-01")
        self.assertEqual(w.emotions["profanity"], 1)
        self.assertEqual(w.emotions["please"], 1)


# --- 4. the assistant's vocabulary reaches the Wrapped -----------------------

class AssistantVoice(unittest.TestCase):
    def _pieces(self):
        return {
            "per_day": {"2026-01-01": 3},
            "prompts": 3, "sessions": 1,
            "top_words": [{"t": "refactor", "n": 9}],
            "top_phrases": [{"t": "dev server", "n": 4}],
            "assistant_top_words": [{"t": "honestly", "n": 40},
                                    {"t": "smoking", "n": 12}],
            "assistant_top_phrases": [{"t": "smoking gun", "n": 12},
                                      {"t": "full picture", "n": 8}],
            "rising": [{"t": "loop"}],
            "hour_histogram": {9: 3}, "weekday_histogram": {"Thu": 3},
            "tools_used": 1, "projects_count": 1,
            "spend": {}, "priciest_day": {"date": "", "cost": 0.0},
        }

    def test_wrapped_carries_assistant_words(self):
        out = W.build(W.Wrapped(), self._pieces())
        self.assertEqual(out["assistant_top_words"][0][0], "honestly")

    def test_wrapped_carries_assistant_phrase(self):
        out = W.build(W.Wrapped(), self._pieces())
        self.assertEqual(out["assistant_top_phrase"], "smoking gun")

    def test_missing_assistant_data_is_empty_not_fatal(self):
        p = self._pieces()
        del p["assistant_top_words"]
        del p["assistant_top_phrases"]
        out = W.build(W.Wrapped(), p)
        self.assertEqual(out["assistant_top_words"], [])
        self.assertEqual(out["assistant_top_phrase"], "")

    def test_assistant_words_carry_no_paths(self):
        """Same privacy rule as the rest of the card."""
        p = self._pieces()
        p["assistant_top_words"] = [{"t": "/Users/someone/x", "n": 5},
                                    {"t": "honestly", "n": 4}]
        out = W.build(W.Wrapped(), p)
        for word, _ in out["assistant_top_words"]:
            self.assertNotIn("/", word)


if __name__ == "__main__":
    unittest.main()
