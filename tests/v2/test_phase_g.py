"""Sprint 16 Phase G — long-tail polish tests.

Two surfaces:
  1) book_pub_year intent + plan — «когда была опубликована Война и мир»
  2) RU genitive / prepositional book title aliases in KNOWN_BOOKS"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import KNOWN_BOOKS, Entities


class BookPubYearIntent(unittest.TestCase):

    def test_russian_kogda_byla_opublikovana(self):
        m = int_mod.classify("Когда была опубликована Война и мир?")
        self.assertEqual(m.label, "book_pub_year")

    def test_russian_god_izdaniya(self):
        m = int_mod.classify("Год издания Pride and Prejudice")
        self.assertEqual(m.label, "book_pub_year")

    def test_russian_v_kakom_godu_vyshla(self):
        m = int_mod.classify("В каком году вышла Война и мир?")
        self.assertEqual(m.label, "book_pub_year")

    def test_english_when_was_published(self):
        m = int_mod.classify("When was Dracula published?")
        self.assertEqual(m.label, "book_pub_year")

    def test_english_year_of_publication(self):
        m = int_mod.classify("year of publication for Frankenstein")
        self.assertEqual(m.label, "book_pub_year")

    def test_doesnt_steal_kogda_rodilsja(self):
        """«когда родился Doyle» stays in author_metadata."""
        m = int_mod.classify("когда родился Doyle")
        self.assertEqual(m.label, "author_metadata")


class BookPubYearPlan(unittest.TestCase):

    def test_routes_to_find_book_with_id(self):
        e = Entities(book_id="PG2600", book_title="War and Peace")
        p = plan_mod.build("book_pub_year", e)
        self.assertEqual(p.intent, "book_pub_year")
        self.assertEqual(p.steps[0].tool, "find_book")
        # First-priority arg is the title (more discriminating than id)
        self.assertEqual(p.steps[0].args["title"], "War and Peace")

    def test_routes_to_find_book_with_title_only(self):
        e = Entities(book_title="Dracula")
        p = plan_mod.build("book_pub_year", e)
        self.assertEqual(p.steps[0].tool, "find_book")
        self.assertEqual(p.steps[0].args["title"], "Dracula")

    def test_clarifies_when_no_book(self):
        e = Entities()
        p = plan_mod.build("book_pub_year", e)
        self.assertTrue(p.needs_clarify)


class RussianGenitiveTitles(unittest.TestCase):
    """Round 6 R5: «слова в Войне и мире» went to clarify because the
    title was in the prepositional case. KNOWN_BOOKS now includes the
    case variants of the popular Russian titles."""

    def test_voiny_i_mira_genitive_resolves(self):
        """«стиль Войны и мира» — genitive form maps to PG2600."""
        self.assertIn("войны и мира", KNOWN_BOOKS)
        pg, canonical = KNOWN_BOOKS["войны и мира"]
        self.assertEqual(pg, "PG2600")
        self.assertEqual(canonical, "War and Peace")

    def test_voine_i_mire_prepositional_resolves(self):
        """«слова в Войне и мире» — prepositional form."""
        self.assertIn("войне и мире", KNOWN_BOOKS)
        pg, canonical = KNOWN_BOOKS["войне и мире"]
        self.assertEqual(pg, "PG2600")

    def test_extract_finds_genitive_form(self):
        """End-to-end: entity extractor picks up RU genitive titles."""
        e = ent_mod.extract("какие слова есть только в Войне и мире")
        self.assertEqual(e.book_id, "PG2600")
        self.assertEqual(e.book_title, "War and Peace")

    def test_extract_finds_gen_form_crime_punishment(self):
        e = ent_mod.extract("стиль Преступления и наказания")
        self.assertEqual(e.book_id, "PG2554")

    def test_extract_finds_prep_form_pride_prejudice(self):
        e = ent_mod.extract("словарный профиль в Гордости и предубеждении")
        self.assertEqual(e.book_id, "PG1342")

    def test_extract_finds_anna_karenina_genitive(self):
        e = ent_mod.extract("персонажи Анны Карениной")
        self.assertEqual(e.book_id, "PG1399")

    def test_extract_finds_dracula_genitive(self):
        e = ent_mod.extract("монолог Дракулы")
        self.assertEqual(e.book_id, "PG345")

    def test_nominative_still_works(self):
        """Confirm we didn't accidentally break the nominative entries."""
        for nom_key in ("война и мир", "преступление и наказание",
                         "гордость и предубеждение", "дракула"):
            self.assertIn(nom_key, KNOWN_BOOKS,
                          msg=f"Nominative {nom_key!r} fell out of KNOWN_BOOKS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
