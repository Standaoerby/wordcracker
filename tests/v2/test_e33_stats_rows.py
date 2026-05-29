"""E33 — corpus_stats_by_author must render EVERY metric row from the
canonical v1 keys.

ROOT CAUSE (fixed in e0daeda, phase-2 contract refactor): the view read
phantom aliases (books_total / tokens_total / vocab_size / book_count /
n_tokens) that v1 never emits. Each resolved to None, the row was dropped,
and the panel rendered only the 1-2 rows whose alias happened to match —
"renders 2 of 6 rows".

The wrapper now reads the canonical keys (books_matched, books_with_counts,
total_tokens, unique_words, avg_book_length_words, longest_book,
shortest_book). This test regression-locks that: it replays the recorded
golden fixture (all canonical keys present + non-null) and asserts all 7
metric rows render non-empty. Reintroducing a phantom key drops its row and
fails this test.
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

# Canonical metric rows the wrapper builds (human label order is internal;
# we assert on count + non-empty values, not wording).
_EXPECTED_ROWS = 7


class TestStatsRowsE33(unittest.TestCase):
    def _golden(self) -> dict:
        with open(_FIX / "scripts.rag_tools.corpus_stats_by_author.json",
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_all_metric_rows_render_nonempty(self):
        from scripts.v2.tools.corpus_meta.stats_by_author import (
            corpus_stats_by_author,
        )

        raw = self._golden()
        with mock.patch("scripts.rag_tools.corpus_stats_by_author",
                        return_value=raw):
            r = corpus_stats_by_author(author_regex="^Stoker, Bram$")

        self.assertTrue(r.ok)
        self.assertIsNotNone(r.view, "no view attached — canonical keys "
                                     "missing would drop all rows")
        rows = r.view.payload.get("rows", [])
        self.assertEqual(
            len(rows), _EXPECTED_ROWS,
            f"E33 regression: expected {_EXPECTED_ROWS} metric rows, got "
            f"{len(rows)} — a phantom key dropped a row. rows={rows}",
        )
        for row in rows:
            self.assertNotIn(
                row.get("value"), (None, "", [], {}),
                f"E33 regression: empty metric value in row {row}",
            )

    def test_phantom_key_would_drop_rows(self):
        """Guard the guard: a v1 payload missing a canonical key (simulating
        a wrapper that read a phantom alias) renders fewer rows — proving the
        row-count assertion above actually bites."""
        from scripts.v2.tools.corpus_meta.stats_by_author import (
            corpus_stats_by_author,
        )

        raw = self._golden()
        raw.pop("total_tokens")          # simulate phantom/missing key
        raw.pop("unique_words")
        with mock.patch("scripts.rag_tools.corpus_stats_by_author",
                        return_value=raw):
            r = corpus_stats_by_author(author_regex="^Stoker, Bram$")
        self.assertTrue(r.ok)
        rows = r.view.payload.get("rows", [])
        self.assertEqual(len(rows), _EXPECTED_ROWS - 2)


if __name__ == "__main__":
    unittest.main()
