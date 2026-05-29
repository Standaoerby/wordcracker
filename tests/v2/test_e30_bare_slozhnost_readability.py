"""E30 (S-R4, 2026-05-29) — bare «сложность <BOOK>» routes to
book_readability.

Stan Q7: «сложность Франкенштейна» (no «уровень», no «для чтения») fell
to clarify because the single-book readability pattern required
«уровень сложн*» / «насколько сложн*» / «сложн* для чтения». The noun
«сложность <ProperNoun>» now routes to book_readability.

R5 compliance: positive («сложность Франкенштейна» → book_readability)
AND negative («сложность викторианской прозы» — abstract/period, no
proper-noun book — must NOT route to book_readability). The negative is
the query that motivated the capital-letter guard: a naive [A-ZА-Я]
under the globally-applied IGNORECASE would have matched the lowercase
«в» of «викторианской» and false-positived.

Regression-lock: the pre-existing readability routes («уровень сложности
X», compare) must keep their labels.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.intent import classify


class E30BareSlozhnostRoutesToReadability(unittest.TestCase):

    def test_slozhnost_frankenstein(self):
        self.assertEqual(classify("сложность Франкенштейна").label,
                         "book_readability")

    def test_slozhnost_quoted_frankenstein(self):
        self.assertEqual(classify('сложность "Франкенштейна"').label,
                         "book_readability")

    def test_slozhnost_latin_title(self):
        self.assertEqual(classify("сложность Pride and Prejudice").label,
                         "book_readability")

    def test_slozhnosti_genitive_variant(self):
        # «сложности Дракулы» — the -и genitive form also routes.
        self.assertEqual(classify("сложности Дракулы").label,
                         "book_readability")


class E30NegativeAbstractDoesNotRoute(unittest.TestCase):
    """NOT-X → NOT-Y: «сложность <lowercase abstract>» must NOT route to
    book_readability — there's no book to measure. This is exactly the
    query that justified the (?-i:[A-ZА-ЯЁ]) capital-letter guard."""

    def test_slozhnost_victorian_prose_not_readability(self):
        self.assertNotEqual(
            classify("сложность викторианской прозы").label,
            "book_readability",
            msg="abstract/period 'сложность' must not hit book_readability",
        )

    def test_slozhnost_yazyka_not_readability(self):
        self.assertNotEqual(
            classify("сложность языка").label,
            "book_readability",
        )

    def test_slozhnost_gothic_genre_not_readability(self):
        self.assertNotEqual(
            classify("сложность готического жанра").label,
            "book_readability",
        )

    def test_cefr_level_after_slozhnosti_not_readability(self):
        """Regression-lock: «сложности B2 при чтении Лавкрафта» is a
        learning query (CEFR level), NOT a book. «B2» is capital-then-digit;
        the trailing-letter guard keeps the rule off it. This is the query
        that broke test_plan::test_learning_b2_lovecraft on the first cut."""
        self.assertNotEqual(
            classify("Какие слова сложности B2 при чтении Лавкрафта?").label,
            "book_readability",
        )


class E30RegressionLockExistingRoutes(unittest.TestCase):
    """The new pattern must not steal the established readability routes."""

    def test_uroven_slozhnosti_still_single(self):
        self.assertEqual(
            classify("уровень сложности Pride and Prejudice").label,
            "book_readability",
        )

    def test_compare_still_wins(self):
        self.assertEqual(
            classify('что сложнее для чтения — "Дракула" или "Франкенштейн"').label,
            "book_readability_compare",
        )

    def test_compare_chitat_still_wins(self):
        self.assertEqual(
            classify("что сложнее читать, Дракула или Франкенштейн").label,
            "book_readability_compare",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
