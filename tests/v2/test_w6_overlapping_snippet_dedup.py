"""W-6 follow-up (2026-05-24) — near-duplicate (overlapping-window)
snippet dedup on top of exact dedup_by_key.

Stan prod 2026-05-22 «примеры heart у Дойла»: after the W-6 snippet
normalization landed, exact dedup collapsed identical strings — but
overlapping ±N-token windows on the SAME source sentence still slipped
through (two snippets that share the same passage from opposite sides
have different surface strings → dedup_by_key keeps both).

This file pins:
  1. `dedup_overlapping_snippets` collapses pairs with token-set Jaccard
     overlap ≥ threshold.
  2. The word_contexts wrapper invokes it AFTER exact dedup and warns
     when it fires.
  3. Distinct snippets that merely share a few stopwords/common tokens
     are NOT collapsed (threshold tuned conservatively).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools._result_filters import dedup_overlapping_snippets


class OverlappingSnippetsCollapsed(unittest.TestCase):
    """Two windows that bracket the same sentence share 8+ content
    tokens → Jaccard ≥ 0.55 → drop the later one."""

    def test_two_windows_on_same_sentence_collapse(self):
        rows = [
            {"pg_id": "PG108", "title": "Sherlock",
             "snippet": "his heart was heavy with the news he had to deliver"},
            {"pg_id": "PG108", "title": "Sherlock",
             "snippet": "the news he had to deliver his heart pounded against his ribs"},
        ]
        out, dropped = dedup_overlapping_snippets(rows, key="snippet")
        # After short-stopword pruning (len<3 dropped: «he», «to»),
        # 6 content tokens shared («his», «heart», «the», «news»,
        # «had», «deliver») over 12 unique union tokens → Jaccard 0.5,
        # above the 0.45 default threshold.
        self.assertEqual(len(out), 1)
        self.assertEqual(dropped, 1)
        # First occurrence wins.
        self.assertEqual(out[0]["pg_id"], "PG108")
        self.assertIn("heavy", out[0]["snippet"])

    def test_three_overlapping_windows_collapse_to_one(self):
        passage_a = "the heart of darkness lay before him in silence"
        rows = [
            {"pg_id": "PG1", "snippet": passage_a},
            {"pg_id": "PG1", "snippet": "darkness lay before him in silence and despair"},
            {"pg_id": "PG1", "snippet": "before him in silence with the heart of darkness"},
        ]
        out, dropped = dedup_overlapping_snippets(rows, key="snippet")
        self.assertEqual(len(out), 1)
        self.assertEqual(dropped, 2)


class DistinctSnippetsPreserved(unittest.TestCase):
    """Snippets that share only stopwords / 1-2 content tokens stay
    in — threshold is conservative."""

    def test_two_genuinely_different_heart_snippets_kept(self):
        rows = [
            {"pg_id": "PG108", "snippet": "his heart was heavy with sorrow"},
            {"pg_id": "PG244", "snippet": "a faint heart never won fair lady"},
        ]
        out, dropped = dedup_overlapping_snippets(rows, key="snippet")
        # Share «heart» only — Jaccard ≈ 0.08, well under threshold.
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_unrelated_snippets_with_common_articles_kept(self):
        rows = [
            {"pg_id": "PG1", "snippet": "the heart of darkness lay before him"},
            {"pg_id": "PG2", "snippet": "she put her heart into the dance and never stopped"},
        ]
        out, dropped = dedup_overlapping_snippets(rows, key="snippet")
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)


class EdgeCases(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        out, dropped = dedup_overlapping_snippets([], key="snippet")
        self.assertEqual(out, [])
        self.assertEqual(dropped, 0)

    def test_missing_or_blank_key_passes_through(self):
        rows = [
            {"pg_id": "PG1"},  # no snippet
            {"pg_id": "PG2", "snippet": ""},
            {"pg_id": "PG3", "snippet": "real snippet text content here"},
        ]
        out, dropped = dedup_overlapping_snippets(rows, key="snippet")
        self.assertEqual(len(out), 3)
        self.assertEqual(dropped, 0)

    def test_threshold_zero_disables_filter(self):
        # Defensive — out-of-range threshold should NOT raise.
        rows = [
            {"snippet": "the heart was heavy with sorrow"},
            {"snippet": "the heart was heavy with sorrow"},
        ]
        out, dropped = dedup_overlapping_snippets(
            rows, key="snippet", threshold=0)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_custom_higher_threshold_keeps_more(self):
        rows = [
            {"snippet": "his heart was heavy with the news"},
            {"snippet": "the news weighed on his heart and pulled him down"},
        ]
        # Share «the», «his», «heart», «news» (4 content tokens).
        # Looser default → likely drop; higher threshold → keep.
        _, dropped_strict = dedup_overlapping_snippets(
            rows, key="snippet", threshold=0.85)
        self.assertEqual(dropped_strict, 0)


class WordContextsWrapperFiresOverlapDedup(unittest.TestCase):
    """End-to-end: the v2 wrapper applies the new filter after exact
    dedup, and emits a `snippet_overlap_dedup` warning when it does."""

    def test_overlapping_windows_collapse_in_wrapper(self):
        from scripts.v2.tools.words.contexts import word_contexts
        v1 = {
            "author_regex": "^Doyle,", "word": "heart",
            "samples": [
                {"pg_id": "PG108", "title": "Sherlock",
                 "context": "his heart was heavy with the news he had to deliver"},
                {"pg_id": "PG108", "title": "Sherlock",
                 "context": "the news he had to deliver his heart pounded against his ribs"},
                {"pg_id": "PG244", "title": "Study",
                 "context": "a faint heart never won fair lady or anything else"},
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts", return_value=v1):
            r = word_contexts(author_regex="^Doyle,", word="heart")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        # 3 inputs → 2 unique after overlap dedup (two overlapping
        # «delivered news» windows collapse to one, faint-heart kept).
        self.assertEqual(len(samples), 2)
        warning_codes = {w.code for w in r.warnings}
        self.assertIn("snippet_overlap_dedup", warning_codes)
        # Filter-drops bookkeeping shows the new key.
        drops = (r.data or {}).get("_filter_drops") or {}
        self.assertEqual(drops.get("dedup_overlapping_snippets"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
