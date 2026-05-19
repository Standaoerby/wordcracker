"""Sprint 18 extra — closeable round-8/long-tail gaps before release.

  1) «в стиле X» ambiguous router (book vs author → plan disambiguates)
  2) «who wrote X» / «кто автор Дракулы» → book_lookup
  3) Multi-word timeline («timeline telephone+automobile+aeroplane»)
  4) book_pub_year scope-narrowing (no more eating «когда появилось»)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class SimilarToAmbiguousRouter(unittest.TestCase):
    """«в стиле X» — book or author? Intent label is similar_to;
    plan-builder dispatches to book_similar OR author_closest based on
    which entity slot the extractor filled."""

    def test_routes_to_book_when_book_resolved(self):
        e = ent_mod.extract("в стиле Pride and Prejudice")
        p = plan_mod.build("similar_to", e)
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")

    def test_routes_to_author_when_author_resolved(self):
        e = ent_mod.extract("в стиле Достоевского")
        p = plan_mod.build("similar_to", e)
        self.assertEqual(p.steps[0].tool, "author_influences")

    def test_intent_classifies_correctly(self):
        for q in [
            "в стиле Pride and Prejudice",
            "в стиле Достоевского",
            "in the style of Hemingway",
            "in the style of Hamlet",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "similar_to",
                                  msg=f"{q!r} → {m.label!r}")

    def test_explicit_book_similar_still_wins(self):
        """«книги похожие на X» — book_similar (146) still beats
        similar_to (130) on priority."""
        m = int_mod.classify("книги похожие на Pride and Prejudice")
        self.assertEqual(m.label, "book_similar")

    def test_author_closest_unaffected(self):
        m = int_mod.classify("кто похож на Doyle")
        self.assertEqual(m.label, "author_closest")

    def test_no_entity_clarifies(self):
        e = Entities()
        p = plan_mod.build("similar_to", e)
        self.assertTrue(p.needs_clarify)


class WhoWroteBibliographic(unittest.TestCase):
    """«who wrote Hamlet» / «who is the author of Dracula» / «кто
    написал Войну и мир» — bibliographic. Plan dispatches via
    find_book (book entity is enough)."""

    POSITIVE = [
        "who wrote Hamlet",
        "who wrote Pride and Prejudice",
        "who is the author of Dracula",
        "кто написал Войну и мир",
        "кто автор Дракулы",
    ]

    def test_all_classify(self):
        for q in self.POSITIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                # author_attribution intent — plan auto-redirects to book_lookup
                self.assertEqual(m.label, "author_attribution")

    def test_plan_dispatches_to_find_book(self):
        for q in self.POSITIVE:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                p = plan_mod.build("author_attribution", e)
                self.assertEqual(p.steps[0].tool, "find_book",
                    msg=f"{q!r} should chain via find_book, got {p.steps[0].tool!r}")

    def test_passage_attribution_unaffected(self):
        """«угадай автора отрывка» / «кто автор этого отрывка» — still
        clarify (need passage text)."""
        for q in ["угадай автора отрывка", "кто автор этого отрывка"]:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                p = plan_mod.build("author_attribution", e)
                self.assertTrue(p.needs_clarify,
                    msg=f"{q!r} should clarify for passage text")


class MultiWordTimeline(unittest.TestCase):
    """Round 8 C5 — «timeline telephone+automobile+aeroplane» now
    dispatches N parallel word_freq_timeline calls."""

    def test_plus_separated(self):
        m = int_mod.classify("timeline telephone+automobile+aeroplane")
        e = ent_mod.extract("timeline telephone+automobile+aeroplane")
        p = plan_mod.build(m.label, e)
        self.assertEqual(m.label, "word_timeline")
        self.assertEqual(len(p.steps), 3)
        words = [s.args.get("word") for s in p.steps]
        self.assertEqual(words, ["telephone", "automobile", "aeroplane"])

    def test_comma_separated(self):
        m = int_mod.classify("timeline telephone, automobile, aeroplane")
        e = ent_mod.extract("timeline telephone, automobile, aeroplane")
        p = plan_mod.build(m.label, e)
        self.assertEqual(len(p.steps), 3)

    def test_when_appeared_multi(self):
        m = int_mod.classify("когда появились telephone, automobile, aeroplane")
        # No longer eaten by book_pub_year — appears-rule + word_timeline
        self.assertEqual(m.label, "word_timeline")

    def test_caps_at_5(self):
        """If user lists 7 words, plan caps at 5."""
        e = Entities(word=None,
                     raw_misc={"raw_text":
                                 "timeline a+b+c+d+e+f+g"})
        p = plan_mod.build("word_timeline", e)
        self.assertLessEqual(len(p.steps), 5)

    def test_primary_required_secondaries_optional(self):
        e = ent_mod.extract("timeline telephone+automobile")
        p = plan_mod.build("word_timeline", e)
        self.assertEqual(len(p.steps), 2)
        self.assertFalse(p.steps[0].optional)
        self.assertTrue(p.steps[1].optional)

    def test_single_word_unchanged(self):
        """«когда появилось слово radio» — single word_freq_timeline."""
        m = int_mod.classify("когда появилось слово radio")
        self.assertEqual(m.label, "word_timeline")
        e = ent_mod.extract("когда появилось слово radio")
        p = plan_mod.build("word_timeline", e)
        self.assertEqual(len(p.steps), 1)


class BookPubYearScopeNarrowing(unittest.TestCase):
    """Sprint 18 — book_pub_year no longer eats «появилось» (too
    generic; конфликтовало с word_timeline). Only matches actual
    publication verbs: опубликован / издан / вышл / написан."""

    def test_word_origin_not_pub_year(self):
        m = int_mod.classify("когда появилось слово radio")
        self.assertNotEqual(m.label, "book_pub_year")

    def test_pub_year_still_matches_correct_phrasings(self):
        for q in [
            "когда была опубликована Война и мир",
            "когда был издан Pride and Prejudice",
            "когда вышла Анна Каренина",
            "год издания Dracula",
            "when was Dracula published",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_pub_year",
                                  msg=f"{q!r} → {m.label!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
