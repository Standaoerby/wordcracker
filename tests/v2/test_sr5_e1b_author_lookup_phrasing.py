"""S-R5 E1b (2026-05-31) — bare-genitive author-catalog phrasing.

Diagnosis (closed against live prod):
  · «какие книги у Диккенса» → author_lookup, deterministic, 2-4s.
  · «книги Диккенса» (bare genitive, NO preposition «у») → fell to clarify
    (no rule matched) → v4 LLM-planner → ~16s flake.

Fix: a no-preposition author_lookup rule in intent.py guarded by the
(?-i:[A-ZА-ЯЁ][a-zа-яё]) proper-noun check (capital-THEN-lowercase). This
is our classify-time author-presence proxy — the same idiom used by the
E30 book_readability and book_emotion proper-noun guards. The guard makes
the bare form safe: it fires on a real surname but NOT on lowercase topic
phrasings, roman-numeral centuries, or CEFR levels.

These tests FAIL on the pre-E1b code (genitive → clarify) and PASS after.
The negatives FAIL if the guard is dropped (over-eager bare-noun match).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import intent as int_mod


class E1bAuthorLookupPhrasing(unittest.TestCase):

    # ---- positive: bare genitive surname now routes to author_lookup ----
    def test_knigi_dickensa(self):
        m = int_mod.classify("книги Диккенса")
        self.assertEqual(m.label, "author_lookup", m.matched_pattern)

    def test_proizvedenija_tolstogo(self):
        m = int_mod.classify("произведения Толстого")
        self.assertEqual(m.label, "author_lookup", m.matched_pattern)

    def test_english_bare_works_dickens(self):
        m = int_mod.classify("works Dickens")
        self.assertEqual(m.label, "author_lookup", m.matched_pattern)

    # ---- negative: topic phrasing must NOT become author_lookup ----
    # The preposition/topic word after «книги» is lowercase, so the
    # capital guard does not fire — topic_book_search stays intact.
    def test_knigi_pro_voinu_not_author_lookup(self):
        m = int_mod.classify("книги про войну")
        self.assertNotEqual(m.label, "author_lookup", m.matched_pattern)
        self.assertEqual(m.label, "topic_book_search", m.matched_pattern)

    def test_knigi_o_kosmose_not_author_lookup(self):
        m = int_mod.classify("книги о космосе")
        self.assertNotEqual(m.label, "author_lookup", m.matched_pattern)
        self.assertEqual(m.label, "topic_book_search", m.matched_pattern)

    # ---- regression: existing intents the guard must preserve ----
    def test_q30_recommendation_unbroken(self):
        """«произведения подойдут…» — lowercase verb, guard excludes it."""
        m = int_mod.classify(
            "Какие произведения подойдут для читателя уровня B2: "
            "не слишком простые, но без плотного слоя архаизмов?"
        )
        self.assertEqual(m.label, "book_recommendation", m.matched_pattern)

    def test_period_ranking_unbroken(self):
        """«книги XIX века…» — roman-numeral century is capital-then-
        UPPERCASE-I, excluded by the capital-then-lowercase guard."""
        m = int_mod.classify("какие книги XIX века самые сложные")
        self.assertEqual(m.label, "book_extremum", m.matched_pattern)

    def test_prepositional_form_still_author_lookup(self):
        """«какие книги у X» — the original prepositional rule still wins."""
        m = int_mod.classify("Какие книги у Doyle?")
        self.assertEqual(m.label, "author_lookup", m.matched_pattern)

    def test_skolko_knig_still_author_metadata(self):
        """«сколько у X книг» — book COUNT, not the catalog list."""
        m = int_mod.classify("сколько у Doyle книг")
        self.assertEqual(m.label, "author_metadata", m.matched_pattern)


if __name__ == "__main__":
    unittest.main()
