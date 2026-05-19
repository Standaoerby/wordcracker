"""Sprint 18 — retrieval pipeline quality lift.

Three coordinated changes:
  1) BGE rerank ON by default for topical-relevance intents
     (topic_book_search, book_similar, word_contexts no-author path).
  2) Retrieval-source logging (_extract_retrieval_log) — diagnostic
     bridge between «model gave bad answer» and «model got bad source».
  3) hybrid_search default per_retriever bumped 30 → 50 — wider
     candidate pool for RRF + rerank to choose from.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class BGERerankDefaults(unittest.TestCase):
    """Three relevance-critical intents must pass rerank_with=
    bge_reranker through their plan-builders so production stops
    relying on bi-encoder ranking alone."""

    def test_topic_book_search_uses_bge(self):
        e = Entities(raw_misc={"raw_text":
                                 "найди книгу про викторианский Лондон"})
        p = plan_mod.build("topic_book_search", e)
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")
        self.assertEqual(p.steps[0].args.get("rerank_with"), "bge_reranker")

    def test_book_similar_uses_bge(self):
        e = Entities(book_id="PG2554", book_title="Crime and Punishment")
        p = plan_mod.build("book_similar", e)
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")
        self.assertEqual(p.steps[0].args.get("rerank_with"), "bge_reranker")

    def test_word_contexts_no_author_uses_bge(self):
        """Without author scope, word_contexts dispatches to hybrid_search
        — rerank must be on so generic mention spam gets reordered out."""
        e = Entities(word="fog")
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(p.steps[0].tool, "hybrid_search")
        self.assertEqual(p.steps[0].args.get("rerank_with"), "bge_reranker")

    def test_word_contexts_with_author_does_NOT_rerank(self):
        """Author-scoped word_contexts uses the cheap v1 tool — no need
        for cross-encoder cost when scope is already narrowed."""
        e = Entities(word="fog", author_regex="^Doyle,")
        p = plan_mod.build("word_contexts", e)
        # primary step is word_contexts (NOT hybrid_search), no rerank_with
        self.assertEqual(p.steps[0].tool, "word_contexts")
        self.assertNotIn("rerank_with", p.steps[0].args)


class HybridSearchDefaults(unittest.TestCase):
    """per_retriever bumped 30 → 50 to give RRF + rerank a wider pool."""

    def test_default_per_retriever_is_50(self):
        import inspect
        from scripts.v2.tools.search import hybrid
        sig = inspect.signature(hybrid.hybrid_search.__wrapped__
                                 if hasattr(hybrid.hybrid_search, "__wrapped__")
                                 else hybrid.hybrid_search)
        # ToolSpec wrap or direct — either way the default is what matters
        # but the registered tool spec wraps via tool_registry. Inspect
        # the underlying function via _impl if needed.
        try:
            default = sig.parameters["per_retriever"].default
        except KeyError:
            default = None
        # Fall through to fn impl if wrapper hides it
        if default is None:
            from scripts.v2.tool_registry import REGISTRY
            spec = REGISTRY.get("hybrid_search")
            if spec:
                sig2 = inspect.signature(spec.fn)
                default = sig2.parameters["per_retriever"].default
        self.assertEqual(default, 50)


class RetrievalLogExtraction(unittest.TestCase):
    """_extract_retrieval_log produces a compact diagnostic row per
    RAG-tool match so we can post-hoc audit «what fed the prompt»."""

    def _success(self, tool: str, data: dict) -> ToolResult:
        return ToolResult.success(
            tool=tool, data=data,
            coverage=Coverage(books_matched=1, books_total=-1),
        )

    def test_returns_none_on_empty(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        self.assertIsNone(_extract_retrieval_log([]))

    def test_skips_non_retrieval_tools(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        # author_metadata is NOT a retrieval tool
        r = self._success("author_metadata", {"matches": [
            {"pg_id": "PG1", "title": "X"}]})
        self.assertIsNone(_extract_retrieval_log([r]))

    def test_captures_hybrid_search_matches(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        r = self._success("hybrid_search", {"matches": [
            {"pg_id": "PG1342", "title": "Pride", "author": "Austen",
             "rrf_score": 0.87, "snippet": "It is a truth universally..."},
            {"pg_id": "PG345", "title": "Dracula", "author": "Stoker",
             "rrf_score": 0.81, "snippet": "The dust was so thick..."},
        ]})
        log = _extract_retrieval_log([r])
        self.assertIsNotNone(log)
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["tool"], "hybrid_search")
        self.assertEqual(log[0]["pg_id"], "PG1342")
        self.assertEqual(log[0]["score"], 0.87)
        self.assertEqual(log[0]["score_kind"], "rrf_score")
        self.assertIn("universally", log[0]["snippet_preview"])

    def test_prefers_rerank_score_when_present(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        r = self._success("find_book_by_topic", {"matches": [
            {"pg_id": "PG1", "rrf_score": 0.5, "rerank_score": 0.95,
             "snippet": "y"},
        ]})
        log = _extract_retrieval_log([r])
        self.assertEqual(log[0]["score"], 0.95)
        self.assertEqual(log[0]["score_kind"], "rerank_score")

    def test_caps_at_limit(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        matches = [{"pg_id": f"PG{i}", "snippet": "x"} for i in range(20)]
        r = self._success("hybrid_search", {"matches": matches})
        log = _extract_retrieval_log([r], limit=5)
        self.assertEqual(len(log), 5)

    def test_combines_multiple_tools(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        r1 = self._success("hybrid_search", {"matches": [
            {"pg_id": "PG1", "rrf_score": 0.9, "snippet": "a"}]})
        r2 = self._success("find_book_by_topic", {"matches": [
            {"pg_id": "PG2", "rrf_score": 0.8, "snippet": "b"}]})
        log = _extract_retrieval_log([r1, r2])
        self.assertEqual(len(log), 2)
        tools_seen = {row["tool"] for row in log}
        self.assertEqual(tools_seen, {"hybrid_search", "find_book_by_topic"})

    def test_skips_failed_results(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        failed = ToolResult.fail(tool="hybrid_search", err_type="internal",
                                  message="boom")
        log = _extract_retrieval_log([failed])
        self.assertIsNone(log)

    def test_snippet_preview_truncated(self):
        from scripts.v2.rag_v2 import _extract_retrieval_log
        long_snippet = "x" * 500
        r = self._success("hybrid_search", {"matches": [
            {"pg_id": "PG1", "rrf_score": 0.9, "snippet": long_snippet}]})
        log = _extract_retrieval_log([r])
        # 120-char cap
        self.assertLessEqual(len(log[0]["snippet_preview"]), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
