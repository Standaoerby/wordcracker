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

    def test_name_after_imya_filler_negative(self):
        """Regression: the proper-noun guard (capital first letter on the
        captured word, no re.IGNORECASE) must reject filler phrasing — we
        only want real names. Caught during the v2.2 regression audit."""
        self.assertIsNone(extract("имя автора").word)
        self.assertIsNone(extract("от моего имени напиши письмо").word)
        self.assertIsNone(extract("по имени никого нет").word)


class Q10CyrillicScopePhrase(unittest.TestCase):
    """Q10 «лексику "второго уровня" из "Pride and Prejudice"» — the Russian
    quoted scope keyword used to win over the English book title. Skip
    Cyrillic-only quoted phrases shorter than 25 chars unless they're in
    KNOWN_BOOKS."""

    def test_q10_picks_english_title(self):
        e = extract('Покажи мне лексику "второго уровня" из "Pride and Prejudice" — не базовые слова')
        self.assertEqual(e.book_id, "PG1342")
        self.assertEqual(e.book_title, "Pride and Prejudice")

    def test_russian_known_title_still_works(self):
        e = extract('Покажи слова из "Преступление и наказание"')
        self.assertEqual(e.book_id, "PG2554")

    def test_long_russian_title_passes_through(self):
        """A long Russian-only phrase (≥25 chars) still goes through as
        title — it's plausibly a real book name we don't have in
        KNOWN_BOOKS yet, let find_book try to resolve it."""
        e = extract('из "какого-то очень длинного русского названия книги"')
        self.assertIsNotNone(e.book_title)


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


class SurnameSpecialization(unittest.TestCase):
    """B-R17-1 stage3.2 (2026-05-21): when AUTHOR_ALIASES maps a bare
    surname to `^Surname,` and multiple canonical authors share that
    surname, `_find_authors` should specialize via the resolver's
    prominence index to the dominant canonical's tighter regex.

    Without this, queries like «какие книги у Wells» extract
    `author_regex=^Wells,` and downstream author_metadata returns the
    aggregate of all Wells authors (1820-?, 10 books) instead of
    H.G. Wells specifically.
    """

    def setUp(self):
        # Reset prominence cache + alias key cache so the mock takes effect
        from scripts.v2 import entity_resolver as er
        from scripts.v2.planner import entities as e
        with er._prom_lock:
            er._prom_state["data"] = None
        e._AUTHOR_KEYS_SORTED = None

    def test_wells_specializes_to_dominant_canonical(self):
        """Bare-surname alias `wells → ^Wells,` should tighten to
        `^Wells, H` when H.G. Wells dominates by ≥5× downloads."""
        import pandas as pd
        import unittest.mock as mock
        fake_df = pd.DataFrame([
            {"author": "Wells, H. G.",   "downloads": 50000, "id": 1},
            {"author": "Wells, Basil",   "downloads": 0,     "id": 2},
            {"author": "Wells, Carolyn", "downloads": 0,     "id": 3},
        ])
        from scripts.v2.planner.entities import _find_authors
        with mock.patch("scripts.rag_tools._metadata_df",
                         return_value=fake_df):
            hits = _find_authors("какие книги у Wells")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "^Wells, H",
                          "Expected specialization to dominant Wells")

    def test_already_specific_alias_unchanged(self):
        """Hardy alias already specific (`^Hardy, Thomas`) — must not
        be re-specialized (would break disambiguation we already did)."""
        import pandas as pd
        import unittest.mock as mock
        fake_df = pd.DataFrame([
            {"author": "Hardy, Thomas",  "downloads": 12000, "id": 1},
            {"author": "Hardy, E. D.",   "downloads": 0,     "id": 2},
        ])
        from scripts.v2.planner.entities import _find_authors
        with mock.patch("scripts.rag_tools._metadata_df",
                         return_value=fake_df):
            hits = _find_authors("стиль Hardy")
        self.assertEqual(hits[0][1], "^Hardy, Thomas")

    def test_specialization_skips_when_metadata_unavailable(self):
        """No-metadata environment: planner returns the bare-surname
        regex as fallback. Critical for CI / dev boxes without SPGC."""
        import unittest.mock as mock
        from scripts.v2.planner.entities import _find_authors
        from scripts.v2 import entity_resolver as er
        # Force the prominence index to be empty
        with er._prom_lock:
            er._prom_state["data"] = {"by_surname": {}, "by_canonical": {}}
        with mock.patch.object(er, "_specialize_surname_to_dominant",
                                return_value=(None, None, {})):
            hits = _find_authors("какие книги у Wells")
        # No specialization → fallback to bare alias
        self.assertEqual(hits[0][1], "^Wells,")

    def test_specialization_failure_does_not_crash(self):
        """If resolver helper raises (defensive coverage), planner must
        still return the bare alias — entity extraction is never
        allowed to crash on this path."""
        import unittest.mock as mock
        from scripts.v2.planner.entities import _find_authors
        from scripts.v2 import entity_resolver as er
        with mock.patch.object(er, "_specialize_surname_to_dominant",
                                side_effect=RuntimeError("boom")):
            hits = _find_authors("какие книги у Wells")
        # Defensive — original alias preserved
        self.assertEqual(hits[0][1], "^Wells,")


if __name__ == "__main__":
    unittest.main(verbosity=2)
