"""Sprint 19+ — find_book_by_topic precision improvements.

Stan 2026-05-19 «найди книгу про магическую школу» surfaced Little
Red Riding Hood (PG29178) at rerank_score ~0.12 alongside legitimate
hits. Three fixes:
  1) min_rerank_score threshold filter (default 0.4)
  2) RU → EN translation pass before semantic search
  3) rerank_score column displayed in renderer output (via _render_note)
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult


class RerankScoreThreshold(unittest.TestCase):

    def _hybrid_response_with_scores(self, scores):
        chunks = [{"pg_id": f"PG{i}", "title": f"Book {i}",
                   "author": f"Author {i}", "rrf_score": 0.5,
                   "rerank_score": s, "snippet": f"snippet {i}"}
                  for i, s in enumerate(scores)]
        return ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "reranked_by": "bge_reranker",
                  "lexical_n": len(chunks), "semantic_n": len(chunks)},
            coverage=Coverage(books_matched=len(chunks), books_total=-1),
        )

    def test_drops_below_threshold(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        # 5 results: 3 above 0.4, 2 below
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._hybrid_response_with_scores(
                             [0.85, 0.62, 0.41, 0.18, 0.05])):
            r = find_book_by_topic("magic school", top=10,
                                    rerank_with="bge_reranker")
        ids = [m["pg_id"] for m in r.data["matches"]]
        self.assertEqual(len(ids), 3, msg=f"expected 3, got {ids}")
        # Only the high-scoring ones survived
        for m in r.data["matches"]:
            self.assertGreaterEqual(m["rerank_score"], 0.4)

    def test_skip_filter_when_no_reranker(self):
        """Without rerank_with, no rerank_score → threshold ignored."""
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": f"PG{i}", "rrf_score": 0.5, "snippet": "x"}
                  for i in range(3)]
        fake = ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "reranked_by": None,
                  "lexical_n": 3, "semantic_n": 3},
            coverage=Coverage(books_matched=3, books_total=-1),
        )
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=fake):
            r = find_book_by_topic("topic", top=10)
        self.assertEqual(len(r.data["matches"]), 3)

    def test_caller_can_lower_threshold(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._hybrid_response_with_scores(
                             [0.85, 0.62, 0.41, 0.18, 0.05])):
            r = find_book_by_topic("topic", top=10, rerank_with="bge_reranker",
                                    min_rerank_score=0.1)
        # Threshold 0.1 → 4 survive
        self.assertEqual(len(r.data["matches"]), 4)


class TopicTranslation(unittest.TestCase):

    def _identity_hybrid(self, query):
        chunks = [{"pg_id": "PG1", "rrf_score": 0.8, "snippet": "x"}]
        return ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "query": query,
                  "lexical_n": 1, "semantic_n": 1},
            coverage=Coverage(books_matched=1, books_total=-1),
        )

    def test_cyrillic_topic_translated(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        captured = {}
        def _spy(name, args):
            captured["args"] = args
            return self._identity_hybrid(args["query"])
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         side_effect=_spy), \
             mock.patch("scripts.rag_tools._maybe_translate",
                         return_value="magic school"):
            r = find_book_by_topic("магическая школа", top=5)
        self.assertEqual(captured["args"]["query"], "magic school")
        self.assertEqual(r.data["translated_from"], "магическая школа")
        self.assertEqual(r.data["topic_searched_as"], "magic school")

    def test_english_topic_not_translated(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        captured = {}
        def _spy(name, args):
            captured["args"] = args
            return self._identity_hybrid(args["query"])
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         side_effect=_spy):
            r = find_book_by_topic("magic school", top=5)
        self.assertEqual(captured["args"]["query"], "magic school")
        self.assertIsNone(r.data["translated_from"])

    def test_translation_can_be_disabled(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        captured = {}
        def _spy(name, args):
            captured["args"] = args
            return self._identity_hybrid(args["query"])
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         side_effect=_spy):
            r = find_book_by_topic("магическая школа", top=5,
                                    translate=False)
        # Raw RU passed through
        self.assertEqual(captured["args"]["query"], "магическая школа")
        self.assertIsNone(r.data["translated_from"])


class RenderNotePresence(unittest.TestCase):

    def test_note_includes_rerank_column_instruction(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": "PG1", "rrf_score": 0.5,
                   "rerank_score": 0.85, "snippet": "x"}]
        fake = ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "reranked_by": "bge_reranker",
                  "lexical_n": 1, "semantic_n": 1},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=fake):
            r = find_book_by_topic("topic", top=5,
                                    rerank_with="bge_reranker")
        note = r.data.get("_render_note") or ""
        self.assertIn("rerank_score", note)

    def test_note_discloses_translation(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": "PG1", "rrf_score": 0.5, "snippet": "x"}]
        fake = ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "reranked_by": None,
                  "lexical_n": 1, "semantic_n": 1},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=fake), \
             mock.patch("scripts.rag_tools._maybe_translate",
                         return_value="magic school"):
            r = find_book_by_topic("магическая школа", top=5)
        note = r.data.get("_render_note") or ""
        self.assertIn("магическая школа", note)
        self.assertIn("magic school", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
