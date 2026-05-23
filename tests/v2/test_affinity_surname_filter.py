"""Sprint 19+ — surname blocklist on affinity outputs.

Stan 2026-05-19 screenshot: Conan Doyle's «фирменные слова» list was
дominated by character surnames (challenger 122 / knolles 119 /
barrymore 112 / holmes 107 / flannigan 105 / stapleton 58 / mcfarlane
56 / baumgarten 54), all of which bypass the existing defences:

  1. corpus-diff heuristic (`corpus_count - author_count >= max(10,
     author_count*0.5)`) — these surnames appear in multiple authors'
     books so corpus_count is non-trivial; they pass.
  2. spaCy PROPN drop on isolated lowercase strings — unreliable for
     ambiguous tokens, returns NOUN for challenger/holmes/burger.
  3. word_dict proper_noun flag — only populated for words seen in
     prior learning_words runs.

The new layer is a positive-signal surname filter using:
  - PG author surnames from metadata.csv (mtime-cached, server-only)
  - curated literary character set in `_surname_filter.py`

This test covers the filter primitives and the affinity wrapper
integration. Real CSV is mocked via tempfile fixtures.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class SurnameBlocklistPrimitives(unittest.TestCase):

    def test_curated_set_contains_known_doyle_characters(self):
        from scripts.v2.tools.authors._surname_filter import (
            _CURATED_CHARACTER_SURNAMES,
        )
        # Stan's screenshot listed challenger / knolles / barrymore /
        # holmes / stapleton / mcfarlane / baumgarten as the worst leaks.
        for name in ("challenger", "knolles", "barrymore", "holmes",
                     "stapleton", "mcfarlane", "baumgarten"):
            self.assertIn(name, _CURATED_CHARACTER_SURNAMES, name)

    def test_curated_set_is_lowercase(self):
        from scripts.v2.tools.authors._surname_filter import (
            _CURATED_CHARACTER_SURNAMES,
        )
        for w in _CURATED_CHARACTER_SURNAMES:
            self.assertEqual(w, w.lower(), w)

    def test_pg_metadata_surnames_extracted(self):
        from scripts.v2.tools.authors._surname_filter import (
            _pg_author_surnames,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".csv", delete=False,
        ) as tmp:
            tmp.write("id,author,language\n")
            tmp.write("PG1,\"Doyle, Arthur Conan\",['en']\n")
            tmp.write("PG2,\"Tolkien, J. R. R.\",['en']\n")
            # multi-token surname — also splits
            tmp.write("PG3,\"de la Mare, Walter\",['en']\n")
            tmp.write("PG4,\"\",['en']\n")  # blank skipped
            p = Path(tmp.name)
        try:
            surnames = _pg_author_surnames(p)
            self.assertIn("doyle", surnames)
            self.assertIn("tolkien", surnames)
            # multi-token: both parts ≥3 chars retained
            self.assertIn("mare", surnames)
            self.assertNotIn("", surnames)
        finally:
            p.unlink(missing_ok=True)

    def test_missing_metadata_returns_empty(self):
        from scripts.v2.tools.authors._surname_filter import (
            _pg_author_surnames,
        )
        s = _pg_author_surnames(Path("/no/such/file.csv"))
        self.assertEqual(s, frozenset())

    def test_filter_surnames_drops_known_characters(self):
        from scripts.v2.tools.authors._surname_filter import filter_surnames
        rows = [
            {"word": "challenger", "author_count": 385, "corpus_count": 2245,
             "affinity": 122.21},
            {"word": "knolles", "author_count": 92, "corpus_count": 553,
             "affinity": 118.56},
            {"word": "barrymore", "author_count": 159, "corpus_count": 1008,
             "affinity": 112.41},
            {"word": "holmes", "author_count": 4045, "corpus_count": 27051,
             "affinity": 106.56},
            # uitlanders — Boer-War term, NOT a surname → keep
            {"word": "uitlanders", "author_count": 71, "corpus_count": 889,
             "affinity": 56.92},
        ]
        kept, dropped = filter_surnames(rows)
        self.assertEqual(dropped, 4)
        self.assertEqual([r["word"] for r in kept], ["uitlanders"])

    def test_filter_surnames_empty_input(self):
        from scripts.v2.tools.authors._surname_filter import filter_surnames
        kept, dropped = filter_surnames([])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 0)


class AffinityByAuthorIntegration(unittest.TestCase):

    def _doyle_v1_output(self):
        """Mimic v1 affinity_by_author output as it landed in Stan's
        screenshot — top of list is character surnames."""
        return {
            "author_regex": "^Doyle, Arthur Conan",
            "slug": "doyle_arthur_conan",
            "pos_filter": None,
            "effective_min_corpus_count": 0,
            "total_unique_words": 12000,
            "top": [
                {"word": "challenger", "author_count": 385,
                 "corpus_count": 2245, "affinity": 122.21},
                {"word": "knolles",    "author_count": 92,
                 "corpus_count": 553,  "affinity": 118.56},
                {"word": "barrymore",  "author_count": 159,
                 "corpus_count": 1008, "affinity": 112.41},
                {"word": "holmes",     "author_count": 4045,
                 "corpus_count": 27051,"affinity": 106.56},
                {"word": "flannigan",  "author_count": 74,
                 "corpus_count": 503,  "affinity": 104.84},
                {"word": "blighter",   "author_count": 65,
                 "corpus_count": 1430, "affinity":  38.50},  # real word
                # Phase 3 W-4 — was labeled «real (Boer)» but it's a Boer-
                # war ethnic-toponym derivation («foreign workers in Boer
                # republics»). Now correctly dropped by toponym filter.
                {"word": "uitlanders", "author_count": 71,
                 "corpus_count": 889,  "affinity":  56.92},  # toponym → drop
            ],
            "cached": True,
            "proper_noun_filter": "corpus-diff heuristic dropped 0, "
                                   "spaCy PROPN dropped 0",
        }

    def test_surnames_filtered_from_top_list(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                          return_value=self._doyle_v1_output()):
            r = affinity_by_author("^Doyle, Arthur Conan", top=10)
        self.assertTrue(r.ok)
        top = r.data["top"]
        words = [t["word"] for t in top]
        # Surnames must be gone
        for surname in ("challenger", "knolles", "barrymore", "holmes",
                          "flannigan"):
            self.assertNotIn(surname, words, surname)
        # Real signature word must remain
        self.assertIn("blighter", words)
        # Phase 3 W-4 acceptance — uitlanders is a Boer-war toponym
        # derivation and MUST be filtered out.
        self.assertNotIn("uitlanders", words,
                          msg="W-4: Boer-war toponym must be dropped")
        # And the filter note is propagated
        self.assertIn("surname", r.data["proper_noun_filter"])
        self.assertIn("toponym", r.data["proper_noun_filter"])

    def test_self_name_still_dropped(self):
        """Original Sprint-2 defence still works alongside the new filter."""
        from scripts.v2.tools.authors.affinity import affinity_by_author
        v1 = {
            "author_regex": "^Wilde, Oscar",
            "slug": "wilde_oscar",
            "pos_filter": None,
            "top": [
                {"word": "wilde", "author_count": 100, "corpus_count": 200,
                 "affinity": 50.0},
                {"word": "blighter", "author_count": 65, "corpus_count": 1430,
                 "affinity": 38.50},
            ],
            "cached": True,
        }
        with mock.patch("scripts.rag_tools.affinity_by_author",
                          return_value=v1):
            r = affinity_by_author("^Wilde, Oscar", top=10)
        words = [t["word"] for t in r.data["top"]]
        self.assertNotIn("wilde", words)
        self.assertIn("blighter", words)


class AffinityByBookIntegration(unittest.TestCase):

    def test_surnames_filtered_from_book_top(self):
        # Phase 2 — V1AffinityByBook canonical: `top` (not `top_words`).
        from scripts.v2.tools.books.affinity_book import affinity_by_book
        v1 = {
            "pg_id": "PG2852",  # Hound of the Baskervilles
            "title": "The Hound of the Baskervilles",
            "top": [
                {"word": "barrymore",  "book_count": 35, "corpus_count": 1008,
                 "affinity": 200.0},
                {"word": "stapleton",  "book_count": 42, "corpus_count": 2554,
                 "affinity": 180.0},
                {"word": "moor",       "book_count": 30, "corpus_count": 15000,
                 "affinity":  18.0},  # real noun
            ],
        }
        with mock.patch("scripts.learning_tools.affinity_by_book",
                          return_value=v1):
            r = affinity_by_book("PG2852", top=10)
        self.assertTrue(r.ok)
        words = [t["word"] for t in r.data["top"]]
        self.assertNotIn("barrymore", words)
        self.assertNotIn("stapleton", words)
        self.assertIn("moor", words)
        self.assertIn("surname", r.data.get("_render_note", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
