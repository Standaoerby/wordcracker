"""Tests for v2 profile cache store (author / book / lemma)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class ProfileStoreSQLite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "profiles.sqlite"
        # Point store at temp DB before importing it.
        import os
        os.environ["WC_V2_PROFILES_DB"] = str(self.db_path)
        # Reset any cached module connection.
        import importlib
        from scripts.v2.profiles import store
        importlib.reload(store)
        self.store = store
        # Pin corpus_version so freshness checks match.
        from scripts.v2 import corpus_version
        corpus_version._reset()

    def tearDown(self):
        # Close any open connection so the temp dir can be cleaned up.
        if self.store._conn is not None:
            self.store._conn.close()
            self.store._conn = None
        self.tmpdir.cleanup()

    def test_get_miss(self):
        self.assertIsNone(
            self.store.get("author_profile", "author_regex", "^Nobody,")
        )

    def test_put_then_get(self):
        payload = {"author": "Doyle", "books": 5}
        self.store.put("author_profile", "author_regex", "^Doyle,", payload)
        got = self.store.get("author_profile", "author_regex", "^Doyle,")
        self.assertIsNotNone(got)
        self.assertEqual(got["author"], "Doyle")
        self.assertEqual(got["books"], 5)
        # Cache metadata threaded through
        self.assertIn("_cached_at", got)
        self.assertIn("_corpus_version", got)

    def test_overwrite_existing(self):
        self.store.put("author_profile", "author_regex", "^Doyle,", {"books": 5})
        self.store.put("author_profile", "author_regex", "^Doyle,", {"books": 10})
        got = self.store.get("author_profile", "author_regex", "^Doyle,")
        self.assertEqual(got["books"], 10)

    def test_three_tables_independent(self):
        self.store.put("author_profile", "author_regex", "^Doyle,", {"a": 1})
        self.store.put("book_profile", "pg_id", "PG1342", {"b": 2})
        self.store.put("lemma_profile", "lemma", "ajar", {"c": 3})
        self.assertEqual(self.store.get("author_profile", "author_regex", "^Doyle,")["a"], 1)
        self.assertEqual(self.store.get("book_profile", "pg_id", "PG1342")["b"], 2)
        self.assertEqual(self.store.get("lemma_profile", "lemma", "ajar")["c"], 3)

    def test_stats(self):
        self.store.put("author_profile", "author_regex", "^A,", {})
        self.store.put("author_profile", "author_regex", "^B,", {})
        self.store.put("book_profile", "pg_id", "PG1", {})
        s = self.store.stats()
        self.assertEqual(s["author_profile"], 2)
        self.assertEqual(s["book_profile"], 1)
        self.assertEqual(s["lemma_profile"], 0)

    def test_stale_corpus_version_returns_none(self):
        # Put an entry, then bump corpus_version → get should miss.
        self.store.put("author_profile", "author_regex", "^Doyle,", {"books": 5})
        from scripts.v2 import corpus_version
        # Patch the cached value
        corpus_version._cache["v"] = corpus_version.CorpusVersion(
            timestamp="2099-01-01T00:00", books_total=99999,
            chunks_total=None, analytics_version="9.9.9",
            spgc_baseline="SPGC-2099", user_uploads=0, orphan_pg=0,
        )
        self.assertIsNone(
            self.store.get("author_profile", "author_regex", "^Doyle,")
        )

    def test_clear(self):
        self.store.put("author_profile", "author_regex", "^Doyle,", {"x": 1})
        self.store.clear()
        self.assertIsNone(self.store.get("author_profile", "author_regex", "^Doyle,"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
