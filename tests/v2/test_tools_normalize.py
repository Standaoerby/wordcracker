"""Unit tests for `scripts.v2.tools._normalize` helpers — Type 2 of T2.

Each helper has positive and negative cases. They replace fallback chains
across `.get` calls that the Phase 2 gate forbids (REFACTOR_BRIEF Phase 2,
REMEDIATION_BRIEF T2).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools._normalize import (
    match_id,
    scope_book_id,
    search_snippet,
)


class TestScopeBookId(unittest.TestCase):
    def test_returns_book_key_when_present(self):
        self.assertEqual(scope_book_id({"book": "PG1342"}), "PG1342")

    def test_returns_pg_id_alias_when_book_missing(self):
        self.assertEqual(scope_book_id({"pg_id": "PG345"}), "PG345")

    def test_prefers_book_over_pg_id_when_both_present(self):
        self.assertEqual(
            scope_book_id({"book": "PG1342", "pg_id": "PG345"}),
            "PG1342",
        )

    def test_returns_none_for_author_scope(self):
        self.assertIsNone(scope_book_id({"author": "^Doyle,"}))

    def test_returns_none_for_empty_dict(self):
        self.assertIsNone(scope_book_id({}))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(scope_book_id(None))
        self.assertIsNone(scope_book_id("all_corpus"))
        self.assertIsNone(scope_book_id(42))


class TestMatchId(unittest.TestCase):
    def test_returns_pg_id_when_present(self):
        self.assertEqual(match_id({"pg_id": "PG1342"}), "PG1342")

    def test_returns_id_alias_when_pg_id_missing(self):
        self.assertEqual(match_id({"id": "PG345"}), "PG345")

    def test_prefers_pg_id_over_id_when_both_present(self):
        self.assertEqual(match_id({"pg_id": "PG1342", "id": "PG345"}), "PG1342")

    def test_returns_none_for_empty_dict(self):
        self.assertIsNone(match_id({}))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(match_id(None))


class TestSearchSnippet(unittest.TestCase):
    def test_prefers_lex_snippet(self):
        lex = {"snippet": "BM25 highlighted"}
        sem = {"text": "semantic full", "snippet": "semantic trimmed"}
        self.assertEqual(search_snippet(lex, sem), "BM25 highlighted")

    def test_falls_back_to_sem_text(self):
        lex = {"snippet": ""}  # empty → falsy → fallback
        sem = {"text": "semantic full"}
        self.assertEqual(search_snippet(lex, sem), "semantic full")

    def test_falls_back_to_sem_snippet_last(self):
        lex = {}
        sem = {"snippet": "semantic trimmed"}
        self.assertEqual(search_snippet(lex, sem), "semantic trimmed")

    def test_returns_none_when_all_empty(self):
        self.assertIsNone(search_snippet({}, {}))
        self.assertIsNone(search_snippet(None, None))

    def test_handles_one_side_missing(self):
        self.assertEqual(search_snippet(None, {"text": "x"}), "x")
        self.assertEqual(search_snippet({"snippet": "y"}, None), "y")


if __name__ == "__main__":
    unittest.main(verbosity=2)
