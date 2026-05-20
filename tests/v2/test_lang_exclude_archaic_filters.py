"""Sprint 20+ — lang_hint + exclude_archaic entity parsing & plan flow.

Closes Round 11 B4 («английская классика» returned non-English books)
and B9 («B2 без архаизмов» returned archaic-heavy Roman Stoicism).

The new Entities fields:
    lang_hint:        'en' | 'ru' | 'fr' | None   (regex over raw query)
    exclude_archaic:  bool                         (regex over raw query)

These are surfaced to the renderer as plan.render_notes when the
planner can't fully filter at tool level (the corpus doesn't yet
have a per-book archaic_density column; find_book_by_topic doesn't
accept a `lang` arg directly).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.plan import (
    QueryPlan,
    _plan_book_recommendation,
    _plan_topic_book_search,
)


class LangHintExtraction(unittest.TestCase):

    def test_english_literature_ru(self):
        e = extract("посоветуй английскую классику про любовь")
        self.assertEqual(e.lang_hint, "en")

    def test_english_literature_en(self):
        e = extract("recommend English literature about war")
        self.assertEqual(e.lang_hint, "en")

    def test_in_english(self):
        e = extract("book in English about Gothic horror")
        self.assertEqual(e.lang_hint, "en")

    def test_russian_classics(self):
        e = extract("русская классика 19 века")
        self.assertEqual(e.lang_hint, "ru")

    def test_french_literature(self):
        e = extract("french literature on romanticism")
        self.assertEqual(e.lang_hint, "fr")

    def test_no_lang_hint_default(self):
        e = extract("найди книгу про драконов")
        self.assertIsNone(e.lang_hint)

    def test_no_lang_hint_short_query(self):
        e = extract("Doyle")
        self.assertIsNone(e.lang_hint)


class ExcludeArchaicExtraction(unittest.TestCase):

    def test_bez_arxaizmov(self):
        e = extract("книга для B2 без архаизмов")
        self.assertTrue(e.exclude_archaic)

    def test_ne_arxaichno(self):
        e = extract("посоветуй не архаичный язык")
        self.assertTrue(e.exclude_archaic)

    def test_no_archaic_en(self):
        e = extract("books with no archaic language")
        self.assertTrue(e.exclude_archaic)

    def test_modern_english(self):
        e = extract("modern English novels")
        self.assertTrue(e.exclude_archaic)

    def test_sovremennyj_jazyk(self):
        e = extract("книги современным языком")
        self.assertTrue(e.exclude_archaic)

    def test_no_flag_default(self):
        e = extract("посоветуй книгу про любовь")
        self.assertFalse(e.exclude_archaic)


class BookRecommendationPlan(unittest.TestCase):

    def test_default_lang_en(self):
        e = extract("посоветуй книгу для B2")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.steps[0].args["lang"], "en")
        # No exclude_archaic note when flag is false
        self.assertEqual(plan.render_notes, [])

    def test_explicit_english(self):
        e = extract("recommend English novel for B1")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.steps[0].args["lang"], "en")

    def test_exclude_archaic_stamps_render_note(self):
        e = extract("посоветуй книгу для B2 без архаизмов")
        plan = _plan_book_recommendation(e)
        # Plan should still execute; the limitation surfaces via render note
        self.assertEqual(plan.steps[0].tool, "top_books_by_downloads")
        self.assertTrue(plan.render_notes, msg="expected render_notes for exclude_archaic")
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("архаизм", joined)
        self.assertIn("disclose", joined)

    def test_explain_flags_exclude_archaic(self):
        e = extract("modern English novel without archaic")
        plan = _plan_book_recommendation(e)
        self.assertIn("exclude_archaic", plan.explain)


class TopicBookSearchPlan(unittest.TestCase):

    def test_no_lang_hint_no_notes(self):
        e = extract("найди книгу про драконов")
        plan = _plan_topic_book_search(e)
        self.assertEqual(plan.render_notes, [])

    def test_english_lang_hint_adds_note(self):
        e = extract("найди английскую классику про викторианский Лондон")
        plan = _plan_topic_book_search(e)
        # Plan still routes to find_book_by_topic
        self.assertEqual(plan.steps[0].tool, "find_book_by_topic")
        # And surfaces a lang disclosure note for the renderer
        self.assertTrue(plan.render_notes,
                        msg=f"expected render_notes; entities.lang_hint={e.lang_hint}")
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("английскую", joined)

    def test_non_english_lang_hint_disclosure(self):
        e = extract("русская классика про любовь")
        plan = _plan_topic_book_search(e)
        # Note disclosing limited non-EN coverage
        self.assertTrue(plan.render_notes)
        self.assertIn("корпус", " ".join(plan.render_notes).lower())


class RenderNotesPropagation(unittest.TestCase):
    """Quick sanity: QueryPlan supports the new render_notes field."""

    def test_querytree_render_notes_default_empty(self):
        e = extract("test")
        plan = QueryPlan(intent="test", entities=e, steps=[])
        self.assertEqual(plan.render_notes, [])

    def test_querytree_render_notes_explicit(self):
        e = extract("test")
        plan = QueryPlan(intent="test", entities=e, steps=[],
                         render_notes=["note A", "note B"])
        self.assertEqual(plan.render_notes, ["note A", "note B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
