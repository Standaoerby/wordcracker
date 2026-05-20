"""Sprint 20+ B8 — book_similar routing improvements.

Stan Round 11: «что почитать после X» / «sequel to X» / «в продолжение X»
queries — previously fell to clarify or to find_book_by_topic with the
bare title (semantic word-cooc surfaces noisy hits). Now:
  - Intent rules pick these up explicitly (with negative-lookahead
    guards against book_recommendation framings: B2 level / no archaic).
  - Plan builds a richer topic «books with similar themes and style to X»
    and emits a render note telling the LLM to exclude the reference book.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import _plan_book_similar


class IntentRouting(unittest.TestCase):

    def test_what_to_read_next_after(self):
        m = classify("what to read after Crime and Punishment")
        self.assertEqual(m.label, "book_similar")

    def test_sequel_to(self):
        m = classify("sequel to Dracula")
        self.assertEqual(m.label, "book_similar")

    def test_in_continuation_ru(self):
        m = classify("в продолжение Властелина Колец")
        self.assertEqual(m.label, "book_similar")

    def test_after_reading_x(self):
        m = classify("after reading Pride and Prejudice")
        self.assertEqual(m.label, "book_similar")

    def test_what_to_read_next(self):
        m = classify("what to read next, Sherlock Holmes")
        self.assertEqual(m.label, "book_similar")


class IntentGuards(unittest.TestCase):
    """Make sure book_recommendation queries with «после» don't steal."""

    def test_b2_level_after_x_stays_recommendation(self):
        q = ('Какие произведения уровня B2 можно читать после "The Adventures '
             'of Sherlock Holmes", чтобы не было слишком много архаизмов?')
        m = classify(q)
        self.assertEqual(m.label, "book_recommendation")

    def test_what_to_read_after_b2_level_stays_recommendation(self):
        q = "what to read after Sherlock for B2 level"
        m = classify(q)
        # B2 level guard kicks in, our rule yields
        self.assertNotEqual(m.label, "book_similar")

    def test_chto_pochitat_posle_routes_to_similar(self):
        # Existing rule at ~line 834 — verify still works
        q = "что почитать после Преступления и наказания"
        m = classify(q)
        self.assertEqual(m.label, "book_similar")


class PlanEnrichment(unittest.TestCase):

    def test_topic_is_enriched(self):
        e = extract("книги похожие на Pride and Prejudice")
        # Mock the book_title since rules don't always extract it
        e.book_title = "Pride and Prejudice"
        plan = _plan_book_similar(e)
        topic = plan.steps[0].args["topic"]
        self.assertIn("similar themes", topic.lower())
        self.assertIn("Pride and Prejudice", topic)

    def test_uses_bge_reranker(self):
        e = extract("sequel to Dracula")
        e.book_title = "Dracula"
        plan = _plan_book_similar(e)
        self.assertEqual(plan.steps[0].args.get("rerank_with"), "bge_reranker")

    def test_render_note_excludes_reference(self):
        e = extract("книги похожие на Crime and Punishment")
        e.book_title = "Crime and Punishment"
        e.book_id = "PG2554"
        plan = _plan_book_similar(e)
        self.assertTrue(plan.render_notes)
        joined = " ".join(plan.render_notes)
        self.assertIn("EXCLUDE", joined)
        self.assertIn("PG2554", joined)

    def test_render_note_without_pg_id(self):
        e = extract("что почитать после Властелина Колец")
        e.book_title = "The Lord of the Rings"
        e.book_id = None
        plan = _plan_book_similar(e)
        self.assertTrue(plan.render_notes)
        joined = " ".join(plan.render_notes)
        self.assertIn("EXCLUDE", joined)
        self.assertIn("Lord of the Rings", joined)

    def test_no_book_falls_to_clarify(self):
        e = extract("книги похожие на")  # no resolvable book
        e.book_id = None
        e.book_title = None
        plan = _plan_book_similar(e)
        self.assertTrue(plan.needs_clarify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
