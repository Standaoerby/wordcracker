"""W-15 polish (2026-05-24) — negative tests for wrapper-level
stopword filter on `word_collocates`.

Production bug report (Stan, 2026-05-24): «collocates of steam»
returned top filled with off / all / under / there / one because:

  * v1 STOPWORDS (~70 tokens, rag_tools.py:75) lacks any of these.
  * v1 word_collocates only filtered against STOPWORDS, never against
    _HIGH_FREQ_NEIGHBOR_DROP (which emotion_collocates already used).
  * The v2 wrapper passed v1 rows straight through to the renderer,
    so any leakage at v1 leaked all the way to the user.

This test pins the new behavior:

  1. v1 word_collocates now ALSO filters against _HIGH_FREQ_NEIGHBOR_DROP
     (mirrors emotion_collocates, single source of truth).
  2. _HIGH_FREQ_NEIGHBOR_DROP now includes off/under/there/back/way/
     every/another/another/first/last (extension matches what actually
     leaked in prod).
  3. The v2 wrapper re-filters as defense-in-depth: even if a stale
     cached row (or a contract drift) re-introduces a stopword, the
     wrapper drops it before NPMI scoring.

If this regresses, top of «steam» collocates fills with noise again.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class W15HighFreqDropExtended(unittest.TestCase):
    """The extended _HIGH_FREQ_NEIGHBOR_DROP set must catch the
    prod-reported leakers. Add a fresh token here when a new one
    leaks in a real conversation — this is the canonical list."""

    def test_set_contains_prod_reported_leakers(self):
        from scripts.rag_tools import _HIGH_FREQ_NEIGHBOR_DROP
        for tok in ("off", "all", "under", "there", "one",
                    "back", "way", "every", "another"):
            self.assertIn(tok, _HIGH_FREQ_NEIGHBOR_DROP,
                          f"{tok!r} must be in _HIGH_FREQ_NEIGHBOR_DROP "
                          f"per W-15 prod report")


class W15WrapperFiltersStopwords(unittest.TestCase):
    """The v2 wrapper drops stopwords from v1's `top_collocates` BEFORE
    NPMI scoring. Mock v1 to return a deliberately polluted list so the
    test is deterministic (production v1 also filters them, but this
    locks the wrapper-layer contract independently)."""

    def _polluted_v1_raw(self) -> dict:
        # Mix of real collocates with the exact stopwords from the
        # W-15 prod report. Wrapper must drop all five.
        return {
            "scope": "author:^Dickens,", "word": "steam", "window": 4,
            "total_occurrences": 500, "books_with_hits": 30,
            "top_collocates": [
                # The five leakers Stan saw in prod top:
                {"word": "off",      "count": 200},
                {"word": "all",      "count": 180},
                {"word": "under",    "count": 160},
                {"word": "there",    "count": 150},
                {"word": "one",      "count": 140},
                # The real signal we want surfaced:
                {"word": "engine",   "count": 90},
                {"word": "pressure", "count": 70},
                {"word": "power",    "count": 60},
            ],
        }

    def test_count_only_path_drops_stopwords(self):
        """Even on the count-only path (no metric rerank), the wrapper
        must drop the stopwords. Critical because dev boxes without
        /workspace fall back to count-only and used to render the
        polluted top intact."""
        from scripts.v2.tools.words.collocates import word_collocates
        raw = self._polluted_v1_raw()
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=raw):
            r = word_collocates({"author": "^Dickens,"}, "steam",
                                top=10, metric="count")
        self.assertTrue(r.ok)
        names = [c["word"] for c in r.data["top_collocates"]]
        for bad in ("off", "all", "under", "there", "one"):
            self.assertNotIn(bad, names,
                              f"{bad!r} must be filtered by the wrapper "
                              f"on the count-only path; got {names}")
        # Real signal survives
        for good in ("engine", "pressure", "power"):
            self.assertIn(good, names)

    def test_npmi_path_drops_stopwords_before_scoring(self):
        """On the NPMI path the filter runs BEFORE _augment_with_marginals,
        so the scoring budget is spent on real collocates and the table
        cannot rank a stopword first via a quirky NPMI score."""
        from scripts.v2.tools.words.collocates import word_collocates
        raw = self._polluted_v1_raw()
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            # Mock returns marginals only for the survivors — verifies
            # wrapper passes a clean candidate set down to augmentation.
            aug.return_value = ([
                {"word": "engine",   "c_pair": 90, "c_neighbor": 2_000},
                {"word": "pressure", "c_pair": 70, "c_neighbor": 1_500},
                {"word": "power",    "c_pair": 60, "c_neighbor": 3_000},
            ], 500, 10_000_000, 30)
            r = word_collocates({"author": "^Dickens,"}, "steam",
                                top=5, metric="npmi")
            # Capture what was passed to augmentation
            args, kwargs = aug.call_args
            sent_candidates = kwargs.get("candidate_rows") or (
                args[2] if len(args) >= 3 else [])
            sent_words = {(c.get("word") or "").lower()
                          for c in sent_candidates}
            for bad in ("off", "all", "under", "there", "one"):
                self.assertNotIn(bad, sent_words,
                                  f"Wrapper must strip {bad!r} BEFORE "
                                  f"calling _augment_with_marginals; "
                                  f"sent={sent_words}")
        self.assertTrue(r.ok)
        names = [c["word"] for c in r.data["top_collocates"]]
        for bad in ("off", "all", "under", "there", "one"):
            self.assertNotIn(bad, names)

    def test_exclude_stopwords_false_skips_wrapper_filter(self):
        """When the caller explicitly asks for raw window (no stopword
        filtering), the wrapper must respect that and not silently
        re-filter. Mirrors v1 behavior."""
        from scripts.v2.tools.words.collocates import word_collocates
        raw = self._polluted_v1_raw()
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=raw):
            r = word_collocates({"author": "^Dickens,"}, "steam",
                                top=10, metric="count",
                                exclude_stopwords=False)
        self.assertTrue(r.ok)
        names = [c["word"] for c in r.data["top_collocates"]]
        # All eight rows pass through when filter disabled
        self.assertIn("off", names)
        self.assertIn("engine", names)

    def test_wrapper_drops_target_word_self_collocate(self):
        """`steam` itself appearing as a collocate of `steam` is noise
        (token-window artifact). Wrapper drops self-collocates."""
        from scripts.v2.tools.words.collocates import word_collocates
        raw = {
            "scope": "author:^X,", "word": "steam", "window": 4,
            "total_occurrences": 200, "books_with_hits": 5,
            "top_collocates": [
                {"word": "steam",  "count": 50},  # self
                {"word": "engine", "count": 40},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=raw):
            r = word_collocates({"author": "^X,"}, "steam", top=5,
                                metric="count")
        names = [c["word"] for c in r.data["top_collocates"]]
        self.assertNotIn("steam", names,
                          "target word must not appear as its own collocate")


class W15WrapperVersionBumped(unittest.TestCase):
    """Sanity: tool metadata pins the new wrapper_version so cached
    entries from the v3 era (which carried the stopword pollution)
    get invalidated on the first read."""

    def test_wrapper_version_pinned_to_v4(self):
        from scripts.v2.tool_registry import REGISTRY
        spec = REGISTRY.get("word_collocates")
        self.assertIsNotNone(spec, "word_collocates not registered")
        self.assertIn("w15", (spec.wrapper_version or ""),
                      f"wrapper_version must reference W-15; "
                      f"got {spec.wrapper_version!r}")
        # Bumped past v3 — v3 had npmi default but no stopword filter,
        # v4 adds the filter, so the version label must reflect that.
        self.assertNotEqual(spec.wrapper_version, "v3-w15-npmi-default",
                            "wrapper_version still on v3 — cache will "
                            "serve stale stopword-laden rows")


if __name__ == "__main__":
    unittest.main(verbosity=2)
