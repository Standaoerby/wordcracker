"""E14 — shared retry-on-empty helper unit tests + affinity_by_book
integration.

ROOT CAUSE: compare_authors had Q15 step-down chain (Sprint 22+) for
both-empty retry. Same UX-class issue existed in affinity_by_book —
empty result with strict min_corpus_count had no auto-retry. R-22
demonstrated: «характерные прилагательные в "Dorian Gray"» at
min_corpus_count=200 + pos_filter=ADJ → empty.

Fix: extracted `_retry_on_empty.retry_with_lower_threshold` helper.
Pattern available to all filter-stepped tools (affinity_by_book,
future affinity_by_author / learning_words).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools._retry_on_empty import retry_with_lower_threshold


class HelperUnit(unittest.TestCase):
    def test_first_call_succeeds_no_retry(self):
        v1 = mock.Mock(return_value={"top_words": [{"word": "civility"}]})
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"pg_id": "PG1342", "min_corpus_count": 200},
            threshold_arg="min_corpus_count",
            steps=(100, 50, 20),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
        )
        self.assertEqual(v1.call_count, 1)
        self.assertEqual(len(raw["top_words"]), 1)
        # No annotations
        self.assertNotIn("_threshold_auto_lowered", raw)

    def test_first_empty_retry_succeeds(self):
        # v1 returns empty for 200, has data for 100
        def fake(**kw):
            if kw.get("min_corpus_count") == 200:
                return {"top_words": []}
            return {"top_words": [{"word": "civility"}]}
        v1 = mock.Mock(side_effect=fake)
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"pg_id": "PG1342", "min_corpus_count": 200},
            threshold_arg="min_corpus_count",
            steps=(100, 50, 20),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
        )
        self.assertEqual(v1.call_count, 2)  # 200 + 100
        self.assertEqual(len(raw["top_words"]), 1)
        self.assertTrue(raw.get("_threshold_auto_lowered"))
        self.assertEqual(raw["min_corpus_count_used"], 100)
        self.assertEqual(raw["min_corpus_count_requested"], 200)

    def test_all_steps_empty(self):
        v1 = mock.Mock(return_value={"top_words": []})
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"pg_id": "PG1342", "min_corpus_count": 200},
            threshold_arg="min_corpus_count",
            steps=(100, 50, 20),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
        )
        # Initial + all 3 retries = 4 calls
        self.assertEqual(v1.call_count, 4)
        # Annotated as exhausted, no fabrication
        self.assertTrue(raw.get("_retry_exhausted"))
        self.assertFalse(raw.get("_threshold_auto_lowered"))

    def test_skip_steps_above_initial(self):
        # Initial=50, steps=(100, 30) — 100 is skipped (above initial)
        v1 = mock.Mock(side_effect=[
            {"top_words": []},  # initial 50 empty
            {"top_words": [{"word": "x"}]},  # 30 has data
        ])
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"min_corpus_count": 50},
            threshold_arg="min_corpus_count",
            steps=(100, 30),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
        )
        # Only 2 calls (50 + 30), 100 skipped
        self.assertEqual(v1.call_count, 2)
        self.assertEqual(raw["min_corpus_count_used"], 30)

    def test_min_initial_gate(self):
        # If min_initial=100, skip retry entirely for initial=50
        v1 = mock.Mock(return_value={"top_words": []})
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"min_corpus_count": 50},
            threshold_arg="min_corpus_count",
            steps=(20, 10),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
            min_initial=100,
        )
        # Only 1 call — gate blocks retry
        self.assertEqual(v1.call_count, 1)
        self.assertFalse(raw.get("_threshold_auto_lowered", False))

    def test_v1_error_doesnt_break_helper(self):
        # First call OK but empty; retry raises; helper continues
        def fake(**kw):
            if kw.get("min_corpus_count") == 200:
                return {"top_words": []}
            if kw.get("min_corpus_count") == 100:
                raise RuntimeError("boom")
            return {"top_words": [{"word": "x"}]}
        v1 = mock.Mock(side_effect=fake)
        raw = retry_with_lower_threshold(
            v1_fn=v1,
            v1_args={"min_corpus_count": 200},
            threshold_arg="min_corpus_count",
            steps=(100, 50),
            is_empty_fn=lambda r: not (r or {}).get("top_words"),
        )
        # 100 raised → continued to 50 which works
        self.assertEqual(raw.get("min_corpus_count_used"), 50)


class AffinityByBookIntegration(unittest.TestCase):
    """Verify affinity_by_book wrapper uses the helper."""

    def test_book_empty_retries_lower(self):
        from scripts.v2.tools.books.affinity_book import affinity_by_book

        def fake_v1(**kw):
            mcc = kw.get("min_corpus_count")
            if mcc == 200:
                return {"top_words": [], "book_title": "Dorian"}
            if mcc == 100:
                return {"top_words": [], "book_title": "Dorian"}
            return {"top_words": [{"word": "uncanny", "affinity": 5.0}],
                    "book_title": "Dorian"}

        with mock.patch("scripts.learning_tools.affinity_by_book",
                         side_effect=fake_v1):
            r = affinity_by_book(pg_id="PG174", pos_filter=["ADJ"],
                                  min_corpus_count=200)
        self.assertTrue(r.ok)
        # Should have retry annotations
        self.assertTrue(r.data.get("_threshold_auto_lowered"))
        self.assertEqual(r.data["min_corpus_count_used"], 50)

    def test_book_initial_works_no_retry_annotation(self):
        from scripts.v2.tools.books.affinity_book import affinity_by_book

        def fake_v1(**kw):
            return {"top_words": [{"word": "uncanny", "affinity": 5.0}],
                    "book_title": "Dorian"}

        with mock.patch("scripts.learning_tools.affinity_by_book",
                         side_effect=fake_v1):
            r = affinity_by_book(pg_id="PG174", min_corpus_count=200)
        self.assertTrue(r.ok)
        # No annotation when initial succeeds
        self.assertFalse(r.data.get("_threshold_auto_lowered", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
