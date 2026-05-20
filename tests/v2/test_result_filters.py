"""Sprint 20+ — unified result hygiene layer.

Round 11 external Claude reported:
- B5 NaN-author #1 in top_authors_by tokens
- B7 ISO language codes (ang, gem-pro, ine-pro) mixed into related forms
- B10 Moby Dick edition duplicates
- B17 identical snippets across PG ids in word_contexts
- B18 PG775 = PG12163 same snippet in topic_book_search
- B19 «th» single-token leaked through corpus_artifacts (was len≥3)
- B21 Twain count 211 because of edition dups in metadata
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools._result_filters import (
    apply_filters,
    dedup_book_editions,
    dedup_by_key,
    drop_iso_language_codes,
    drop_null_authors,
    drop_short_consonant_clusters,
    looks_like_iso_code,
)


class DropNullAuthors(unittest.TestCase):

    def test_drops_none_nan_anonymous(self):
        rows = [
            {"author": "Doyle, Arthur Conan", "tokens": 1_000_000},
            {"author": None, "tokens": 204_876_514},  # B5 case
            {"author": "NaN", "tokens": 100_000_000},
            {"author": "", "tokens": 50_000_000},
            {"author": "Anonymous", "tokens": 9_000_000},
            {"author": "Twain, Mark", "tokens": 800_000},
        ]
        kept, dropped = drop_null_authors(rows)
        self.assertEqual(dropped, 4)
        self.assertEqual([r["author"] for r in kept],
                          ["Doyle, Arthur Conan", "Twain, Mark"])

    def test_passes_real_authors(self):
        rows = [{"author": "Тургенев, Иван Сергеевич", "tokens": 1}]
        kept, dropped = drop_null_authors(rows)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)


class DropIsoLanguageCodes(unittest.TestCase):

    def test_drops_explicit_codes(self):
        rows = [
            {"word": "sword"},     # real word
            {"word": "ang"},       # Old English ISO
            {"word": "gem-pro"},   # Proto-Germanic
            {"word": "ine-pro"},   # Proto-Indo-European
            {"word": "enm-wmi"},   # Middle English West Midlands
            {"word": "swerd"},     # real attested form
            {"word": "sweard"},    # real attested form
        ]
        kept, dropped = drop_iso_language_codes(rows)
        self.assertEqual(dropped, 4)
        words = [r["word"] for r in kept]
        self.assertIn("sword", words)
        self.assertIn("swerd", words)
        self.assertIn("sweard", words)
        self.assertNotIn("ang", words)
        self.assertNotIn("gem-pro", words)

    def test_short_real_words_kept(self):
        """3-char real words like «cat» have vowels — kept."""
        rows = [{"word": "cat"}, {"word": "dog"}, {"word": "and"}]
        kept, dropped = drop_iso_language_codes(rows)
        # «and» also a real word, not in ISO set
        self.assertEqual(dropped, 0)

    def test_looks_like_iso_code_helper(self):
        self.assertTrue(looks_like_iso_code("ang"))
        self.assertTrue(looks_like_iso_code("gem-pro"))
        self.assertTrue(looks_like_iso_code("la-vul"))
        self.assertFalse(looks_like_iso_code("sword"))
        self.assertFalse(looks_like_iso_code(""))


class DedupByKey(unittest.TestCase):

    def test_snippet_dedup(self):
        # B17 — multi-author word_contexts had identical snippets
        rows = [
            {"snippet": "the fog was thick", "pg_id": "PG345"},
            {"snippet": "the fog was thick", "pg_id": "PG501"},  # dup
            {"snippet": "fog rolled in slowly", "pg_id": "PG345"},
            {"snippet": "the fog was thick", "pg_id": "PG2852"},  # dup
        ]
        kept, dropped = dedup_by_key(rows, key="snippet")
        self.assertEqual(dropped, 2)
        self.assertEqual(len(kept), 2)

    def test_missing_key_preserved(self):
        rows = [{"foo": 1}, {"foo": 2}]
        kept, dropped = dedup_by_key(rows, key="snippet")
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 2)

    def test_custom_normalizer(self):
        rows = [
            {"id": "PG2701"}, {"id": "pg2701"},  # case difference
            {"id": "PG345"},
        ]
        import functools
        kept, dropped = dedup_by_key(rows, key="id",
                                        normalize=lambda v: str(v).upper())
        self.assertEqual(dropped, 1)


class DedupBookEditions(unittest.TestCase):

    def test_moby_dick_editions_collapse(self):
        # B10 — same book under two PG ids
        rows = [
            {"title": "Moby Dick", "author": "Melville, Herman",
              "id": "PG2701", "downloads": 28000},
            {"title": "Moby Dick; Or, The Whale", "author": "Melville, Herman",
              "id": "PG2489", "downloads": 12000},
            {"title": "Pride and Prejudice", "author": "Austen, Jane",
              "id": "PG1342", "downloads": 50000},
        ]
        kept, dropped = dedup_book_editions(rows)
        self.assertEqual(dropped, 1)
        titles = [r["title"] for r in kept]
        # Keeps the higher-downloads edition (Moby Dick = 28000 vs ;Or = 12000)
        self.assertIn("Moby Dick", titles)
        self.assertNotIn("Moby Dick; Or, The Whale", titles)

    def test_strip_leading_article(self):
        rows = [
            {"title": "The Adventures of Sherlock Holmes",
              "author": "Doyle, Arthur Conan", "id": "PG1661", "downloads": 100},
            {"title": "Adventures of Sherlock Holmes",
              "author": "Doyle, Arthur Conan", "id": "PG1662", "downloads": 50},
        ]
        kept, dropped = dedup_book_editions(rows)
        self.assertEqual(dropped, 1)
        # Higher downloads kept
        self.assertEqual(kept[0]["downloads"], 100)

    def test_different_authors_keep_both(self):
        """Same title, different authors → keep both (translations etc.)."""
        rows = [
            {"title": "Hamlet", "author": "Shakespeare, William",
              "id": "PG1524", "downloads": 5000},
            {"title": "Hamlet", "author": "Smith, Some",
              "id": "PG9999", "downloads": 10},
        ]
        kept, dropped = dedup_book_editions(rows)
        self.assertEqual(dropped, 0)


class DropShortConsonantClusters(unittest.TestCase):

    def test_th_dropped(self):
        # B19 — «th» single-token leaked through ≥3 filter
        rows = [
            {"word": "thick"},  # real word, has vowel
            {"word": "th"},      # OCR fragment, no vowel, 2 chars
            {"word": "pp"},      # another OCR fragment
            {"word": "fog"},     # real word
        ]
        kept, dropped = drop_short_consonant_clusters(rows)
        self.assertEqual(dropped, 2)
        words = [r["word"] for r in kept]
        self.assertIn("thick", words)
        self.assertIn("fog", words)
        self.assertNotIn("th", words)
        self.assertNotIn("pp", words)

    def test_vowelled_short_kept(self):
        """«he», «is», «an» — short but have vowels, kept."""
        rows = [{"word": "he"}, {"word": "is"}, {"word": "an"}]
        kept, dropped = drop_short_consonant_clusters(rows)
        self.assertEqual(dropped, 0)


class ApplyFiltersPipeline(unittest.TestCase):

    def test_chain_with_drop_counts(self):
        rows = [
            {"author": "Doyle, Arthur", "word": "blighter"},
            {"author": None, "word": "ang"},        # null author + ISO code
            {"author": "Wodehouse", "word": "th"},   # consonant cluster
            {"author": "Christie", "word": "stitch"},
        ]
        kept, drops = apply_filters([
            drop_null_authors,
            drop_iso_language_codes,
            drop_short_consonant_clusters,
        ], rows)
        # 1 dropped null, 0 ISO codes (Doyle row kept), 1 consonant
        # Actually: row 2 dropped by drop_null_authors → never reaches ISO check
        # Then row 3 dropped by short_consonant
        self.assertEqual(len(kept), 2)
        words = [r["word"] for r in kept]
        self.assertIn("blighter", words)
        self.assertIn("stitch", words)
        self.assertEqual(drops.get("drop_null_authors"), 1)
        self.assertEqual(drops.get("drop_short_consonant_clusters"), 1)

    def test_empty_input(self):
        kept, drops = apply_filters([drop_null_authors], [])
        self.assertEqual(kept, [])
        self.assertEqual(drops, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
