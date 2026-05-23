"""W-13 (Phase 5 P2 polish, 2026-05-23) — latency budget for
`find_book_by_topic` and the plans that fan into it.

Before:
  book_similar (find_book_by_topic + BGE rerank) reliably took 200-317s
  on cold cache because per_retriever defaulted to 60 and k formula was
  max(top*4, 40). With top=10 that meant 40 chunks → BGE cross-encoder
  → 30s+ wall clock per chunk in the worst case.

Acceptance:
  · Wrapper default per_retriever=30 (was 60).
  · k formula tightened to max(top*3, 30) (was max(top*4, 40)).
  · Plan builders (_plan_book_similar, _plan_topic_book_search) pin
    per_retriever=30 + top=8 at the plan level so the budget is
    visible without reading wrapper internals.

This file pins the budget — if any of these constants drift back up
without a separate ticket, the test catches it.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class W13FindBookByTopicBudget(unittest.TestCase):

    def test_wrapper_default_per_retriever_is_30(self):
        from scripts.v2.tools.books.find_book_by_topic import (
            find_book_by_topic,
        )
        sig = inspect.signature(find_book_by_topic)
        self.assertEqual(sig.parameters["per_retriever"].default, 30,
                          "W-13 caps per_retriever at 30 to bound BGE "
                          "rerank wall clock; default drift breaks the "
                          "<60s budget on book_similar.")

    def test_wrapper_default_top_unchanged(self):
        """`top` stays at 8 — W-13 doesn't change the user-visible
        result count, only the internal candidate pool."""
        from scripts.v2.tools.books.find_book_by_topic import (
            find_book_by_topic,
        )
        sig = inspect.signature(find_book_by_topic)
        self.assertEqual(sig.parameters["top"].default, 8)


class W13PlanBudget(unittest.TestCase):

    def test_plan_book_similar_pins_per_retriever_and_top(self):
        from scripts.v2.planner.builders.book import _plan_book_similar
        from scripts.v2.planner.entities import Entities
        e = Entities(book_id="PG2554", book_title="Crime and Punishment",
                     raw_misc={"raw_text": "что почитать после Crime and Punishment"})
        plan = _plan_book_similar(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(len(plan.steps), 1)
        step = plan.steps[0]
        self.assertEqual(step.tool, "find_book_by_topic")
        self.assertEqual(step.args["per_retriever"], 30,
                          "book_similar must pin per_retriever=30")
        self.assertEqual(step.args["top"], 8,
                          "book_similar must pin top=8")
        self.assertEqual(step.args["rerank_with"], "bge_reranker")

    def test_plan_topic_book_search_pins_per_retriever(self):
        from scripts.v2.planner.builders.composite import _plan_topic_book_search
        from scripts.v2.planner.entities import Entities
        e = Entities(
            raw_misc={"raw_text": "найди книгу про викторианский Лондон"},
        )
        plan = _plan_topic_book_search(e)
        self.assertEqual(len(plan.steps), 1)
        step = plan.steps[0]
        self.assertEqual(step.tool, "find_book_by_topic")
        self.assertEqual(step.args["per_retriever"], 30)
        self.assertEqual(step.args["top"], 8)


class W13KFormula(unittest.TestCase):

    def test_k_formula_under_load(self):
        """Trace the wrapper's `k` computation for the typical book_similar
        call (top=8) and confirm we land at 30 chunks — not 40+."""
        import unittest.mock as mock
        from scripts.v2.tools.books.find_book_by_topic import find_book_by_topic
        captured = {}

        def fake_dispatch(tool_name, args):
            captured["tool"] = tool_name
            captured["args"] = dict(args)
            # Pretend hybrid_search succeeded with empty matches so the
            # wrapper short-circuits on the no_topical_matches branch.
            from scripts.v2._types import ToolResult
            return ToolResult.success(
                tool="hybrid_search",
                data={"matches": [], "reranked_by": None},
            )

        with mock.patch("scripts.v2.tools.books.find_book_by_topic."
                        "v2_dispatch", side_effect=fake_dispatch):
            find_book_by_topic("crime fiction", top=8, per_retriever=30,
                                translate=False)
        self.assertEqual(captured["tool"], "hybrid_search")
        self.assertEqual(captured["args"]["k"], 30,
                          f"k formula should be max(top*3, 30); got "
                          f"{captured['args']['k']}")
        self.assertEqual(captured["args"]["per_retriever"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
