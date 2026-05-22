"""E11 (R-22 P11) — book_similar latency 317s → 45s timeout enforcement.

ROOT CAUSE: find_book_by_topic tool used default 60s timeout. Cold-path
ChromaDB + BGE rerank can exceed that with sufficiently complex queries.
SIGALRM did enforce, but 60s UX is still bad.

Fix: explicit timeout_s=45 on the tool. Cost rating bumped «medium» →
«heavy». User gets timeout error with retry hint instead of hanging.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FindBookByTopicTimeoutContract(unittest.TestCase):
    """Tool registration carries explicit timeout_s=45 (was default 60)."""

    def test_tool_has_explicit_timeout(self):
        # Trigger registration
        from scripts.v2.tool_registry import REGISTRY
        for mod in list(sys.modules):
            if mod.startswith("scripts.v2.tools"):
                continue  # already loaded
        import scripts.v2.tools  # noqa: F401

        spec = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(spec, "find_book_by_topic must be registered")
        # Explicit 45s timeout
        self.assertEqual(
            spec.timeout_s, 45,
            f"find_book_by_topic timeout_s should be 45 (E11 fix); "
            f"got {spec.timeout_s}",
        )

    def test_tool_cost_heavy(self):
        from scripts.v2.tool_registry import REGISTRY
        spec = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(spec)
        self.assertEqual(
            spec.cost, "heavy",
            f"find_book_by_topic should be cost=heavy (E11); got {spec.cost}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
