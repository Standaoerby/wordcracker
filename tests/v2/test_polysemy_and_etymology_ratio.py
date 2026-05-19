"""Sprint 19+ — polysemy scope fix + Beowulf/Paradise Lost/Milton +
etymology-ratio smart clarify.

Stan 2026-05-19, two screenshots back-to-back:

  Q1: «polysemy для слова set»
      → v1 word_pos_distribution rejected the bare 'all_corpus' string
        (line 1577: «bad scope; use {'book':PGid} | {'author':regex}»).
        Plan now widens to {'author': '.*'} when no scope is set.

  Q2: «germanic vs latinate ratio в Beowulf и Paradise Lost»
      → Beowulf + Paradise Lost weren't in KNOWN_BOOKS, Milton not in
        AUTHOR_ALIASES_CURATED. Also «german» substring matched inside
        «germanic» and tagged the query as country=DE.
        Added books + Milton + word-bounded country alias match +
        a new smart-clarify branch describing the per-book recipe.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class PolysemyScopeFix(unittest.TestCase):
    """word_pos with no book/author no longer passes the bare string
    'all_corpus' to v1 — widens to {'author': '.*'} which v1 accepts."""

    def test_polysemy_set_dispatches_global_author_scope(self):
        m = int_mod.classify("polysemy для слова set")
        e = ent_mod.extract("polysemy для слова set")
        p = plan_mod.build(m.label, e)
        self.assertEqual(m.label, "word_pos")
        self.assertFalse(p.needs_clarify)
        self.assertEqual(p.steps[0].tool, "word_pos_distribution")
        scope = p.steps[0].args["scope"]
        # The bug was scope being the string 'all_corpus' — v1 rejects it.
        self.assertIsInstance(scope, dict)
        self.assertEqual(scope, {"author": ".*"})
        self.assertEqual(p.steps[0].args["word"], "set")

    def test_polysemy_with_explicit_book_uses_book_scope(self):
        e = ent_mod.extract("polysemy для слова light в Pride and Prejudice")
        p = plan_mod.build("word_pos", e)
        self.assertEqual(p.steps[0].args["scope"], {"book": "PG1342"})

    def test_polysemy_with_explicit_author_uses_author_scope(self):
        e = ent_mod.extract("polysemy для слова light у Wodehouse")
        p = plan_mod.build("word_pos", e)
        self.assertIn("author", p.steps[0].args["scope"])


class BeowulfParadiseLostMiltonAliases(unittest.TestCase):

    def test_beowulf_resolves(self):
        e = ent_mod.extract("уровень сложности Beowulf")
        self.assertEqual(e.book_id, "PG16328")
        self.assertEqual(e.book_title, "Beowulf")

    def test_paradise_lost_resolves(self):
        e = ent_mod.extract("эмоциональный профиль Paradise Lost")
        self.assertEqual(e.book_id, "PG26")
        self.assertEqual(e.book_title, "Paradise Lost")

    def test_milton_resolves(self):
        e = ent_mod.extract("фирменные слова у Milton")
        self.assertEqual(e.author_regex, "^Milton, John")

    def test_milton_russian(self):
        e = ent_mod.extract("стиль у Мильтона")
        self.assertEqual(e.author_regex, "^Milton, John")

    def test_potyeranny_rai_russian(self):
        e = ent_mod.extract("ключевые темы Потерянного рая")
        self.assertEqual(e.book_id, "PG26")


class CountryAliasNoEtymologyFalsePositive(unittest.TestCase):
    """`german` was substring-matching inside `germanic` (etymology) and
    tagging the query as country=DE. Now Latin-script aliases require
    word boundaries; Cyrillic stems still substring-match for
    declensional coverage."""

    def test_germanic_word_does_not_set_country(self):
        e = ent_mod.extract("germanic vs latinate ratio в Beowulf и Paradise Lost")
        self.assertIsNone(e.country)

    def test_german_authors_still_detected(self):
        e = ent_mod.extract("german authors of the 19th century")
        self.assertEqual(e.country, "DE")

    def test_russian_stem_still_works(self):
        e = ent_mod.extract("немецкая литература XIX века")
        self.assertEqual(e.country, "DE")

    def test_english_word_bounded(self):
        # «english» shouldn't trip on «englishman» or similar — but it
        # SHOULD on a bare «english authors» query.
        e = ent_mod.extract("english authors of victorian era")
        self.assertEqual(e.country, "GB")


class EtymologyRatioSmartClarify(unittest.TestCase):

    QUERY = "germanic vs latinate ratio в Beowulf и Paradise Lost"

    def test_books_both_resolved(self):
        e = ent_mod.extract(self.QUERY)
        # One book as primary, other in multi
        all_ids = sorted([e.book_id] + (e.multi_book_ids or []))
        self.assertEqual(all_ids, ["PG16328", "PG26"])

    def test_etymology_family_extracted(self):
        e = ent_mod.extract(self.QUERY)
        self.assertEqual(e.etymology_family, "germanic")

    def test_smart_clarify_recipe_fires(self):
        e = ent_mod.extract(self.QUERY)
        p = plan_mod.build("clarify", e)
        self.assertTrue(p.needs_clarify)
        msg = p.clarify_question or ""
        self.assertIn("Etymology-ratio", msg)
        self.assertIn("find_words_by_etymology", msg)
        # Both books mentioned by id
        self.assertIn("PG16328", msg)
        self.assertIn("PG26", msg)
        # Both families mentioned
        self.assertIn("germanic", msg)
        self.assertIn("latin", msg)
        # Does NOT fall through to bare «не уверен»
        self.assertNotIn("Не уверен, что ты имеешь", msg)

    def test_single_book_no_recipe(self):
        """One book + etymology shouldn't trigger ratio recipe."""
        e = ent_mod.extract("германские слова в Beowulf")
        p = plan_mod.build("clarify", e)
        msg = p.clarify_question or ""
        self.assertNotIn("Etymology-ratio", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
