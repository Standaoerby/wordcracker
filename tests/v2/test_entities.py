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


class SurnameClarifyOnAmbiguous(unittest.TestCase):
    """B-R17-1 stage3.2 v2 (2026-05-21 — Stan UX correction): when a
    bare surname like «Wells» maps to multiple canonical authors in
    the corpus, planner must SURFACE the candidate list so the plan
    builder can ask the user which Wells they meant — instead of
    silently auto-resolving to the dominant one (H.G. Wells).

    The first attempt tried auto-pick-dominant; smoke test showed
    that still returned the aggregate card in some cases, and Stan
    explicitly asked for clarify-with-list behavior. This locks in
    the candidate-collection contract.
    """

    def setUp(self):
        # Reset prominence cache + alias key cache so the mock takes effect
        from scripts.v2 import entity_resolver as er
        from scripts.v2.planner import entities as e
        with er._prom_lock:
            er._prom_state["data"] = None
        e._AUTHOR_KEYS_SORTED = None

    def test_wells_surfaces_candidates_for_clarify(self):
        """Bare `wells` alias matches multiple canonicals → Entities
        gets a populated `author_clarify_candidates` list sorted by
        downloads desc. Planner keeps the bare regex; plan builder
        decides to clarify."""
        import pandas as pd
        import unittest.mock as mock
        fake_df = pd.DataFrame([
            {"author": "Wells, H. G.",   "downloads": 50000, "id": 1},
            {"author": "Wells, Basil",   "downloads": 0,     "id": 2},
            {"author": "Wells, Carolyn", "downloads": 0,     "id": 3},
        ])
        from scripts.v2.planner.entities import extract
        with mock.patch("scripts.rag_tools._metadata_df",
                         return_value=fake_df):
            ent = extract("какие книги у Wells")
        # Regex stays the bare alias — plan builder routes to clarify
        # based on author_clarify_candidates, not regex shape.
        self.assertEqual(ent.author_regex, "^Wells,")
        self.assertEqual(len(ent.author_clarify_candidates), 3)
        # Sorted by downloads desc
        names = [c["name"] for c in ent.author_clarify_candidates]
        self.assertEqual(names[0], "Wells, H. G.")
        # Each candidate has the expected shape
        for c in ent.author_clarify_candidates:
            self.assertIn("name", c)
            self.assertIn("downloads", c)
            self.assertIn("books", c)

    def test_already_specific_alias_no_clarify(self):
        """Hardy alias `^Hardy, Thomas` is specific — `_find_authors`
        returns it as-is, candidate collection skips (regex doesn't
        match `^Surname,$` pattern)."""
        import pandas as pd
        import unittest.mock as mock
        fake_df = pd.DataFrame([
            {"author": "Hardy, Thomas",  "downloads": 12000, "id": 1},
            {"author": "Hardy, E. D.",   "downloads": 0,     "id": 2},
        ])
        from scripts.v2.planner.entities import extract
        with mock.patch("scripts.rag_tools._metadata_df",
                         return_value=fake_df):
            ent = extract("стиль Hardy")
        self.assertEqual(ent.author_regex, "^Hardy, Thomas")
        self.assertEqual(ent.author_clarify_candidates, [])

    def test_single_canonical_no_clarify(self):
        """If only one canonical matches the surname (e.g. Wodehouse),
        no clarify needed even though the alias is bare-surname."""
        import pandas as pd
        import unittest.mock as mock
        fake_df = pd.DataFrame([
            {"author": "Wodehouse, P. G.", "downloads": 5000, "id": 1},
        ])
        from scripts.v2.planner.entities import extract
        with mock.patch("scripts.rag_tools._metadata_df",
                         return_value=fake_df):
            ent = extract("стиль Wodehouse")
        self.assertEqual(ent.author_clarify_candidates, [])

    def test_no_metadata_no_clarify(self):
        """No metadata loaded (CI / dev) — return empty candidates,
        don't crash. The bare regex still goes through to the tool,
        same fallback behavior as pre-stage3.2."""
        import unittest.mock as mock
        from scripts.v2.planner.entities import extract
        from scripts.v2 import entity_resolver as er
        with er._prom_lock:
            er._prom_state["data"] = {"by_surname": {}, "by_canonical": {}}
        with mock.patch.object(er, "get_prominence_index",
                                return_value={"by_surname": {},
                                              "by_canonical": {}}):
            ent = extract("какие книги у Wells")
        self.assertEqual(ent.author_clarify_candidates, [])

    def test_clarify_helper_failure_does_not_crash(self):
        """If candidate collector raises (defensive cover), extraction
        still returns clean Entities with empty candidates list."""
        import unittest.mock as mock
        from scripts.v2.planner import entities as e_mod
        with mock.patch.object(e_mod, "_collect_surname_candidates",
                                side_effect=RuntimeError("boom")):
            # Direct call so the exception propagates — verify our wrapper
            # path catches it. Currently the wrapper is `_collect_*` itself;
            # we only need to ensure no extract() consumer breaks.
            ent = e_mod.extract("какие книги у Wells")
        # Defensive — empty list is the safe fallback
        self.assertEqual(ent.author_clarify_candidates, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
