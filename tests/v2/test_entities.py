"""Unit tests for entity extraction.

Covers author/book/word/year/country/level/emotion/etymology/pos_filter
extraction on the 40-example list plus a few targeted probes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract


class AuthorExtraction(unittest.TestCase):
    def test_doyle_ru(self):
        e = extract("Какие слова у Конан Дойла встречаются чаще?")
        self.assertEqual(e.author_regex, "^Doyle,")

    def test_doyle_short_form(self):
        e = extract("кто похож на Doyle")
        self.assertEqual(e.author_regex, "^Doyle,")

    def test_dickens_hemingway_multi(self):
        e = extract("Найди слова, которые повторяются у Диккенса, но не у Хемингуэя.")
        self.assertEqual(e.author_regex, "^Dickens,")
        self.assertIn("^Hemingway,", e.multi_author_regex)

    def test_poe_lovecraft_multi(self):
        e = extract("Какие слова отличают стиль По от стиля Лавкрафта?")
        self.assertIsNotNone(e.author_regex)
        self.assertEqual(len(e.multi_author_regex) + 1, 2)
        regs = {e.author_regex, *e.multi_author_regex}
        self.assertEqual(regs, {"^Poe,", "^Lovecraft,"})

    def test_no_author_when_none(self):
        e = extract("сколько книг в корпусе")
        self.assertIsNone(e.author_regex)

    def test_multi_three_authors(self):
        e = extract("у морских авторов — Мелвилла, Конрада и Стивенсона")
        regs = {e.author_regex, *e.multi_author_regex}
        self.assertEqual(regs, {"^Melville,", "^Conrad,", "^Stevenson,"})


class BookExtraction(unittest.TestCase):
    def test_quoted_known_book(self):
        e = extract('Слова в книге "Преступление и наказание" чаще обычного')
        self.assertEqual(e.book_id, "PG2554")

    def test_typographic_quotes(self):
        e = extract("Какие слова из «Dracula» считаются устаревшими?")
        self.assertEqual(e.book_id, "PG345")

    def test_quoted_unknown(self):
        e = extract('Слова в книге «1984»')
        # 1984 isn't in SPGC (copyright). title_query is set, pg_id is None.
        self.assertIsNone(e.book_id)
        self.assertEqual(e.book_title, "1984")

    def test_explicit_pg_id(self):
        e = extract("affinity for PG1342 please")
        self.assertEqual(e.book_id, "PG1342")


class WordExtraction(unittest.TestCase):
    def test_quoted_word(self):
        self.assertEqual(extract('слово "fog"').word, "fog")

    def test_word_typographic(self):
        self.assertEqual(extract('слово "ajar"').word, "ajar")

    def test_no_word(self):
        self.assertIsNone(extract("какая статистика по Wodehouse").word)

    def test_name_after_imya(self):
        """Bug B: «имени Анна / имя Anna» → e.word should capture the name."""
        self.assertEqual(
            extract("приведи примеры использования имени Анна в английской классике").word,
            "анна",
        )
        self.assertEqual(extract("примеры имени Anna").word, "anna")
        self.assertEqual(extract("под именем John в романе").word, "john")


class YearExtraction(unittest.TestCase):
    def test_after_year(self):
        e = extract("слова вышли из употребления после 1920 года")
        self.assertEqual(e.year_from, 1921)
        self.assertIsNone(e.year_to)

    def test_before_year(self):
        e = extract("публикации до 1900 года")
        self.assertIsNone(e.year_from)
        self.assertEqual(e.year_to, 1899)

    def test_range(self):
        e = extract("книги 1850–1920 годов")
        self.assertEqual((e.year_from, e.year_to), (1850, 1920))

    def test_victorian_keyword(self):
        e = extract("викторианские авторы")
        self.assertEqual((e.year_from, e.year_to), (1837, 1901))


class CountryExtraction(unittest.TestCase):
    def test_british(self):
        self.assertEqual(extract("британские слова").country, "GB")

    def test_american(self):
        self.assertEqual(extract("в американской литературе").country, "US")

    def test_no_country(self):
        self.assertIsNone(extract("сколько книг").country)


class LevelExtraction(unittest.TestCase):
    def test_b2(self):
        self.assertEqual(extract("читатель B2").level, "intermediate")

    def test_c1(self):
        self.assertEqual(extract("уровень C1").level, "advanced")

    def test_keyword_intermediate(self):
        self.assertEqual(extract("intermediate vocabulary").level, "intermediate")


class EmotionExtraction(unittest.TestCase):
    def test_fear(self):
        self.assertEqual(extract("слова страха у По").emotion, "fear")

    def test_terror(self):
        self.assertEqual(extract("words like terror and madness").emotion, "fear")


class EtymologyExtraction(unittest.TestCase):
    def test_germanic(self):
        self.assertEqual(extract("слова германского происхождения").etymology_family, "germanic")

    def test_norse(self):
        self.assertEqual(extract("скандинавские слова").etymology_family, "norse")


class POSExtraction(unittest.TestCase):
    def test_adj(self):
        self.assertEqual(extract("характерные прилагательные Уайльда").pos_filter, ["ADJ"])

    def test_verb_movement(self):
        # "глаголы движения" → expect VERB
        self.assertIn("VERB", extract("самые необычные глаголы движения").pos_filter or [])


class TopNExtraction(unittest.TestCase):
    def test_top_300(self):
        self.assertEqual(extract("первые 300 слов для изучения").top_n, 300)

    def test_top_dash_15(self):
        self.assertEqual(extract("топ-15").top_n, 15)


class EmptyInput(unittest.TestCase):
    def test_empty(self):
        e = extract("")
        self.assertIsNone(e.author_regex)
        self.assertIsNone(e.book_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
