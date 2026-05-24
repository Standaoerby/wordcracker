"""W-11 + W-12 acceptance suite (Phase 5 P2, 2026-05-24).

Direct mirror of the «приёмка» / acceptance section in
tz_claude_code_fixes_2026-05-22.md:

    W-11: «слова викторианцев 1837-1901» → результат либо быстрый
          smart-clarify; разумные запросы не уходят в 50s+ parse-fail.
    W-12: «какие слова стали чаще к концу XIX века» → ранжированный
          список по росту частоты.

These tests are deliberately phrased as «приёмка-as-test» — each method
documents the exact prod-shaped query Stan called out, then asserts
the plan / tool args / classifier outcome the user would observe.

R5: positive (acceptance query → expected behavior) and a couple of
negatives so we don't accidentally over-fit and steal adjacent
queries («слова после 1900» stays in word_timeline drop path).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import build


# ---------------------------------------------------------------------------
# W-11 acceptance — period-vocab + book-difficulty queries don't parse-fail
# ---------------------------------------------------------------------------


class W11Acceptance(unittest.TestCase):

    def test_victorian_word_query_executes(self):
        """«слова викторианцев 1837-1901» → period_vocab plan with the
        year range from the query. Was: LLM fallback 50-60s parse-fail
        before W-11; now: executable single-step plan."""
        q = "слова викторианцев 1837-1901"
        intent = classify(q)
        e = extract(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "period_vocab")
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(plan.steps[0].tool, "top_ngrams_by_author")
        self.assertEqual(plan.steps[0].args["year_from"], 1837)
        self.assertEqual(plan.steps[0].args["year_to"], 1901)

    def test_hardest_19c_books_gets_fast_recipe(self):
        """«какие книги XIX века самые сложные» → book_extremum smart-
        clarify with 2-step recipe (top_books → book_readability per
        candidate). Was: parse-fail; now: <1s recipe."""
        q = "какие книги XIX века самые сложные"
        intent = classify(q)
        e = extract(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "book_extremum")
        self.assertTrue(plan.needs_clarify, plan.explain)
        self.assertTrue(plan.authoritative_clarify)
        # Recipe must point at the two-tool chain so user has a
        # concrete next action, not a generic «уточни» bounce.
        q_text = plan.clarify_question or ""
        self.assertIn("book_readability", q_text)
        self.assertIn("top_books_by_downloads", q_text)

    def test_top_nouns_era_classifies_not_clarifies(self):
        """«топ существительных эпохи» — was clarify → LLM 50s
        parse-fail. Now: routed to period_vocab (then smart-clarify
        in the builder because period is too vague to default)."""
        q = "топ существительных эпохи"
        intent = classify(q)
        self.assertEqual(intent.label, "period_vocab",
                          f"Expected period_vocab, got {intent.label!r} "
                          f"(pattern={intent.matched_pattern!r})")
        # Builder smart-clarifies — fast, NOT a 50s heavy default
        plan = build(intent.label, extract(q))
        self.assertTrue(plan.needs_clarify)
        self.assertTrue(plan.authoritative_clarify)

    def test_top_nouns_19c_executes(self):
        """«топ существительных XIX века» — specific period anchor
        present, plan executes with the year range."""
        q = "топ существительных XIX века"
        intent = classify(q)
        e = extract(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "period_vocab")
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(plan.steps[0].args["year_from"], 1800)
        self.assertEqual(plan.steps[0].args["year_to"], 1899)
        self.assertEqual(plan.steps[0].args["pos_filter"], ["NOUN"])


# ---------------------------------------------------------------------------
# W-12 acceptance — rise direction queries
# ---------------------------------------------------------------------------


class W12Acceptance(unittest.TestCase):

    def test_rise_to_end_of_19c_routes_to_appearing(self):
        """«какие слова стали чаще к концу XIX века» → word_timeline
        with `words_appearing_after`. Was: «нет такой функции» before
        W-12; now: ranked rise list with cutoff inferred from XIX
        века (1899)."""
        q = "какие слова стали чаще к концу XIX века"
        intent = classify(q)
        e = extract(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "word_timeline")
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools,
                       f"Expected rise tool; got plan={plan.explain}")
        self.assertNotIn("words_disappearing_after", tools)
        # Cutoff should be inferred from year_to=1899 (W-12 plan
        # builder uses year_to as cutoff when direction is rise).
        appear_step = next(s for s in plan.steps
                            if s.tool == "words_appearing_after")
        self.assertEqual(appear_step.args.get("year"), 1899)

    def test_disappear_phrasing_kept(self):
        """Negative — drop phrasing stays on the disappearing tool."""
        q = "слова, вышедшие из употребления после 1920"
        intent = classify(q)
        e = extract(q)
        plan = build(intent.label, e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_disappearing_after", tools)
        self.assertNotIn("words_appearing_after", tools)


if __name__ == "__main__":
    unittest.main(verbosity=2)
