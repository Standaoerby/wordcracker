"""E44 (2026-05-22) — «что почитать, если нравится X» routes to similar_to,
not generic book_recommendation.

ROOT CAUSE (Stan prod 2026-05-22 evening):
Query «что почитать, если нравится Толстой?» matched
`book_recommendation` → `_plan_book_recommendation` → dispatches
`top_books_by_downloads({top: 20, lang: 'en'})` WITHOUT any author
filter or relevance context. Returned generic popularity list (Gatsby,
Blue Castle, JFK Commission report, Pride & Prejudice…) — zero
relation to Tolstoy.

Two issues:
  1. Intent classifier missed «если нравится X» / «if you like X»
     phrasings — existing Sprint 17 rule only caught «после|подобн|
     похож|типа» variants.
  2. `_plan_book_recommendation` ignores resolved entity entirely
     even when author is present (separate structural issue, fix
     deferred — for «если нравится» we now bypass via similar_to).

FIX (E44): new intent rule catches all «taste» / «fan» phrasings and
routes them to `similar_to` (NOT `book_similar`) because entity may be
AUTHOR (Tolstoy → Burrows Delta neighbours) OR BOOK (Pride & Prejudice
→ thematic neighbours). `_plan_similar_to` (plan.py:1683-1707)
dispatches:
  - book_id/title set → _plan_book_similar (find_book_by_topic)
  - author_regex set → _plan_author_closest (author_influences /
    Burrows Delta)
  - neither resolved → clarify with both-options hint
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class IfYouLikeIntentRouting(unittest.TestCase):
    """Intent classifier must route taste-phrasings to similar_to."""

    def test_stan_exact_prod_query(self):
        from scripts.v2.planner.intent import classify
        m = classify("что почитать, если нравится Толстой?")
        self.assertEqual(m.label, "similar_to",
                          f"Stan's exact prod query — got {m.label}")

    def test_reverse_order_esli_nravitsya_first(self):
        from scripts.v2.planner.intent import classify
        m = classify("если нравится Тургенев, что почитать")
        self.assertEqual(m.label, "similar_to")

    def test_with_book_title(self):
        from scripts.v2.planner.intent import classify
        m = classify("если нравится Pride and Prejudice, что почитать")
        self.assertEqual(m.label, "similar_to")

    def test_english_if_you_like(self):
        from scripts.v2.planner.intent import classify
        m = classify("if you like Tolstoy what to read")
        self.assertEqual(m.label, "similar_to")

    def test_lyublyu_chto_posovetuesh(self):
        from scripts.v2.planner.intent import classify
        m = classify("я люблю Достоевского, что посоветуешь")
        self.assertEqual(m.label, "similar_to")

    def test_existing_posle_pattern_unchanged(self):
        """Existing «что почитать после X» must STILL route to book_similar."""
        from scripts.v2.planner.intent import classify
        m = classify("что почитать после Crime and Punishment")
        self.assertEqual(m.label, "book_similar")

    def test_cefr_level_unchanged(self):
        """«что почитать на B2» still routes to book_recommendation."""
        from scripts.v2.planner.intent import classify
        m = classify("что почитать на B2 уровне")
        self.assertEqual(m.label, "book_recommendation")


class EndToEndPlanForToldoyTaste(unittest.TestCase):
    """Verify full pipeline: classify → extract → plan.build →
    correct tool dispatched."""

    def test_tolstoy_taste_dispatches_author_closest(self):
        from scripts.v2.planner.intent import classify
        from scripts.v2.planner.entities import extract
        from scripts.v2.planner import plan as p

        q = "что почитать, если нравится Толстой?"
        m = classify(q)
        e = extract(q)
        plan_obj = p.build(m.label, e)

        self.assertEqual(plan_obj.intent, "author_closest",
                          "Tolstoy taste must dispatch via _plan_similar_to "
                          "→ _plan_author_closest")
        self.assertFalse(plan_obj.needs_clarify,
                          f"must not clarify; reason: "
                          f"{plan_obj.clarify_question!r}")
        tool_names = [s.tool for s in plan_obj.steps]
        self.assertIn("author_influences", tool_names,
                       "must dispatch author_influences for Burrows Delta "
                       "neighbours")
        # Author resolution must succeed
        self.assertEqual(e.author_regex, "^Tolstoy,")


if __name__ == "__main__":
    unittest.main(verbosity=2)
