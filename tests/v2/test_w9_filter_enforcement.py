"""W-9 tests — declared filters either apply or disclose, never silent.

Stan 2026-05-22 (Phase 3):
    «глаголы движения у Диккенса» → «не нашлось униграммы при текущих
    фильтрах». Filter was silently empty because affinity-ranked top-N
    contained zero motion verbs (Dickens' top affinity verbs = dialogue
    tags). Now: direct corpus scan fallback returns real motion verbs.

    «B2 без архаизмов» → возвращало top-by-downloads без архаизм-фильтра.
    Архаизм-фильтр сейчас не реализован на learning_words v1, поэтому
    surface disclaimer через render_notes — not silent ignore.

R5 compliance: каждый filter имеет позитивный кейс (filter применился)
и негативный кейс (filter явно дисклеймится когда не реализован).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.builders.learning import _plan_learning
from scripts.v2.tools.authors._motion_verbs import (
    MOTION_VERBS,
    count_motion_verbs_in_author,
    filter_motion_verbs,
)


# ---------------------------------------------------------------------------
# word_movement — fallback to direct corpus scan when affinity intersect = 0
# ---------------------------------------------------------------------------


class MotionVerbsDirectScanFallback(unittest.TestCase):
    """When affinity-ranked top-N has zero motion verbs, the wrapper
    must fall back to a direct corpus scan instead of returning empty.
    """

    def test_lexicon_filter_alone_empty_for_dialogue_only_top(self):
        # Replicates Dickens' situation: top affinity verbs are dialogue
        # tags. Intersection with MOTION_VERBS = empty.
        dialogue_top = [
            {"ngram": "said", "count": 12000},
            {"ngram": "replied", "count": 3000},
            {"ngram": "cried", "count": 2500},
            {"ngram": "inquired", "count": 1800},
            {"ngram": "answered", "count": 1500},
        ]
        kept = filter_motion_verbs(dialogue_top, word_key="ngram")
        self.assertEqual(kept, [],
                         msg="dialogue tags must not pass motion filter")

    def test_lexicon_filter_keeps_motion_verbs(self):
        # Positive: real motion verbs survive
        mixed = [
            {"ngram": "said", "count": 12000},
            {"ngram": "walked", "count": 3000},
            {"ngram": "ran", "count": 1800},
            {"ngram": "replied", "count": 1500},
            {"ngram": "rode", "count": 900},
        ]
        kept = filter_motion_verbs(mixed, word_key="ngram")
        kept_grams = {r["ngram"] for r in kept}
        self.assertEqual(kept_grams, {"walked", "ran", "rode"})

    def test_motion_verbs_lexicon_covers_obvious_set(self):
        # Confirm the lexicon includes verbs we explicitly want to surface
        for verb in ("walked", "ran", "rode", "came", "went",
                      "fled", "rushed", "hastened", "climbed",
                      "leaped", "fell", "drove", "sailed"):
            self.assertIn(verb, MOTION_VERBS,
                          msg=f"{verb!r} must be in MOTION_VERBS lexicon")

    def test_count_motion_verbs_in_author_returns_empty_on_missing_corpus(self):
        # On a dev workstation without /workspace/spgc/, the function must
        # return [] gracefully — not crash.
        result = count_motion_verbs_in_author(
            author_regex="^DefinitelyNotARealAuthor,",
            top=10,
        )
        self.assertEqual(result, [])

    def test_wrapper_falls_back_to_direct_scan_when_affinity_empty(self):
        # Integration: when v1 returns dialogue-only top, the wrapper
        # uses the direct-scan fallback. We stub both v1 and the scan
        # helper, assert the wrapper composes them right.
        from scripts.v2.tools.authors import top_ngrams as tn

        fake_v1_dialogue_only = {
            "author_regex": "^Dickens,", "n": 1, "pos_filter": ["VERB"],
            "books_used": 20, "total_ngrams": 5_000_000,
            "top": [
                {"ngram": "said", "count": 12000},
                {"ngram": "replied", "count": 3000},
                {"ngram": "cried", "count": 2500},
            ],
        }
        fake_scan_result = [
            {"ngram": "came", "count": 4500},
            {"ngram": "went", "count": 4000},
            {"ngram": "walked", "count": 1500},
            {"ngram": "rode", "count": 900},
            {"ngram": "fell", "count": 700},
        ]

        with patch("scripts.rag_tools.top_ngrams_by_author",
                    return_value=fake_v1_dialogue_only), \
             patch("scripts.v2.tools.authors._motion_verbs."
                    "count_motion_verbs_in_author",
                    return_value=fake_scan_result):
            result = tn.top_ngrams_by_author(
                author_regex="^Dickens,", n=1, top=10,
                pos_filter=["VERB"], semantic_class="motion",
            )

        self.assertTrue(result.ok)
        data = result.data
        rows = data.get("top") or []
        # Result must be non-empty — that's the W-9 acceptance criterion
        self.assertGreater(len(rows), 0,
                           msg="motion-verb result must not be silently empty")
        grams = {r["ngram"] for r in rows}
        # All returned items must be motion verbs
        for g in grams:
            self.assertIn(g, MOTION_VERBS,
                          msg=f"{g!r} surfaced but not in motion lexicon")
        # Fallback metadata exposed
        sf = data.get("_semantic_filter") or {}
        self.assertTrue(sf.get("fallback_direct_scan"),
                        msg="fallback flag missing — renderer can't disclose")
        # Warning surfaces the strategy switch (otherwise renderer
        # would silently misrepresent the ranking)
        codes = {w.code for w in result.warnings}
        self.assertIn("semantic_fallback_used", codes)

    def test_wrapper_keeps_affinity_top_when_intersection_nonempty(self):
        # Negative: when v1 already returns motion verbs in top-N (some
        # authors do — adventure fiction), the wrapper sticks with the
        # affinity ranking and does NOT call the scan fallback.
        from scripts.v2.tools.authors import top_ngrams as tn

        fake_v1_with_motion = {
            "author_regex": "^Stevenson,", "n": 1, "pos_filter": ["VERB"],
            "books_used": 8, "total_ngrams": 1_000_000,
            "top": [
                {"ngram": "ran", "count": 800},
                {"ngram": "said", "count": 500},
                {"ngram": "rushed", "count": 300},
                {"ngram": "walked", "count": 250},
            ],
        }
        scan_spy = []

        def scan_spy_fn(**kwargs):
            scan_spy.append(kwargs)
            return [{"ngram": "fell", "count": 1}]

        with patch("scripts.rag_tools.top_ngrams_by_author",
                    return_value=fake_v1_with_motion), \
             patch("scripts.v2.tools.authors._motion_verbs."
                    "count_motion_verbs_in_author",
                    side_effect=scan_spy_fn):
            result = tn.top_ngrams_by_author(
                author_regex="^Stevenson,", n=1, top=10,
                pos_filter=["VERB"], semantic_class="motion",
            )

        self.assertTrue(result.ok)
        rows = result.data.get("top") or []
        # Affinity intersection was non-empty → fallback was NOT used
        self.assertEqual(scan_spy, [],
                         msg="fallback called even though affinity had matches")
        # No fallback flag, no semantic_fallback_used warning
        sf = result.data.get("_semantic_filter") or {}
        self.assertFalse(sf.get("fallback_direct_scan"))
        codes = {w.code for w in result.warnings}
        self.assertNotIn("semantic_fallback_used", codes)


# ---------------------------------------------------------------------------
# exclude_archaic on learning_words — disclose, don't silently ignore
# ---------------------------------------------------------------------------


class ExcludeArchaicLearningDisclosure(unittest.TestCase):
    """When user asks «B2 без архаизмов» for learning_words, the plan
    must stamp a render_note disclosing that the archaic filter isn't
    implemented at this layer — instead of silently returning top
    band-pass without filtering archaisms.
    """

    def test_exclude_archaic_extracted_from_query(self):
        # Sanity — the entities layer should detect the phrase
        e = extract("дай 30 слов B2 для Pride and Prejudice без архаизмов")
        self.assertTrue(e.exclude_archaic,
                        msg="exclude_archaic must extract from RU phrase")

    def test_plan_stamps_render_note_when_exclude_archaic(self):
        e = extract("дай 30 слов B2 для Pride and Prejudice без архаизмов")
        plan = _plan_learning(e)
        self.assertTrue(
            plan.render_notes,
            msg=("expected render_notes when exclude_archaic flagged on "
                 "learning intent"),
        )
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("архаизм", joined)
        # Honest disclosure phrasing: must mention that filter is not
        # implemented OR explicitly tell the renderer to disclose.
        self.assertTrue(
            "disclose" in joined or "не имеет" in joined,
            msg="render_note must contain explicit disclosure instruction",
        )

    def test_plan_does_NOT_stamp_render_note_without_exclude_archaic(self):
        # Negative: regular «B2 для P&P» — no archaic flag, no note.
        e = extract("дай B2 слова для Pride and Prejudice")
        plan = _plan_learning(e)
        self.assertEqual(plan.render_notes, [])

    def test_plan_still_routes_to_learning_words_with_disclosure(self):
        # The plan still executes; it doesn't bail out into clarify.
        # The filter is unimplemented but the tool call is fine.
        e = extract("слова B2 из Pride and Prejudice без архаизмов")
        plan = _plan_learning(e)
        self.assertEqual(plan.intent, "learning")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].tool, "learning_words")

    def test_explain_string_flags_exclude_archaic(self):
        e = extract("слова B2 из Pride and Prejudice без архаизмов")
        plan = _plan_learning(e)
        self.assertIn("exclude_archaic", plan.explain)


# ---------------------------------------------------------------------------
# country / year_from-to / lang_hint propagation — stable, not silent-drop
# ---------------------------------------------------------------------------


class FilterPropagationStability(unittest.TestCase):
    """Filters that ARE implemented must propagate end-to-end."""

    def test_year_range_propagates_to_word_movement_args(self):
        from scripts.v2.planner.builders.word import _plan_word_movement
        e = extract("глаголы движения у Dickens 1840-1860")
        # We can't trust the exact regex extraction; force the entity
        # field directly to test plan construction.
        e.author_regex = "^Dickens,"
        e.year_from, e.year_to = 1840, 1860
        plan = _plan_word_movement(e)
        self.assertEqual(plan.intent, "word_movement")
        args = plan.steps[0].args
        self.assertEqual(args["year_from"], 1840)
        self.assertEqual(args["year_to"], 1860)
        self.assertEqual(args["pos_filter"], ["VERB"])
        self.assertEqual(args["semantic_class"], "motion")

    def test_country_propagates_to_word_movement_args(self):
        from scripts.v2.planner.builders.word import _plan_word_movement
        e = extract("motion verbs in British literature")
        e.author_regex = ".*"
        e.country = "GB"
        plan = _plan_word_movement(e)
        self.assertEqual(plan.steps[0].args["country"], "GB")

    def test_lang_hint_propagates_via_book_recommendation(self):
        from scripts.v2.planner.builders.book import _plan_book_recommendation
        e = extract("recommend a French novel for B1")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.steps[0].args.get("lang"), "fr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
