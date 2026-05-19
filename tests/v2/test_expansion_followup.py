"""Sprint 19+ — expansion follow-up («покажи все», «полный список»).

Stan 2026-05-19: after «у тебя есть Harry Potter?» found 7 books (top
5 shown), said «покажи все книги серии» → fell to clarify. Lost the
HP context entirely.

Fix:
  1. _REF_TRIGGERS + _EXPAND_PATTERNS recognize the expansion phrase
  2. infer_followup_intent re-classifies the prior user message →
     inherits book_lookup
  3. merge_with_history bumps top_n=30 so find_book returns the
     wider set (7 of 7 instead of top 5 of 7)
  4. _plan_book_lookup respects e.top_n and passes through to find_book
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import history as hist_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities, extract


class ExpansionRecognition(unittest.TestCase):

    def test_is_expansion_recognizes_phrases(self):
        for q in [
            "покажи все",
            "покажи все книги",
            "покажи все книги серии",
            "полный список",
            "все книги серии",
            "все произведения",
            "список всех",
            "show all",
            "show me all",
            "list all",
            "give me the full list",
        ]:
            with self.subTest(query=q):
                self.assertTrue(hist_mod.is_expansion_followup(q),
                                 msg=f"expected is_expansion=True for {q!r}")

    def test_is_expansion_rejects_normal_queries(self):
        for q in [
            "у тебя есть Harry Potter?",
            "архаизмы в Dracula",
            "фирменные слова Wodehouse",
            "теперь у Doyle",  # context_swap, not expansion
            "ещё примеры",     # ref_trigger, not expansion
        ]:
            with self.subTest(query=q):
                self.assertFalse(hist_mod.is_expansion_followup(q),
                                  msg=f"expected is_expansion=False for {q!r}")


class StanExactSequence(unittest.TestCase):
    """The verbatim sequence from Stan's 2026-05-19 screenshot:
       T1: «у тебя есть Harry Potter?» → book_lookup top=5 (found 7)
       T2: «покажи все книги серии»     → was clarify, must be book_lookup top=30."""

    HISTORY = [
        {"role": "user",
         "content": "у тебя есть Harry Potter?"},
        {"role": "assistant",
         "content": "Да, у меня есть книги «Harry Potter». Ниже первые 5..."},
    ]

    def _classify_with_followup(self, query, history):
        m = int_mod.classify(query)
        if m.label == "clarify":
            inferred = hist_mod.infer_followup_intent(query, history)
            if inferred:
                from scripts.v2.planner.intent import IntentMatch
                m = IntentMatch(label=inferred, confidence=0.75,
                                 matched_pattern="followup-inferred")
        return m

    def test_pokazi_vse_knigi_inherits_book_lookup(self):
        m = self._classify_with_followup("покажи все книги серии",
                                           self.HISTORY)
        self.assertEqual(m.label, "book_lookup")

    def test_top_n_bumped_to_30(self):
        e = extract("покажи все книги серии")
        e = hist_mod.merge_with_history(e, self.HISTORY,
                                          "покажи все книги серии")
        self.assertEqual(e.top_n, 30)

    def test_book_title_backfilled_from_history(self):
        e = extract("покажи все книги серии")
        e = hist_mod.merge_with_history(e, self.HISTORY,
                                          "покажи все книги серии")
        # Prior turn had «Harry Potter» → backfilled
        self.assertEqual(e.book_title, "Harry Potter")

    def test_plan_dispatches_find_book_with_top_30(self):
        e = extract("покажи все книги серии")
        e = hist_mod.merge_with_history(e, self.HISTORY,
                                          "покажи все книги серии")
        p = plan_mod.build("book_lookup", e)
        self.assertFalse(p.needs_clarify)
        self.assertEqual(p.steps[0].tool, "find_book")
        self.assertEqual(p.steps[0].args.get("top"), 30)


class ExpansionAfterDifferentPriorIntents(unittest.TestCase):
    """The expansion logic should inherit ANY prior non-clarify
    intent, not just book_lookup."""

    def _seq(self, prior_query):
        return [
            {"role": "user", "content": prior_query},
            {"role": "assistant", "content": "..."},
        ]

    def test_inherits_top_authors_books(self):
        m = int_mod.classify("покажи все")
        if m.label == "clarify":
            inferred = hist_mod.infer_followup_intent(
                "покажи все", self._seq("топ-5 авторов по числу книг"))
            self.assertEqual(inferred, "top_authors_books")

    def test_inherits_author_metadata(self):
        # If prior was something like "сколько у Doyle книг"
        history = self._seq("сколько у Doyle книг")
        inferred = hist_mod.infer_followup_intent("полный список", history)
        self.assertIn(inferred, ("author_metadata", "author_lookup"))


class TopNUnchangedForNonExpansion(unittest.TestCase):
    """Sanity — non-expansion queries don't get top_n bumped."""

    def test_regular_query_no_bump(self):
        e = extract("у тебя есть Harry Potter?")
        history = [{"role": "user", "content": "что-то другое"},
                   {"role": "assistant", "content": "..."}]
        e = hist_mod.merge_with_history(e, history,
                                          "у тебя есть Harry Potter?")
        self.assertIsNone(e.top_n)

    def test_book_lookup_default_top_is_5(self):
        """When top_n is None, plan uses default 5 (legacy behaviour)."""
        e = extract("у тебя есть Dracula?")
        p = plan_mod.build("book_lookup", e)
        self.assertEqual(p.steps[0].args.get("top"), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
