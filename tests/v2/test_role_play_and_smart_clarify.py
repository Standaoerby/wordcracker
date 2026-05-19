"""Sprint 19+ — role-play preamble stripper + smart clarify recipe.

Stan 2026-05-19 showed two failing patterns:

  Q1: «я готический сомелье, готовлю меню на ночь чтения. начнём с
       аперитива — какие архаизмы у Walpole в Castle of Otranto?»
       → clarify (entity extractors confused by 100+ char role-play prefix)
  Q2: «кто из английских авторов XIX века самый темный по эмоциональной
       палитре, у кого больше всего fear?»
       → clarify «не уверен» despite extracting country=GB, period=
       1800-1899, emotion=fear

Fixes:
  1. _strip_roleplay_preamble removes «я <role>...» up to em-dash/period
     so entity extractors see the substantive trailing question.
  2. Walpole/Radcliffe/Maturin aliases + Castle of Otranto / Mysteries
     of Udolpho / The Monk in KNOWN_BOOKS.
  3. «английск» added to COUNTRY_ALIASES (was missing — only «english»
     was, which doesn't substring-match «английских»).
  4. _smart_clarify_recipe inspects entities; if ≥2 fields populated,
     surfaces a concrete 3-step recipe instead of «не уверен».
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


class RolePlayPreambleStrip(unittest.TestCase):

    def test_stan_gothic_sommelier_query(self):
        q = ('я готический сомелье, готовлю меню на ночь чтения. '
             'начнём с аперитива — какие архаизмы у Walpole в Castle of Otranto?')
        e = ent_mod.extract(q)
        # Both Walpole + Castle of Otranto resolved despite the preamble
        self.assertEqual(e.author_regex, "^Walpole, Horace")
        self.assertEqual(e.book_id, "PG696")

    def test_classify_still_sees_archaic_trigger(self):
        q = ('я готический сомелье — какие архаизмы у Walpole в '
             'Castle of Otranto?')
        m = int_mod.classify(q)
        self.assertEqual(m.label, "book_archaic")

    def test_short_preamble_just_role(self):
        e = ent_mod.extract("я учитель литературы — фирменные слова Walpole?")
        self.assertEqual(e.author_regex, "^Walpole, Horace")

    def test_predstav_chto(self):
        e = ent_mod.extract(
            "представь, что я редактор — какие архаизмы в Castle of Otranto?")
        self.assertEqual(e.book_id, "PG696")

    def test_no_preamble_unaffected(self):
        e = ent_mod.extract("архаизмы у Walpole в Castle of Otranto")
        self.assertEqual(e.author_regex, "^Walpole, Horace")
        self.assertEqual(e.book_id, "PG696")

    def test_strip_helper_returns_question_only(self):
        from scripts.v2.planner.entities import _strip_roleplay_preamble
        q = ("я доктор, занимаюсь исследованием классики XIX века — "
             "что значит слово gloomy?")
        stripped = _strip_roleplay_preamble(q)
        # Substantive question survives, preamble gone
        self.assertIn("gloomy", stripped)
        self.assertNotIn("доктор", stripped)
        self.assertLess(len(stripped), len(q))


class GothicCanonAliases(unittest.TestCase):

    CASES = [
        ("Walpole",            "^Walpole, Horace"),
        ("Хорас Уолпол",       "^Walpole, Horace"),
        ("Уолпола",            "^Walpole, Horace"),
        ("Radcliffe",          "^Radcliffe, Ann"),
        ("Анна Радклифф",      "^Radcliffe, Ann"),
        ("Maturin",            "^Maturin,"),
        ("Матюрин",            "^Maturin,"),
        ("Matthew Lewis",      "^Lewis, M"),
    ]

    def test_each_resolves(self):
        for label, expected in self.CASES:
            with self.subTest(author=label):
                e = ent_mod.extract(f"фирменные слова у {label}")
                self.assertEqual(e.author_regex, expected)

    def test_gothic_books_resolve(self):
        for q, expected_pg in [
            ("архаизмы в Castle of Otranto", "PG696"),
            ("слова в Mysteries of Udolpho", "PG3268"),
            ("персонажи The Monk",           "PG601"),
        ]:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                self.assertEqual(e.book_id, expected_pg)


class CountryAliasEnglishAdjective(unittest.TestCase):
    """Sprint 19+: «английских авторов» now extracts country=GB."""

    def test_anglijskih_avtorov(self):
        e = ent_mod.extract("топ английских авторов")
        self.assertEqual(e.country, "GB")

    def test_anglijskij_xix_veka(self):
        e = ent_mod.extract("кто из английских авторов XIX века...")
        self.assertEqual(e.country, "GB")


class SmartClarifyRecipe(unittest.TestCase):
    """Compound research queries get a 3-step recipe instead of generic
    «не уверен»."""

    def test_stan_q2_compound_emotion_country_period(self):
        q = ("кто из английских авторов XIX века самый темный по "
             "эмоциональной палитре, у кого больше всего fear?")
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        # Should NOT use generic fallback message
        self.assertTrue(p.needs_clarify)
        self.assertNotIn("фирменные слова Wodehouse", p.clarify_question or "")
        # Should mention extracted entities
        self.assertIn("GB", p.clarify_question)
        self.assertIn("fear", p.clarify_question.lower())
        self.assertIn("1800-1899", p.clarify_question)

    def test_generic_fallback_for_sparse_entities(self):
        """Empty entities → generic clarify message (unchanged)."""
        e = Entities(raw_misc={"raw_text": "asdf qwerty"})
        p = plan_mod.build("unknown_intent", e)
        self.assertIn("Wodehouse", p.clarify_question)


if __name__ == "__main__":
    unittest.main(verbosity=2)
