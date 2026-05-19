"""Sprint 19+ — four typed clarify failures from Stan 2026-05-19.

Stan showed a screenshot with 4 distinct clarify drops, each needing
a different fix:

  Q1: «покажи свою схему: planner, router, renderer, critic — что
       делает каждый»
       → introduction (architecture variant)
  Q2: «метаданные книги PG1342»
       → book_lookup → find_book
  Q3: «распределение Flesch Reading Ease по корпусу — среднее,
       медиана, p10, p90»
       → smart clarify recipe (corpus-wide aggregation, no single tool)
  Q4: «Burrows Delta между Dickens и Trollope: кто ближе к Eliot»
       → smart clarify recipe (triangulation, multi-author compare)

Bonus: added Trollope + George Eliot to AUTHOR_ALIASES_CURATED — both
Victorian novelists, frequently asked-about cohort.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class ArchitectureQuestionIntroIntent(unittest.TestCase):

    def test_pokazi_svoju_skhemu(self):
        m = int_mod.classify(
            "покажи свою схему: planner, router, renderer, critic — "
            "что делает каждый")
        self.assertEqual(m.label, "introduction")

    def test_show_architecture_en(self):
        for q in [
            "show me your architecture",
            "show your pipeline",
            "что делает planner",
            "архитектура?",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "introduction")


class MetadataKnigiBookLookup(unittest.TestCase):

    def test_metadannye_knigi_pg(self):
        q = "метаданные книги PG1342"
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        self.assertEqual(m.label, "book_lookup")
        self.assertEqual(e.book_id, "PG1342")
        self.assertEqual(p.steps[0].tool, "find_book")

    def test_metadata_en(self):
        m = int_mod.classify("metadata for the book PG1342")
        self.assertEqual(m.label, "book_lookup")

    def test_infa_o_pg(self):
        m = int_mod.classify("что ты знаешь о PG1342")
        self.assertEqual(m.label, "book_lookup")


class CorpusWideDistributionSmartClarify(unittest.TestCase):

    def test_flesch_distribution_routes_to_recipe(self):
        q = ("распределение Flesch Reading Ease по корпусу — "
             "среднее, медиана, p10, p90")
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        # intent matches book_readability (via «Flesch» trigger), but
        # plan recognizes no-book + corpus-wide aggregation → clarify
        self.assertTrue(p.needs_clarify)
        # Recipe mentions concrete next steps
        msg = p.clarify_question or ""
        self.assertIn("Sample", msg)
        self.assertIn("book_readability", msg)
        # Doesn't fall to bare «нужна книга» message
        self.assertNotIn("Уточни название книги", msg)

    def test_single_book_readability_unaffected(self):
        """Sanity: single book readability still works."""
        e = ent_mod.extract("уровень сложности Pride and Prejudice")
        p = plan_mod.build("book_readability", e)
        self.assertFalse(p.needs_clarify)
        self.assertEqual(p.steps[-1].tool, "book_readability")


class BurrowsDeltaTriangulation(unittest.TestCase):

    QUERY = ("Burrows Delta между Dickens и Trollope: "
             "кто ближе к Eliot")

    def test_three_authors_extracted(self):
        e = ent_mod.extract(self.QUERY)
        # Dickens is primary, Trollope + Eliot in multi
        self.assertEqual(e.author_regex, "^Dickens,")
        self.assertIn("^Trollope, Anthony", e.multi_author_regex)
        self.assertIn("^Eliot, George", e.multi_author_regex)

    def test_routes_to_triangulation_recipe(self):
        e = ent_mod.extract(self.QUERY)
        p = plan_mod.build("clarify", e)
        self.assertTrue(p.needs_clarify)
        msg = p.clarify_question or ""
        self.assertIn("Triangulation", msg)
        self.assertIn("compare_authors", msg)
        self.assertIn("author_closest", msg)


class VictorianNovelistAliases(unittest.TestCase):
    """Sprint 19+ — Trollope + Eliot added to curated aliases."""

    def test_trollope_resolves(self):
        e = ent_mod.extract("фирменные слова у Trollope")
        self.assertEqual(e.author_regex, "^Trollope, Anthony")

    def test_eliot_resolves(self):
        e = ent_mod.extract("фирменные слова у Eliot")
        self.assertEqual(e.author_regex, "^Eliot, George")

    def test_russian_genitive(self):
        e = ent_mod.extract("стиль у Троллопа")
        self.assertEqual(e.author_regex, "^Trollope, Anthony")


if __name__ == "__main__":
    unittest.main(verbosity=2)
