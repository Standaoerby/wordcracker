"""Defensive tests for book-scoped tools — pg_id=None must invalid_args.

Pre-existing bug, visible in journalctl since v3.2.0-alpha4:
  «AttributeError: 'NoneType' object has no attribute 'upper'»
  at rag_tools.book_readability:1120 → pg = pg_id.upper()

Triggered when planner emits a step with pg_id=None — e.g. unresolved
find_book chain, or v4 LLM-planner schema gap, or render path injects
None into a scoped tool. v1 crashes; v2 wrapper now catches early.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.authors.affinity import compare_authors  # noqa


class BookReadabilityPgIdGuard(unittest.TestCase):
    def test_none_pg_id_fails_with_invalid_args(self):
        from scripts.v2.tools.books.readability import book_readability
        r = book_readability(pg_id=None)
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.error)
        self.assertEqual(r.error.type, "invalid_args")
        self.assertIn("pg_id", r.error.message)

    def test_empty_pg_id_fails(self):
        from scripts.v2.tools.books.readability import book_readability
        for bad in ("", " ", "   "):
            r = book_readability(pg_id=bad)
            self.assertFalse(r.ok)
            self.assertEqual(r.error.type, "invalid_args")


class BookArchaicWordsPgIdGuard(unittest.TestCase):
    def test_none_pg_id_fails(self):
        from scripts.v2.tools.books.readability import book_archaic_words
        r = book_archaic_words(pg_id=None)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


class AffinityByBookPgIdGuard(unittest.TestCase):
    def test_none_pg_id_fails(self):
        from scripts.v2.tools.books.affinity_book import affinity_by_book
        r = affinity_by_book(pg_id=None)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


class BookEmotionProfilePgIdGuard(unittest.TestCase):
    def test_none_pg_id_fails(self):
        from scripts.v2.tools.books.top_books import book_emotion_profile
        r = book_emotion_profile(pg_id=None)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


if __name__ == "__main__":
    unittest.main(verbosity=2)
