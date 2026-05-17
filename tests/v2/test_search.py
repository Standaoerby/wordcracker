"""Tests for v2 lexical_search and hybrid_search.

The FTS5 index doesn't exist on the dev box, so we either:
  * Build a tiny in-memory SQLite FTS5 fixture, OR
  * Point WC_FTS_DB at a real file built from a handful of test docs.

We go with the in-memory route — SQLite supports `:memory:` URIs, but the
lexical module uses URI-mode open, so we create a small temp file instead.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURE_DOCS = [
    ("PG1342", "Pride and Prejudice", "Austen, Jane",
     "It is a truth universally acknowledged that a single man in possession "
     "of a good fortune must be in want of a wife. The door was ajar."),
    ("PG345", "Dracula", "Stoker, Bram",
     "The dust was so thick that the count walked through the fog. "
     "He found the door slightly ajar."),
    ("PG2554", "Crime and Punishment", "Dostoyevsky, Fyodor",
     "Raskolnikov walked through the streets. There was no fog, but the "
     "shutters were closed."),
]


def _build_fts(db_path: Path):
    """Build a small FTS5 index from FIXTURE_DOCS."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    # Same DDL the build script writes — keep in sync if the script changes.
    conn.executescript("""
        CREATE TABLE documents (
            id      TEXT PRIMARY KEY,
            path    TEXT NOT NULL,
            mtime   REAL NOT NULL,
            text    TEXT NOT NULL,
            n_bytes INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            text,
            content='documents',
            content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
    """)
    for pg_id, _title, _author, text in FIXTURE_DOCS:
        conn.execute(
            "INSERT INTO documents (id, path, mtime, text, n_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            (pg_id, f"/fixture/{pg_id}.txt", 0.0, text, len(text)),
        )
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
    conn.close()


class LexicalSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "fts.sqlite"
        _build_fts(cls.db_path)
        os.environ["WC_FTS_DB"] = str(cls.db_path)
        # Refresh module config + connection
        from scripts.v2.tools.search import lexical
        lexical._close_for_test()
        lexical.FTS_DB_PATH = cls.db_path
        cls.lexical = lexical

    @classmethod
    def tearDownClass(cls):
        cls.lexical._close_for_test()
        cls.tmpdir.cleanup()
        os.environ.pop("WC_FTS_DB", None)

    def test_single_word(self):
        r = self.lexical.lexical_search("ajar", k=5)
        self.assertTrue(r.ok)
        ids = {m["pg_id"] for m in r.data["matches"]}
        self.assertIn("PG1342", ids)
        self.assertIn("PG345", ids)
        self.assertNotIn("PG2554", ids)

    def test_phrase(self):
        r = self.lexical.lexical_search('"through the fog"', k=5)
        self.assertTrue(r.ok)
        ids = {m["pg_id"] for m in r.data["matches"]}
        self.assertIn("PG345", ids)
        self.assertNotIn("PG1342", ids)

    def test_no_match(self):
        r = self.lexical.lexical_search("xyzzy_no_such_word", k=5)
        self.assertTrue(r.ok)  # FTS5 returns empty set, not error
        self.assertEqual(r.data["matches"], [])
        codes = [w.code for w in r.warnings]
        self.assertIn("no_matches", codes)

    def test_snippet_has_brackets(self):
        r = self.lexical.lexical_search("ajar", k=5)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.data["matches"]), 0)
        first = r.data["matches"][0]
        self.assertIn("[", first["snippet"])

    def test_bad_fts_syntax(self):
        # parentheses without contents are an error in FTS5
        r = self.lexical.lexical_search('( "', k=5)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


class LexicalNoIndex(unittest.TestCase):
    """When the FTS DB doesn't exist, the tool should fail soft."""

    def setUp(self):
        from scripts.v2.tools.search import lexical
        lexical._close_for_test()
        self.lexical = lexical
        self.tmpdir = tempfile.TemporaryDirectory()
        missing = Path(self.tmpdir.name) / "missing.sqlite"
        lexical.FTS_DB_PATH = missing
        os.environ["WC_FTS_DB"] = str(missing)

    def tearDown(self):
        self.lexical._close_for_test()
        self.tmpdir.cleanup()
        os.environ.pop("WC_FTS_DB", None)

    def test_missing_index_returns_warning_not_crash(self):
        r = self.lexical.lexical_search("ajar")
        self.assertFalse(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("fts_unavailable", codes)


class HybridRRFMerge(unittest.TestCase):
    """Pure-function check on the merge math — no DB needed."""

    def test_rrf_combines_ranks(self):
        from scripts.v2.tools.search.hybrid import _rank_map, K_RRF
        lex = [{"pg_id": "A"}, {"pg_id": "B"}, {"pg_id": "C"}]
        sem = [{"pg_id": "B"}, {"pg_id": "D"}]
        lex_r = _rank_map(lex)
        sem_r = _rank_map(sem)
        self.assertEqual(lex_r["A"], 1)
        self.assertEqual(sem_r["B"], 1)
        # Doc B appears in both → highest combined RRF
        score_b = 1 / (K_RRF + lex_r["B"]) + 1 / (K_RRF + sem_r["B"])
        score_a = 1 / (K_RRF + lex_r["A"])
        score_d = 1 / (K_RRF + sem_r["D"])
        self.assertGreater(score_b, score_a)
        self.assertGreater(score_b, score_d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
