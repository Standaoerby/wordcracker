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


class HybridIntegration(unittest.TestCase):
    """End-to-end hybrid with both retrievers mocked.

    Verifies the merge correctly handles either retriever failing — semantic
    or lexical alone should still produce useful output."""

    def setUp(self):
        import types
        from scripts.v2 import legacy_dispatch
        from scripts.v2.tool_registry import REGISTRY, ToolSpec
        from scripts.v2._types import Coverage, ToolResult

        # Snapshot REGISTRY, ensure tools loaded so the spec class exists.
        # If a prior test cleared REGISTRY, re-import v2.tools to repopulate.
        if "lexical_search" not in REGISTRY:
            for mod in list(sys.modules):
                if mod.startswith("scripts.v2.tools"):
                    del sys.modules[mod]
            import scripts.v2.tools  # noqa: F401
        self._snap = dict(REGISTRY)

        # Replace lexical_search with a mock that returns 3 PG ids.
        def _mock_lex(query, k=10, snippet_chars=200):
            return ToolResult.success(
                tool="lexical_search",
                data={"query": query, "matches": [
                    {"pg_id": "PG1", "score": -5.1, "snippet": "[ajar] door"},
                    {"pg_id": "PG2", "score": -4.5, "snippet": "door [ajar] there"},
                    {"pg_id": "PG3", "score": -4.0, "snippet": "[ajar]"}]},
                coverage=Coverage(books_matched=3, books_total=-1),
            )
        REGISTRY["lexical_search"] = ToolSpec(
            name="lexical_search",
            fn=_mock_lex,
            category="search",
            description="mock",
            input_schema={"type": "object"},
            cost="cheap",
            cacheable=False,
        )

        # Mock semantic_search too. After native migration semantic_search is
        # in REGISTRY (not legacy_dispatch), so we replace its ToolSpec here
        # and the rag_query stub is kept as a backstop in case any old
        # legacy_dispatch call sneaks through.
        def _mock_sem(query, k=8, author_filter=None):
            return ToolResult.success(
                tool="semantic_search",
                data={"results": [
                    {"metadata": {"pg_id": "PG2", "title": "T2", "author": "A2"},
                     "text": "near"},
                    {"metadata": {"pg_id": "PG4", "title": "T4", "author": "A4"},
                     "text": "haze"},
                ]},
                coverage=Coverage(books_matched=2, books_total=-1),
            )
        REGISTRY["semantic_search"] = ToolSpec(
            name="semantic_search",
            fn=_mock_sem,
            category="search",
            description="mock",
            input_schema={"type": "object"},
            cost="medium",
            cacheable=False,
        )

        # Backstop for legacy_dispatch (unlikely to fire now, but defensive).
        m = types.ModuleType("scripts.rag_query")
        m.TOOL_DISPATCH = {
            "semantic_search": lambda **kw: {
                "results": [
                    {"metadata": {"pg_id": "PG2", "title": "T2", "author": "A2"}, "text": "near"},
                    {"metadata": {"pg_id": "PG4", "title": "T4", "author": "A4"}, "text": "haze"},
                ]
            }
        }
        sys.modules["scripts.rag_query"] = m
        legacy_dispatch._LEGACY_DISPATCH_CACHE.clear()
        legacy_dispatch._LEGACY_DISPATCH_CACHE.update({"dispatch": None, "loaded": False})

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear(); REGISTRY.update(self._snap)
        sys.modules.pop("scripts.rag_query", None)

    def test_both_retrievers_merge(self):
        from scripts.v2.tools.search.hybrid import hybrid_search
        r = hybrid_search("ajar", k=10, per_retriever=10)
        self.assertTrue(r.ok)
        ids = [m["pg_id"] for m in r.data["matches"]]
        # PG2 appears in both → should rank #1
        self.assertEqual(ids[0], "PG2")
        # All 4 unique ids should appear
        self.assertEqual(set(ids), {"PG1", "PG2", "PG3", "PG4"})
        self.assertEqual(r.data["lexical_n"], 3)
        self.assertEqual(r.data["semantic_n"], 2)

    def test_rerank_reorders_top_k(self):
        """rerank_with='bge_reranker' should reorder by cross-encoder scores.

        Mock the reranker to assign decreasing scores to PG4, PG3, PG2, PG1
        — exact reverse of RRF order. Final order should follow the
        reranker, not RRF."""
        from unittest import mock as _mock
        from scripts.v2.tools.search.hybrid import hybrid_search
        # Force-load registry, then patch the plugin's compute().
        from scripts.v2.scoring import REGISTRY, ScoredItem
        plugin = REGISTRY["bge_reranker"]
        fake_compute = _mock.MagicMock(return_value=[
            ScoredItem(id="PG4", score=0.95, direction="higher_better"),
            ScoredItem(id="PG3", score=0.80, direction="higher_better"),
            ScoredItem(id="PG2", score=0.60, direction="higher_better"),
            ScoredItem(id="PG1", score=0.10, direction="higher_better"),
        ])
        with _mock.patch.object(plugin, "compute", fake_compute):
            r = hybrid_search("ajar", k=10, per_retriever=10,
                              rerank_with="bge_reranker")
        self.assertTrue(r.ok)
        ids = [m["pg_id"] for m in r.data["matches"]]
        self.assertEqual(ids, ["PG4", "PG3", "PG2", "PG1"])
        self.assertEqual(r.data.get("reranked_by"), "bge_reranker")
        # rerank_score is attached to each match
        for m in r.data["matches"]:
            self.assertIn("rerank_score", m)

    def test_rerank_unknown_plugin_warns(self):
        from scripts.v2.tools.search.hybrid import hybrid_search
        r = hybrid_search("ajar", k=5, per_retriever=10,
                          rerank_with="not_a_real_plugin")
        # Tool still succeeds — rerank failure is non-fatal
        self.assertTrue(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("rerank_unknown", codes)
        # data shouldn't claim it was reranked
        self.assertNotIn("reranked_by", r.data)

    def test_rerank_wrong_kind_warns(self):
        from scripts.v2.tools.search.hybrid import hybrid_search
        # burrows_delta is kind="author_similarity", not retrieval_rerank
        r = hybrid_search("ajar", k=5, per_retriever=10,
                          rerank_with="burrows_delta")
        self.assertTrue(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("rerank_kind_mismatch", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
