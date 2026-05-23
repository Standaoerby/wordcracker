"""W-11 (Phase 5 P2 polish, 2026-05-23) — over-eager clarify / parse-fail
fixes for thematic + ranking queries.

Before:
  «какие существительные чаще у викторианцев»   → smart-clarify 60s (LLM fallback)
  «какие книги XIX века самые сложные»          → generic parse-fail 54s
  «слова викторианцев 1837-1901»                → parse-fail

After (acceptance from tz):
  · «существительные/прилагательные/глаголы» triggers period_vocab when
    paired with a period marker (viktorian / edwardian / explicit year
    range) regardless of order.
  · year-range «1837-1901» with «слова/words» also triggers period_vocab.
  · plural-difficulty book ranking («самые сложные книги <period>»)
    routes to `book_extremum`, where the builder ships a fast recipe-
    clarify instead of LLM fallback.

R5 (CLAUDE.md): every regex/builder change ships with the motivating
queries as named cases.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.intent import classify
from scripts.v2.planner.entities import extract
from scripts.v2.planner.builders.book import _plan_book_extremum
from scripts.v2.planner.builders.composite import _plan_period_vocab


PERIOD_VOCAB_PROBES: tuple[str, ...] = (
    # The three queries called out in the W-11 acceptance section.
    "какие существительные чаще у викторианцев",
    "какие книги XIX века самые сложные",  # this one bucket → book_extremum
    "слова викторианцев 1837-1901",
    # Adjacent phrasings that previously slipped through.
    "самые частые прилагательные у викторианцев",
    "топ глаголов в эдвардианскую эпоху",
    "слова викторианского периода",
    "vocab of the Victorian era",
    "nouns 1837-1901",
    "слова 1880-1900",
)


class W11PeriodVocab(unittest.TestCase):

    def test_period_vocab_intent_for_thematic_probes(self):
        """Each thematic probe except the plural-book-ranking case
        classifies to period_vocab."""
        for q in PERIOD_VOCAB_PROBES:
            if "книг" in q or "book" in q.lower() or "роман" in q:
                continue  # those belong to the book_extremum case below
            with self.subTest(query=q):
                m = classify(q)
                self.assertEqual(
                    m.label, "period_vocab",
                    f"Expected period_vocab for {q!r}, got "
                    f"label={m.label!r} pattern={m.matched_pattern!r}",
                )

    def test_period_vocab_plan_uses_top_ngrams_with_period(self):
        """End-to-end: classify → extract → builder. The plan must call
        top_ngrams_by_author with year_from/year_to from the period
        signal (viktorian → 1837-1901, explicit «1837-1901», etc.)."""
        for q in ("какие существительные чаще у викторианцев",
                  "слова викторианцев 1837-1901"):
            with self.subTest(query=q):
                e = extract(q)
                self.assertEqual(e.year_from, 1837,
                                  f"year_from from {q!r}")
                self.assertEqual(e.year_to, 1901,
                                  f"year_to from {q!r}")
                plan = _plan_period_vocab(e)
                self.assertFalse(plan.needs_clarify, plan.explain)
                self.assertEqual(len(plan.steps), 1)
                step = plan.steps[0]
                self.assertEqual(step.tool, "top_ngrams_by_author")
                self.assertEqual(step.args["year_from"], 1837)
                self.assertEqual(step.args["year_to"], 1901)

    def test_period_vocab_noun_pos_filter_recognized(self):
        """POS filter from «существительные» propagates to plan args.
        Without this the plan would scan all POS and renderer couldn't
        honor the «существительные/nouns» part of the question."""
        e = extract("какие существительные чаще у викторианцев")
        self.assertIn("NOUN", (e.pos_filter or []))
        plan = _plan_period_vocab(e)
        self.assertEqual(plan.steps[0].args.get("pos_filter"), e.pos_filter)


class W11BookRanking(unittest.TestCase):

    def test_plural_difficulty_book_ranking_classifies_to_extremum(self):
        for q in ("какие книги XIX века самые сложные",
                  "какие самые простые романы у викторианцев",
                  "самые архаичные произведения 1800-1899",
                  "hardest books of the 1800s"):
            with self.subTest(query=q):
                m = classify(q)
                self.assertEqual(
                    m.label, "book_extremum",
                    f"Expected book_extremum for {q!r}, got "
                    f"label={m.label!r} pattern={m.matched_pattern!r}",
                )

    def test_book_extremum_plural_difficulty_builds_recipe_clarify(self):
        """The builder returns a needs_clarify=True plan with an
        actionable recipe (NOT an LLM-fallback or empty plan) and
        marks it authoritative so v4 LLM-planner doesn't override."""
        e = extract("какие книги XIX века самые сложные")
        plan = _plan_book_extremum(e)
        self.assertTrue(plan.needs_clarify, plan.explain)
        self.assertTrue(plan.authoritative_clarify,
                         "recipe-clarify must be authoritative so "
                         "v4 LLM-planner doesn't bypass it")
        # Recipe text mentions both top_books_by_downloads and
        # book_readability as the next-step tools — that's the contract
        # the user gets in <1s instead of a 54s parse-fail.
        question = plan.clarify_question or ""
        self.assertIn("book_readability", question)
        self.assertIn("top_books_by_downloads", question)

    def test_book_extremum_singular_popular_still_works(self):
        """Don't regress the singular «самая популярная книга» branch."""
        e = extract("самая популярная книга")
        plan = _plan_book_extremum(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].tool, "top_books_by_downloads")


if __name__ == "__main__":
    unittest.main(verbosity=2)
