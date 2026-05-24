"""Sprint 16 Phase C — word_collocates metric reranking integration tests.

Pure math is covered in test_scoring_plugins.py (PMI/NPMI/Dice). Here we
verify the wrapper's wiring: v1 passthrough, metric dispatch, error
soft-fail paths."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class WordCollocatesMetric(unittest.TestCase):

    def setUp(self):
        # Stable v1 fixture: 4 candidates with known co-occurrence counts.
        self.v1_raw = {
            "scope": "author:^X,",
            "word": "fog",
            "window": 4,
            "total_occurrences": 200,
            "books_with_hits": 50,
            "top_collocates": [
                {"word": "thick",  "count": 80},   # tight pair
                {"word": "morning","count": 60},
                {"word": "city",   "count": 40},
                {"word": "the",    "count": 600},  # noise (very common neighbor)
            ],
        }

    def test_explicit_metric_count_unchanged_passthrough(self):
        """metric='count' (explicit) → wrapper passes v1 ordering through
        without rerank, but still applies the W-15 stopword filter so
        the count-only path doesn't surface noise.

        W-15 (2026-05-23) — the default flipped to 'npmi'.
        W-15 polish (2026-05-24) — wrapper-level stopword filter drops
        'the' (in v1 STOPWORDS) even on the count path. The signal rows
        ('thick'/'morning'/'city') stay in their original v1 order.
        """
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw):
            r = word_collocates({"author": "^X,"}, "fog", top=4,
                                 metric="count")
        self.assertTrue(r.ok)
        words = [c["word"] for c in r.data["top_collocates"]]
        # 'the' filtered by wrapper (STOPWORDS); signal order preserved.
        self.assertEqual(words, ["thick", "morning", "city"])
        self.assertNotIn("metric", r.data)

    def test_explicit_metric_count_no_filter_when_stopwords_off(self):
        """When the caller disables stopword filtering, metric=count
        truly passes through — including 'the'. Locks the
        exclude_stopwords=False contract."""
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw):
            r = word_collocates({"author": "^X,"}, "fog", top=4,
                                 metric="count", exclude_stopwords=False)
        words = [c["word"] for c in r.data["top_collocates"]]
        self.assertEqual(words, ["thick", "morning", "city", "the"])

    def test_w15_default_metric_is_npmi(self):
        """W-15 acceptance: calling word_collocates with no `metric` kwarg
        defaults to NPMI rerank, not raw counts. When marginals are
        readable (mocked) the wrapper writes raw['metric']='npmi' and
        each row carries an `npmi` key — the rendered table can sort
        by association strength instead of frequency.
        """
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            aug.return_value = ([
                {"word": "thick",   "c_pair": 80,  "c_neighbor": 300},
                {"word": "morning", "c_pair": 60,  "c_neighbor": 400},
                {"word": "city",    "c_pair": 40,  "c_neighbor": 250},
                {"word": "the",     "c_pair": 600, "c_neighbor": 8000},
            ], 200, 100_000, 50)
            # NO `metric=` kwarg — new default should be npmi.
            r = word_collocates({"author": "^X,"}, "fog", top=4)
        self.assertTrue(r.ok)
        self.assertEqual(r.data.get("metric"), "npmi")
        for c in r.data["top_collocates"]:
            self.assertIn("npmi", c)
        # 'the' must NOT be on top after NPMI rerank — that was the W-15 bug.
        words = [c["word"] for c in r.data["top_collocates"]]
        self.assertNotEqual(words[0], "the")

    def test_unknown_metric_emits_warning(self):
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw):
            r = word_collocates({"author": "^X,"}, "fog", top=4,
                                 metric="frequency_z_score")
        self.assertTrue(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("metric_unavailable", codes)

    def test_pmi_rerank_changes_order(self):
        """When counts files are accessible (mocked), PMI rerank moves
        common-neighbor 'the' down despite its high raw co-occurrence."""
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            # Hand the wrapper a fully-marginalized candidate set.
            # Scope: N=100000 tokens, target c=200, window=4 (W=8).
            # Expected = 200 * 8 * c_neighbor / 100000
            #   thick   c=300  → E=4.8  vs observed 80 → PMI ≈ log2(80/4.8) ≈ 4.06
            #   morning c=400  → E=6.4  vs observed 60 → PMI ≈ log2(60/6.4) ≈ 3.23
            #   city    c=250  → E=4.0  vs observed 40 → PMI ≈ log2(40/4.0) ≈ 3.32
            #   the     c=8000 → E=128  vs observed 600 → PMI ≈ log2(600/128) ≈ 2.23
            aug.return_value = ([
                {"word": "thick",   "c_pair": 80,  "c_neighbor": 300},
                {"word": "morning", "c_pair": 60,  "c_neighbor": 400},
                {"word": "city",    "c_pair": 40,  "c_neighbor": 250},
                {"word": "the",     "c_pair": 600, "c_neighbor": 8000},
            ], 200, 100_000, 50)
            r = word_collocates({"author": "^X,"}, "fog", top=4,
                                 metric="pmi", min_cooccurrence=10)
        self.assertTrue(r.ok)
        words = [c["word"] for c in r.data["top_collocates"]]
        # 'the' should drop to the bottom even though it had the highest raw count
        self.assertEqual(words[-1], "the")
        # 'thick' should rise to the top (highest PMI)
        self.assertEqual(words[0], "thick")
        # Each row carries the PMI score
        for c in r.data["top_collocates"]:
            self.assertIn("pmi", c)
        self.assertEqual(r.data["metric"], "pmi")
        self.assertEqual(r.data["scope_total_tokens"], 100_000)

    def test_min_cooccurrence_filters_noise(self):
        """Pairs with c_pair < min_cooccurrence drop before scoring."""
        from scripts.v2.tools.words.collocates import word_collocates
        # v1 returns 3 candidates including 2 noise pairs with c=1
        v1_with_noise = {
            **self.v1_raw,
            "top_collocates": [
                {"word": "thick",   "count": 80},
                {"word": "blip1",   "count": 1},
                {"word": "blip2",   "count": 2},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=v1_with_noise), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            aug.return_value = ([
                {"word": "thick", "c_pair": 80, "c_neighbor": 300},
                {"word": "blip1", "c_pair": 1,  "c_neighbor": 5},
                {"word": "blip2", "c_pair": 2,  "c_neighbor": 8},
            ], 200, 100_000, 50)
            r = word_collocates({"author": "^X,"}, "fog", top=10,
                                 metric="pmi", min_cooccurrence=5)
        words = [c["word"] for c in r.data["top_collocates"]]
        self.assertEqual(words, ["thick"])
        self.assertNotIn("blip1", words)

    def test_marginals_unavailable_warns_keeps_v1(self):
        """If _augment_with_marginals returns None (no /workspace), the
        wrapper warns and ships the v1 count-based ordering."""
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=self.v1_raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals", return_value=None):
            r = word_collocates({"author": "^X,"}, "fog", top=4, metric="pmi")
        codes = [w.code for w in r.warnings]
        self.assertIn("marginals_unavailable", codes)
        # Result still has the v1 list (no rerank happened)
        words = [c["word"] for c in r.data["top_collocates"]]
        self.assertEqual(words[0], "thick")

    def test_v1_error_propagates(self):
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value={"error": "bad scope"}):
            r = word_collocates({}, "fog", top=4, metric="pmi")
        self.assertFalse(r.ok)

    def test_dice_and_npmi_also_dispatch(self):
        """All three new metrics resolve to a plugin and reorder."""
        from scripts.v2.tools.words.collocates import word_collocates
        for metric in ("dice", "npmi"):
            with self.subTest(metric=metric):
                with mock.patch("scripts.rag_tools.word_collocates",
                                return_value=self.v1_raw), \
                     mock.patch("scripts.v2.tools.words.collocates."
                                "_augment_with_marginals") as aug:
                    aug.return_value = ([
                        {"word": "thick",   "c_pair": 80,  "c_neighbor": 300},
                        {"word": "morning", "c_pair": 60,  "c_neighbor": 400},
                        {"word": "the",     "c_pair": 600, "c_neighbor": 8000},
                    ], 200, 100_000, 50)
                    r = word_collocates({"author": "^X,"}, "fog", top=3,
                                         metric=metric, min_cooccurrence=5)
                self.assertTrue(r.ok)
                self.assertEqual(r.data["metric"], metric)
                for c in r.data["top_collocates"]:
                    self.assertIn(metric, c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
