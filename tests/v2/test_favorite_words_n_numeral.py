"""Sprint 20 — Stan 2026-05-19 prod log:

    2026-05-19 16:28:40  clarify  «сто любимых слов Дойла»
    → «Не уверен, что ты имеешь в виду…»

Two missing patterns combined into one drop:
  (a) author_vocab rule didn't include the «любимые слова» phrasing
      (only фирменные / характерные / signature / маркеры).
  (b) top_n parser only handled digits + a narrow list of Russian
      ordinal prefixes («первые N»). «сто» = 100 (Russian numeral word)
      and friends weren't recognized.

Real systemic fix is v3.2.0-alpha1 LLM planner — but until Stan flips
`WC_LLM_PLANNER=on` in prod, the rules-path is what handles his
queries. This test pins the surface fix so it doesn't regress later.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class FavoriteWordsIntent(unittest.TestCase):
    """«любимые слова» / «favorite words» / «go-to vocabulary» should
    route to author_vocab — they're synonyms of «фирменные слова»."""

    def test_stans_exact_query(self):
        q = "сто любимых слов Дойла"
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        self.assertEqual(m.label, "author_vocab")
        self.assertEqual(e.author_regex, "^Doyle,")
        self.assertEqual(e.top_n, 100)
        self.assertFalse(p.needs_clarify)

    def test_lyubimaya_lexika(self):
        m = int_mod.classify("любимая лексика Wodehouse")
        self.assertEqual(m.label, "author_vocab")

    def test_favorite_words_en(self):
        m = int_mod.classify("favorite words of Doyle")
        self.assertEqual(m.label, "author_vocab")

    def test_favourite_spelling(self):
        m = int_mod.classify("favourite phrases used by Christie")
        self.assertEqual(m.label, "author_vocab")

    def test_go_to_vocabulary(self):
        m = int_mod.classify("Doyle's go-to vocabulary")
        self.assertEqual(m.label, "author_vocab")


class TopNRussianNumeralWords(unittest.TestCase):

    def test_sto(self):
        e = ent_mod.extract("сто любимых слов Дойла")
        self.assertEqual(e.top_n, 100)

    def test_dvesti(self):
        e = ent_mod.extract("двести слов Шекспира")
        self.assertEqual(e.top_n, 200)

    def test_pyatdesyat(self):
        e = ent_mod.extract("пятьдесят характерных слов у По")
        self.assertEqual(e.top_n, 50)

    def test_tysyacha(self):
        e = ent_mod.extract("тысяча слов из Pride and Prejudice")
        self.assertEqual(e.top_n, 1000)

    def test_english_fifty(self):
        e = ent_mod.extract("fifty words by Wodehouse")
        self.assertEqual(e.top_n, 50)


class TopNDigitWithAdjective(unittest.TestCase):
    """`\\d+ <adj>? слов` pattern — covers «100 любимых слов»,
    «50 favorite words», «20 archaic words»."""

    def test_100_lyubimykh_slov(self):
        e = ent_mod.extract("100 любимых слов Дойла")
        self.assertEqual(e.top_n, 100)

    def test_50_favorite_words(self):
        e = ent_mod.extract("50 favorite words by Wodehouse")
        self.assertEqual(e.top_n, 50)

    def test_20_archaic_words(self):
        e = ent_mod.extract("20 archaic words")
        self.assertEqual(e.top_n, 20)

    def test_plain_digits_words_still_works(self):
        """Regression: the original «N слов» pattern without adjective
        keeps working (the inserted `(\\w+\\s+)?` is optional)."""
        e = ent_mod.extract("100 слов Шекспира")
        self.assertEqual(e.top_n, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
