"""W-13 / W-14 (Phase 5 P2 polish, 2026-05-24) — wrapper-internal latency
bench for `find_book_by_topic`.

End-to-end acceptance for «что почитать после "Преступления и наказания"»
is wall-clock < ~60s on warm cache. The bulk of that budget is spent in
v1 hybrid_search + BGE rerank — bounded structurally by W-13 (per_retriever
= 30, k = max(top*3, 30)).

This file pins the OTHER side of the budget: the wrapper's own
post-processing (rerank-threshold filter → dedup_by_key → dedup_book_editions
→ META blocklist → view emission). When v1 returns the maximum candidate
pool the wrapper allows under W-13, the wrapper itself must not add
measurable wall-clock — anything > ~0.5s here is a structural regression.

Also covers W-14: with a worst-case noise floor of META-doc rows, the
blocklist must still drop them in linear time and surface the `dedup`
warning. The fiction-vs-meta split is repeated here as a smoke check in
addition to the explicit blocklist precision tests in
`test_w14_book_similar_meta_filter.py`.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult


# 0.5s is conservative — on a cold Python import + view-builder path the
# wrapper measured at ~0.05-0.15s on a modest dev box for top=10. Setting
# the gate to 0.5s gives generous headroom for CI variance while still
# catching the kind of regression that would turn the wrapper itself into
# a meaningful chunk of the 60s end-to-end budget.
_WRAPPER_OVERHEAD_BUDGET_S = 0.5


def _fake_hybrid_search(chunks):
    return ToolResult.success(
        tool="hybrid_search",
        data={"matches": chunks, "reranked_by": "bge_reranker"},
        coverage=Coverage(books_matched=len(chunks), books_total=-1),
        query={"query": "bench"},
    )


def _mk_fiction_chunk(i: int) -> dict:
    return {
        "pg_id": f"PGFIC{i:04d}",
        "title": f"Fictional Title #{i}",
        "author": f"Author {i}",
        "rrf_score": 0.9 - (i / 1000.0),
        "rerank_score": 0.85 - (i / 1000.0),
        "snippet": f"unique snippet body number {i} — gothic horror prose.",
    }


def _mk_meta_chunk(i: int) -> dict:
    # Rotate through several blocklist phrases so the test exercises
    # multiple META branches in `_is_meta_title`.
    meta_titles = [
        f"Project Gutenberg ({1971 + (i % 30)}-{2009 + (i % 5)})",
        f"Catalogue of London Books, {1850 + (i % 50)}",
        f"Greek Mythology Stories Volume {i}",
        f"History of English Literature, Vol. {i}",
        f"Anthology of English Poetry — Series {i}",
        f"Bibliography of Romantic Verse {i}",
        f"Manual of English Literature, Edition {i}",
    ]
    title = meta_titles[i % len(meta_titles)]
    return {
        "pg_id": f"PGMETA{i:04d}",
        "title": title,
        "author": "various",
        "rrf_score": 0.95 - (i / 1000.0),
        "rerank_score": 0.92 - (i / 1000.0),
        "snippet": f"meta-doc body {i} — index of titles, bibliography excerpt.",
    }


class WrapperOverheadIsBounded(unittest.TestCase):
    """When v1 returns the worst-case candidate pool W-13 allows
    (per_retriever=30 → wrapper requests k=max(top*3, 30)=30 chunks),
    the wrapper's own pipeline must not add measurable wall-clock to
    the 60s end-to-end budget."""

    def test_wrapper_overhead_under_500ms_on_typical_load(self):
        # 30 chunks is the W-13 pool size for top=10. Mix of fiction +
        # meta + duplicates so every filter stage actually runs.
        chunks = (
            [_mk_fiction_chunk(i) for i in range(20)]
            + [_mk_meta_chunk(i) for i in range(8)]
            + [_mk_fiction_chunk(0), _mk_fiction_chunk(1)]  # snippet dups
        )
        hybrid_result = _fake_hybrid_search(chunks)
        with patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                   return_value=hybrid_result):
            from scripts.v2.tools.books.find_book_by_topic import (
                find_book_by_topic,
            )
            t0 = time.perf_counter()
            result = find_book_by_topic(
                topic="gothic horror like Dracula",
                top=10, translate=False,
            )
            elapsed = time.perf_counter() - t0
        self.assertTrue(result.ok, f"wrapper failed: {result.error}")
        self.assertLess(
            elapsed, _WRAPPER_OVERHEAD_BUDGET_S,
            f"wrapper overhead {elapsed*1000:.0f}ms exceeds "
            f"{_WRAPPER_OVERHEAD_BUDGET_S*1000:.0f}ms budget "
            f"— W-13/W-14 end-to-end 60s budget assumes wrapper "
            f"post-processing is negligible.",
        )

    def test_wrapper_overhead_bounded_under_heavy_noise(self):
        """Worst case: v1 returns a flood (100 chunks) where most are
        meta-docs. Blocklist + dedup must still terminate well within
        budget — linear scan with frozenset/substring checks should be
        near-instant even at 10× the typical pool size."""
        chunks = (
            [_mk_meta_chunk(i) for i in range(70)]
            + [_mk_fiction_chunk(i) for i in range(30)]
        )
        hybrid_result = _fake_hybrid_search(chunks)
        with patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                   return_value=hybrid_result):
            from scripts.v2.tools.books.find_book_by_topic import (
                find_book_by_topic,
            )
            t0 = time.perf_counter()
            result = find_book_by_topic(
                topic="gothic horror like Dracula",
                top=10, per_retriever=100, translate=False,
            )
            elapsed = time.perf_counter() - t0
        self.assertTrue(result.ok, f"wrapper failed: {result.error}")
        # 100-chunk pool is 3.3× the typical W-13 load; budget scales
        # roughly linearly so 1.0s is still ample headroom.
        self.assertLess(
            elapsed, 1.0,
            f"wrapper overhead at 100-chunk noise floor: {elapsed*1000:.0f}ms",
        )
        # The dedup warning must be present — the blocklist DID fire
        # (70 meta-doc rows), and the codepath must surface that fact.
        codes = {w.code for w in result.warnings}
        self.assertIn(
            "dedup", codes,
            f"meta_blocklist drops at heavy noise floor should surface "
            f"via dedup warning; got {codes!r}",
        )


class WrapperPipelineDropsMetaWhenFlooded(unittest.TestCase):
    """Smoke check for the W-13 + W-14 union: when v1 returns a flood
    of META rows alongside legit fiction, the wrapper still returns
    fiction in the topN and the LLM render payload has no META-doc
    titles. Mirrors the «что почитать после Дракулы» repro shape."""

    def test_meta_flood_does_not_displace_fiction_in_top(self):
        # 5 fiction + 25 meta — meta outnumbers fiction 5:1, BUT fiction
        # must still win the topN because meta gets dropped before truncation.
        chunks = (
            [_mk_meta_chunk(i) for i in range(15)]
            + [_mk_fiction_chunk(i) for i in range(5)]
            + [_mk_meta_chunk(i + 100) for i in range(10)]
        )
        hybrid_result = _fake_hybrid_search(chunks)
        with patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                   return_value=hybrid_result):
            from scripts.v2.tools.books.find_book_by_topic import (
                find_book_by_topic,
            )
            result = find_book_by_topic(
                topic="thematic neighbours of Dracula",
                top=10, translate=False,
            )
        self.assertTrue(result.ok, f"wrapper failed: {result.error}")
        titles = [b.get("title") for b in (result.data or {}).get("matches", [])]
        meta_in_top = [t for t in titles if t and any(
            phrase in t.lower()
            for phrase in ("project gutenberg", "catalogue of",
                            "greek mythology", "history of english",
                            "anthology of english", "bibliography of",
                            "manual of english")
        )]
        self.assertEqual(
            meta_in_top, [],
            f"META-doc titles leaked into recommendation top: {meta_in_top}",
        )
        # And fiction MUST be present — otherwise we accidentally over-
        # blocked something.
        fiction_in_top = [t for t in titles if t and t.startswith("Fictional Title")]
        self.assertGreater(
            len(fiction_in_top), 0,
            f"no fiction survived in top after META filter; titles={titles!r}",
        )


class CacheabilityPreservesTheWarmCacheGate(unittest.TestCase):
    """W-13 acceptance: «< ~60s на тёплом кэше». For the warm-cache half
    to hold, `find_book_by_topic` must remain `cacheable=True` in the
    registry. A drift to `False` here would silently turn every repeat
    query into a cold-cache hybrid_search + BGE rerank cycle — exactly
    the failure mode W-13 closes."""

    def test_find_book_by_topic_is_cacheable(self):
        import scripts.v2.tools  # ensure tools imported (R-23)
        from scripts.v2.tool_registry import REGISTRY
        entry = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(entry, "find_book_by_topic must be registered")
        self.assertTrue(
            getattr(entry, "cacheable", False),
            "find_book_by_topic must stay cacheable=True for the warm-cache "
            "side of the W-13 <60s budget to hold.",
        )

    def test_word_freq_timeline_is_cacheable(self):
        """W-13 mentioned word_freq_timeline (50s) as the secondary
        offender on the heavy-query path. cacheable=True is the cheapest
        win — warm cache turns the 50s call into <100ms."""
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY
        entry = REGISTRY.get("word_freq_timeline")
        self.assertIsNotNone(entry)
        self.assertTrue(
            getattr(entry, "cacheable", False),
            "word_freq_timeline must stay cacheable=True to keep "
            "follow-up timeline queries inside the chat budget.",
        )

    def test_learning_words_is_cacheable(self):
        """Same logic for learning_words (158s on the W-13 trace).
        Without caching, every export-to-markdown follow-up re-runs the
        full lemmatize+POS+CEFR pipeline."""
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY
        entry = REGISTRY.get("learning_words")
        self.assertIsNotNone(entry)
        self.assertTrue(
            getattr(entry, "cacheable", False),
            "learning_words must stay cacheable=True so follow-up "
            "queries re-use the heavy first call.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
