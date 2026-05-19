"""Sprint 16 Phase F — topic_book_search intent + find_book_by_topic tool tests.

Verifies:
  * Intent routes «найди книгу про X» / «book about Y» to topic_book_search
  * Plan builder passes the topic phrase (with filler stripped) to the tool
  * find_book_by_topic dedupes chunks by pg_id and surfaces top-k books
  * Error paths: empty topic, no hybrid_search matches, hybrid failure"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class TopicBookSearchIntent(unittest.TestCase):

    def test_russian_najdi_knigu_pro(self):
        m = int_mod.classify("Найди книгу про викторианский Лондон")
        self.assertEqual(m.label, "topic_book_search")

    def test_russian_posovetuj_roman_o(self):
        m = int_mod.classify("Посоветуй роман о море")
        self.assertEqual(m.label, "topic_book_search")

    def test_russian_kniga_pro(self):
        """Без «найди» — просто «книга про Х»."""
        m = int_mod.classify("Книга про готический хоррор")
        self.assertEqual(m.label, "topic_book_search")

    def test_english_book_about(self):
        m = int_mod.classify("Find me a book about Victorian gas lamps")
        self.assertEqual(m.label, "topic_book_search")

    def test_chto_pochitat_pro(self):
        m = int_mod.classify("Что почитать про викторианский Лондон")
        self.assertEqual(m.label, "topic_book_search")

    def test_doesnt_steal_najdi_knigu_X(self):
        """«найди книгу X» (title search) → book_lookup, not topic search.
        topic_book_search requires the «про/о/about» preposition."""
        m = int_mod.classify("Найди книгу Pride and Prejudice")
        self.assertNotEqual(m.label, "topic_book_search")

    def test_doesnt_steal_b2_recommendation(self):
        """Pure level-based recommendation stays in book_recommendation."""
        m = int_mod.classify("Что почитать на уровне B2?")
        self.assertEqual(m.label, "book_recommendation")


class TopicBookSearchPlan(unittest.TestCase):

    def test_routes_to_find_book_by_topic(self):
        e = Entities(raw_misc={"raw_text": "Найди книгу про викторианский Лондон"})
        p = plan_mod.build("topic_book_search", e)
        self.assertEqual(p.intent, "topic_book_search")
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")
        self.assertEqual(p.steps[0].args["top"], 8)

    def test_filler_stripped_from_topic(self):
        e = Entities(raw_misc={"raw_text": "Найди книгу про викторианский Лондон"})
        p = plan_mod.build("topic_book_search", e)
        # Filler «найди книгу про» removed, topic just the phrase
        self.assertEqual(p.steps[0].args["topic"], "викторианский Лондон")

    def test_english_filler_stripped(self):
        e = Entities(raw_misc={"raw_text": "Find me a book about Gothic horror"})
        p = plan_mod.build("topic_book_search", e)
        self.assertEqual(p.steps[0].args["topic"], "Gothic horror")

    def test_no_match_passes_raw(self):
        """If the filler regex doesn't match, we pass raw text through —
        better to over-include than send empty topic."""
        e = Entities(raw_misc={"raw_text": "morning fog london"})
        p = plan_mod.build("topic_book_search", e)
        self.assertEqual(p.steps[0].args["topic"], "morning fog london")


class FindBookByTopicTool(unittest.TestCase):

    def _mock_hybrid_response(self, chunks):
        return ToolResult.success(
            tool="hybrid_search",
            data={"matches": chunks, "lexical_n": 5, "semantic_n": 5,
                  "k_rrf": 60},
            coverage=Coverage(books_matched=len(chunks), books_total=-1),
        )

    def test_dedupes_by_pg_id(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        # 5 chunks across 3 books — should dedupe to 3 unique books
        chunks = [
            {"pg_id": "PG1", "title": "T1", "author": "A1",
             "rrf_score": 0.9, "snippet": "best chunk PG1"},
            {"pg_id": "PG1", "title": "T1", "author": "A1",  # dupe
             "rrf_score": 0.8, "snippet": "second chunk PG1"},
            {"pg_id": "PG2", "title": "T2", "author": "A2",
             "rrf_score": 0.7, "snippet": "best chunk PG2"},
            {"pg_id": "PG3", "title": "T3", "author": "A3",
             "rrf_score": 0.6, "snippet": "best chunk PG3"},
            {"pg_id": "PG2", "title": "T2", "author": "A2",  # dupe
             "rrf_score": 0.5, "snippet": "second chunk PG2"},
        ]
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response(chunks)):
            r = find_book_by_topic("fog", top=5)
        self.assertTrue(r.ok)
        books = r.data["matches"]
        self.assertEqual(len(books), 3)
        ids = [b["pg_id"] for b in books]
        self.assertEqual(ids, ["PG1", "PG2", "PG3"])
        # Each book carries its BEST snippet (first one seen)
        self.assertEqual(books[0]["snippet"], "best chunk PG1")
        self.assertEqual(books[1]["snippet"], "best chunk PG2")

    def test_first_id_for_chaining(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": "PG1342", "title": "Pride", "author": "Austen",
                   "rrf_score": 0.9, "snippet": "marriage prospects"}]
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response(chunks)):
            r = find_book_by_topic("marriage")
        self.assertEqual(r.data["first_id"], "PG1342")

    def test_caps_at_top(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": f"PG{i}", "title": f"T{i}", "author": f"A{i}",
                    "rrf_score": 1.0 - i * 0.1, "snippet": f"s{i}"}
                  for i in range(10)]
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response(chunks)):
            r = find_book_by_topic("anything", top=3)
        self.assertEqual(len(r.data["matches"]), 3)
        self.assertEqual(r.data["books_returned"], 3)

    def test_empty_topic_returns_invalid_args(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        r = find_book_by_topic("")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")

    def test_hybrid_failure_propagates(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        fail_result = ToolResult.fail(
            tool="hybrid_search", err_type="internal", message="boom")
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=fail_result):
            r = find_book_by_topic("anything")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.message, "boom")

    def test_no_matches_returns_not_ok_with_warning(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response([])):
            r = find_book_by_topic("xyzzy nothing matches")
        self.assertFalse(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("no_topical_matches", codes)

    def test_few_unique_books_warns(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        # Only 2 unique books; user asked for 5
        chunks = [
            {"pg_id": "PG1", "title": "T", "author": "A", "rrf_score": 0.9, "snippet": "x"},
            {"pg_id": "PG2", "title": "T2", "author": "A2", "rrf_score": 0.8, "snippet": "y"},
        ]
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response(chunks)):
            r = find_book_by_topic("topic", top=5)
        codes = [w.code for w in r.warnings]
        self.assertIn("few_unique_books", codes)

    def test_passes_rerank_with_to_hybrid(self):
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        chunks = [{"pg_id": "PG1", "title": "T", "author": "A",
                    "rrf_score": 0.9, "snippet": "x"}]
        with mock.patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                         return_value=self._mock_hybrid_response(chunks)) as mp:
            find_book_by_topic("topic", rerank_with="bge_reranker")
            args = mp.call_args[0][1]
            self.assertEqual(args["rerank_with"], "bge_reranker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
