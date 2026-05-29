"""E34 — semantic_search Coverage must count DISTINCT FLAT pg_id.

ROOT CAUSE: the wrapper computed
    books_matched = len({r.get("metadata", {}).get("pg_id") for r in rows})
but v1 semantic_search returns each result row with pg_id FLAT at the top
level (no `metadata` nesting). So `r.get("metadata", {})` is `{}` for every
row, `.get("pg_id")` is None, the set collapses to {None}, and
books_matched is stuck at 1 regardless of how many distinct books matched.

FIX: read pg_id flat (`r.get("pg_id")`) and drop empties.

This replays the recorded golden fixture (8 result rows, 8 distinct pg_id)
so a future regression to the nested read — or any shape drift — fails
loudly. The v1↔v2 static contract gate cannot catch this: it checks key
*names* (both `pg_id` and `metadata` are tolerated row keys), not nesting.
"""
from __future__ import annotations

import json
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
_FIX = _REPO / "scripts" / "v2" / "contracts" / "fixtures"


class TestSemanticCoverageE34(unittest.TestCase):
    def _golden(self) -> dict:
        with open(_FIX / "scripts.rag_tools.semantic_search.json",
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_coverage_counts_distinct_flat_pg_ids(self):
        from scripts.v2.tools.search.semantic import semantic_search

        raw = self._golden()
        distinct = len({r["pg_id"] for r in raw["results"]})
        self.assertGreater(
            distinct, 1,
            "golden fixture must have >1 distinct pg_id for this test to bite",
        )

        with mock.patch("scripts.rag_tools.semantic_search", return_value=raw):
            r = semantic_search(query="vampire")

        self.assertTrue(r.ok)
        # The bug symptom: stuck at 1.
        self.assertNotEqual(
            r.coverage.books_matched, 1,
            "E34 regression: Coverage stuck at 1 — wrapper read nested "
            "metadata.pg_id instead of flat pg_id",
        )
        self.assertEqual(r.coverage.books_matched, distinct)

    def test_distinct_dedup_not_row_count(self):
        """books_matched counts DISTINCT pg_id, not row count: 3 rows with
        2 distinct pg_id → 2 (proves flat read + dedup, not the {None}=1 bug
        and not a naive len(rows))."""
        from scripts.v2.tools.search.semantic import semantic_search

        raw = {
            "query": "x", "retrieval_query": "x", "author_filter": None,
            "results": [
                {"pg_id": "PG1", "title": "a", "distance": 0.1},
                {"pg_id": "PG1", "title": "b", "distance": 0.2},
                {"pg_id": "PG2", "title": "c", "distance": 0.3},
            ],
        }
        with mock.patch("scripts.rag_tools.semantic_search", return_value=raw):
            r = semantic_search(query="x")
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage.books_matched, 2)


if __name__ == "__main__":
    unittest.main()
