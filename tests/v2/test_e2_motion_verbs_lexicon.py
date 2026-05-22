"""E2 (R-22 P2) — semantic-class lexicon filter for word_movement.

ROOT CAUSE: top_ngrams_by_author(pos_filter=['VERB']) returns top-
affinity verbs (said, replied, cried for Dickens). NOT motion verbs.
Result: «глаголы движения у Диккенса» either showed generic verbs OR
empty (R-22 P2 case).

Fix: closed-list motion-verbs lexicon (~250 verbs incl. inflections).
top_ngrams_by_author accepts `semantic_class="motion"` param. When set:
  1. pulls wider top from v1 (top × 8)
  2. intersects with lexicon
  3. returns top N motion verbs by affinity rank

word_movement plan passes `semantic_class="motion"` automatically.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.authors._motion_verbs import (
    MOTION_VERBS,
    filter_motion_verbs,
)


class MotionVerbsLexicon(unittest.TestCase):
    """Lexicon contents — sanity checks."""

    def test_has_walk_run_ride(self):
        for v in ("walk", "run", "ride", "sail", "fly", "swim"):
            self.assertIn(v, MOTION_VERBS, f"{v} should be in motion lexicon")

    def test_has_inflected_forms(self):
        # past + gerund + 3p-sg + past participle
        for v in ("walked", "running", "rides", "flown"):
            self.assertIn(v, MOTION_VERBS, f"{v} should be in lexicon")

    def test_excludes_polysemous_verbs(self):
        # These should NOT be in lexicon — too polysemous
        for v in ("set", "take", "give", "make", "do", "have"):
            self.assertNotIn(
                v, MOTION_VERBS,
                f"{v} is too polysemous — keep out of motion lexicon",
            )

    def test_excludes_non_motion(self):
        for v in ("said", "thought", "remembered", "loved", "hated"):
            self.assertNotIn(v, MOTION_VERBS)


class FilterMotionVerbs(unittest.TestCase):
    """The filter helper preserves order and only matches motion verbs."""

    def test_filters_by_lexicon(self):
        rows = [
            {"ngram": "said", "affinity": 5.0},
            {"ngram": "walked", "affinity": 4.5},
            {"ngram": "replied", "affinity": 4.2},
            {"ngram": "ran", "affinity": 4.0},
            {"ngram": "cried", "affinity": 3.8},
            {"ngram": "rode", "affinity": 3.5},
        ]
        out = filter_motion_verbs(rows)
        ngrams = [r["ngram"] for r in out]
        self.assertEqual(ngrams, ["walked", "ran", "rode"])

    def test_preserves_other_fields(self):
        rows = [{"ngram": "walked", "affinity": 4.5, "count": 100}]
        out = filter_motion_verbs(rows)
        self.assertEqual(out[0]["count"], 100)

    def test_case_insensitive(self):
        rows = [{"ngram": "Walked"}, {"ngram": "RAN"}]
        out = filter_motion_verbs(rows)
        self.assertEqual(len(out), 2)

    def test_empty_input(self):
        self.assertEqual(filter_motion_verbs([]), [])

    def test_falls_back_to_word_field(self):
        # Some v1 calls use `word` instead of `ngram`
        rows = [{"word": "ran"}]
        out = filter_motion_verbs(rows, word_key="ngram")
        # Should still work via fallback to «word»
        self.assertEqual(len(out), 1)


class WordMovementPlanUsesLexicon(unittest.TestCase):
    """Plan builder passes semantic_class='motion' to top_ngrams_by_author."""

    def test_plan_has_motion_arg(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import _plan_word_movement

        e = Entities(
            author_regex="^Dickens,",
            raw_misc={"raw_text": "глаголы движения у Диккенса"},
        )
        plan = _plan_word_movement(e)
        self.assertEqual(plan.intent, "word_movement")
        self.assertEqual(len(plan.steps), 1)
        args = plan.steps[0].args
        self.assertEqual(args.get("semantic_class"), "motion")
        self.assertEqual(args.get("pos_filter"), ["VERB"])


class TopNgramsAppliesLexicon(unittest.TestCase):
    """The v2 top_ngrams_by_author wrapper applies lexicon when
    semantic_class is set."""

    def test_wrapper_applies_motion_filter(self):
        import unittest.mock as mock
        from scripts.v2.tools.authors.top_ngrams import top_ngrams_by_author

        fake_v1 = mock.Mock(return_value={
            "top_ngrams": [
                {"ngram": "said", "affinity": 5.0},
                {"ngram": "walked", "affinity": 4.5},
                {"ngram": "replied", "affinity": 4.2},
                {"ngram": "ran", "affinity": 4.0},
                {"ngram": "cried", "affinity": 3.8},
                {"ngram": "rode", "affinity": 3.5},
            ],
            "books_used": 50,
        })
        with mock.patch("scripts.rag_tools.top_ngrams_by_author",
                         side_effect=fake_v1):
            result = top_ngrams_by_author(
                author_regex="^Dickens,",
                n=1, top=25, pos_filter=["VERB"],
                semantic_class="motion",
            )
        # v1 should be called with wider top (200+)
        call_args = fake_v1.call_args
        self.assertGreater(call_args.kwargs["top"], 50,
                            "Should pull wider when semantic_class set")
        # Result has only motion verbs
        self.assertTrue(result.ok)
        rows = result.data.get("top_ngrams", [])
        ngrams = [r["ngram"] for r in rows]
        self.assertEqual(ngrams, ["walked", "ran", "rode"])
        # _semantic_filter metadata recorded
        self.assertEqual(result.data["_semantic_filter"]["class"], "motion")
        self.assertEqual(result.data["_semantic_filter"]["after"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
