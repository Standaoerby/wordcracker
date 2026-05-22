"""E15 — v1↔v2 wrapper contract: each v2 wrapper must read the actual
key v1 returns, not invented aliases.

ROOT CAUSE PATTERN (same class as B-R14-7 / E9 / E14b):
v1 functions in scripts/rag_tools.py + scripts/learning_tools.py +
scripts/v2/tools/... have evolved their output keys over years. v2
wrappers were written against the spec at-the-time and stopped being
audited. Result: many wrappers read keys that don't exist, fall
through to «empty» branch, and emit EMPTY_EXPECTED views that look
correct in tests but produce blank panels in prod.

This test suite injects mocked v1 responses with the ACTUAL prod
shape (sampled by reading scripts/rag_tools.py:* return statements
on 2026-05-22) and asserts the wrapper extracts non-empty rows.
If any v1 changes shape again, these tests break loudly.

Wrappers covered in this fix:
  P0-1  emotion_collocates       v1 returns `top_collocates` (NOT `top`)
  P0-2  top_ngrams_by_author     v1 returns `top` (NOT `top_ngrams`)
  P0-3  book_archaic_words view  v1 returns `top` (NOT `archaic_words/top_words/words`)
  P0-4  book_emotion_profile     v1 returns `share_among_primary_emotions` +
                                 `per_million` (NOT `emotions/profile/distribution`)
  P0-5  word_pos_distribution    v1 returns `pos_distribution` as LIST
                                 (NOT dict)
  P0-6  words_disappearing_after v1 returns `top` (NOT `words`); buckets
                                 nested under `pre_bucket/post_bucket`
                                 (NOT flat `books_before/after`)
  P0-7  compare_authors          v1 returns NESTED `author1.top_unique`
                                 (NOT flat `top_unique_a`); no
                                 `burrows_delta/books_a/b`
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestEmotionCollocatesContract(unittest.TestCase):
    """v1 emotion_collocates returns `top_collocates`, not `top`.
    Source: scripts/rag_tools.py:2052."""

    def test_reads_v1_top_collocates_key(self):
        from scripts.v2.tools.words.emotion import emotion_collocates

        v1_real_shape = {
            "scope": {"book": "PG1342"},
            "emotion": "fear",
            "window": 4,
            "n_books": 1,
            "top_collocates": [
                {"word": "darkness", "npmi": 0.42, "count": 12},
                {"word": "haunted", "npmi": 0.38, "count": 8},
            ],
        }
        with mock.patch("scripts.rag_tools.emotion_collocates",
                         return_value=v1_real_shape):
            r = emotion_collocates(scope={"book": "PG1342"}, emotion="fear")
        self.assertTrue(r.ok)
        # Critical: wrapper must extract from `top_collocates`
        self.assertFalse(any(w.code == "no_collocates" for w in r.warnings),
                         "wrapper failed to read v1 `top_collocates` key")


class TestTopNgramsByAuthorContract(unittest.TestCase):
    """v1 top_ngrams_by_author returns `top`, not `top_ngrams`.
    Source: scripts/rag_tools.py:597."""

    def test_reads_v1_top_key(self):
        from scripts.v2.tools.authors.top_ngrams import top_ngrams_by_author

        v1_real_shape = {
            "author_regex": "^Dickens,",
            "n": 2,
            "books_used": 12,
            "top": [
                {"ngram": "old man", "count": 142},
                {"ngram": "young lady", "count": 98},
            ],
        }
        with mock.patch("scripts.rag_tools.top_ngrams_by_author",
                         return_value=v1_real_shape):
            r = top_ngrams_by_author(author_regex="^Dickens,", n=2)
        self.assertTrue(r.ok)
        # Wrapper writes both `top` and `top_ngrams` for downstream consistency
        rows = r.data.get("top") or r.data.get("top_ngrams") or []
        self.assertEqual(len(rows), 2, "wrapper failed to read v1 `top` key")
        # No empty warning
        self.assertFalse(any(w.code == "empty_top" for w in r.warnings))


class TestBookArchaicWordsContract(unittest.TestCase):
    """v1 book_archaic_words returns `top`.
    Source: scripts/learning_tools.py:777."""

    def test_view_reads_v1_top_key(self):
        from scripts.v2.tools.books.readability import book_archaic_words

        v1_real_shape = {
            "pg_id": "PG345",
            "book_title": "Dracula",
            "top": [
                {"word": "thither", "count": 42},
                {"word": "whence", "count": 28},
                {"word": "morrow", "count": 19},
            ],
        }
        with mock.patch("scripts.learning_tools.book_archaic_words",
                         return_value=v1_real_shape):
            r = book_archaic_words(pg_id="PG345")
        self.assertTrue(r.ok)
        # View must be non-empty (NOT EMPTY_EXPECTED) — wrapper read v1's
        # `top` key correctly.
        view = r.view
        self.assertIsNotNone(view)
        # data_validity should be OK, not EMPTY_*
        self.assertEqual(r.data_validity.name, "OK",
                          "view fell through to EMPTY because wrapper "
                          "didn't read v1's `top` key")


class TestBookEmotionProfileContract(unittest.TestCase):
    """v1 book_emotion_profile returns `share_among_primary_emotions` +
    `per_million`, not `emotions/profile/distribution`.
    Source: scripts/rag_tools.py:1900-1908."""

    def test_view_reads_share_among_primary_emotions(self):
        from scripts.v2.tools.books.top_books import book_emotion_profile

        v1_real_shape = {
            "id": "PG84",
            "title": "Frankenstein",
            "author": "Shelley, Mary",
            "total_tokens": 78231,
            "emotion_bearing_tokens": 6112,
            "emotion_coverage_pct": 7.8,
            "per_million": {
                "fear": 4823.1, "sadness": 3210.4, "anticipation": 2901.7,
                "anger": 1421.0, "trust": 1810.5, "joy": 921.2,
                "disgust": 502.3, "surprise": 388.1,
            },
            "share_among_primary_emotions": {
                "fear": 0.302, "sadness": 0.201, "anticipation": 0.181,
                "trust": 0.113, "anger": 0.089, "joy": 0.058,
                "disgust": 0.031, "surprise": 0.024,
            },
            "sample_anchor_words": {"fear": ["dread", "terror"]},
        }
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                         return_value=v1_real_shape):
            r = book_emotion_profile(pg_id="PG84")
        self.assertTrue(r.ok)
        # View must extract emotions from share_among_primary_emotions
        # (or per_million as fallback).
        # Sanity: the view should not be empty.
        self.assertIsNotNone(r.view)
        emotions = (r.view.payload or {}).get("emotions") or []
        self.assertGreaterEqual(
            len(emotions), 5,
            "view didn't extract emotions from v1's "
            "share_among_primary_emotions/per_million keys",
        )

    def test_view_falls_back_to_per_million_when_share_absent(self):
        from scripts.v2.tools.books.top_books import book_emotion_profile

        v1_partial = {
            "id": "PG84", "title": "Frankenstein",
            "per_million": {"fear": 4823.1, "sadness": 3210.4, "joy": 921.2},
        }
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                         return_value=v1_partial):
            r = book_emotion_profile(pg_id="PG84")
        self.assertTrue(r.ok)
        emotions = (r.view.payload or {}).get("emotions") or []
        self.assertEqual(len(emotions), 3)


class TestWordPosDistributionContract(unittest.TestCase):
    """v1 word_pos_distribution returns `pos_distribution` as a LIST of
    dicts, not as a dict.
    Source: scripts/rag_tools.py:1649."""

    def test_view_reads_list_shape(self):
        from scripts.v2.tools.words.pos import word_pos_distribution

        v1_real_shape = {
            "scope": "author:^Wilde,",
            "word": "light",
            "total_occurrences": 142,
            "max_occurrences": 200,
            "pos_distribution": [
                {"pos": "NOUN", "count": 89, "share": 0.627, "samples": []},
                {"pos": "ADJ",  "count": 38, "share": 0.268, "samples": []},
                {"pos": "VERB", "count": 15, "share": 0.106, "samples": []},
            ],
        }
        with mock.patch("scripts.rag_tools.word_pos_distribution",
                         return_value=v1_real_shape):
            r = word_pos_distribution(scope={"author": "^Wilde,"},
                                       word="light")
        self.assertTrue(r.ok)
        self.assertEqual(r.data_validity.name, "OK",
                          "wrapper fell through to EMPTY because it "
                          "couldn't handle pos_distribution as a list")
        rows = (r.view.payload or {}).get("rows") or []
        self.assertEqual(len(rows), 3)
        # Each row should have pos + share + count
        self.assertEqual(rows[0]["pos"], "NOUN")

    def test_view_still_handles_dict_shape_for_legacy_mocks(self):
        from scripts.v2.tools.words.pos import word_pos_distribution

        # Older test fixtures used a dict — must still work
        v1_legacy = {
            "scope": "book:PG1342", "word": "duty",
            "pos_distribution": {"NOUN": 45, "VERB": 12},
        }
        with mock.patch("scripts.rag_tools.word_pos_distribution",
                         return_value=v1_legacy):
            r = word_pos_distribution(scope={"book": "PG1342"},
                                       word="duty")
        self.assertTrue(r.ok)
        rows = (r.view.payload or {}).get("rows") or []
        self.assertEqual(len(rows), 2)


class TestWordsDisappearingContract(unittest.TestCase):
    """v1 words_disappearing_after returns `top`, not `words`; bucket
    counts under `pre_bucket/post_bucket` (with `books` field), not
    flat `books_before/books_after`.
    Source: scripts/rag_tools.py:1375-1383."""

    def test_reads_v1_top_key_and_nested_buckets(self):
        from scripts.v2.tools.words.timeline import words_disappearing_after

        v1_real_shape = {
            "year_cutoff": 1920,
            "basis": "auto",
            "pre_bucket":  {"books": 5000, "total_tokens": 142_000_000},
            "post_bucket": {"books": 5000, "total_tokens": 98_000_000},
            "min_pre_per_million": 50.0,
            "top": [
                {"word": "thither", "pre_per_million": 142.3,
                 "post_per_million": 2.1, "drop_ratio": 67.8,
                 "pre_count": 20208, "post_count": 206},
                {"word": "whence", "pre_per_million": 98.4,
                 "post_per_million": 3.4, "drop_ratio": 28.9,
                 "pre_count": 13973, "post_count": 333},
            ],
        }
        with mock.patch("scripts.rag_tools.words_disappearing_after",
                         return_value=v1_real_shape):
            r = words_disappearing_after(year=1920)
        self.assertTrue(r.ok)
        # View must show 2 rows from v1's `top` key
        rows = (r.view.payload or {}).get("rows") or []
        self.assertEqual(len(rows), 2,
                          "wrapper didn't read v1's `top` key")
        # Coverage warning should include the bucket book counts
        coverage_warnings = [w for w in r.warnings if w.code == "coverage"]
        self.assertEqual(len(coverage_warnings), 1)
        self.assertIn("5000", coverage_warnings[0].message)


class TestCompareAuthorsContract(unittest.TestCase):
    """v1 compare_authors returns NESTED shape (author1.top_unique,
    author2.top_unique) + cosine_similarity + shared_high_affinity,
    NOT flat top_unique_a / top_unique_b / burrows_delta / books_a/b.
    Source: scripts/rag_tools.py:857-866."""

    def test_normalizes_nested_v1_shape(self):
        from scripts.v2.tools.authors.affinity import compare_authors

        v1_real_shape = {
            "author1": {
                "regex": "^Poe,",
                "slug": "poe-edgar-allan",
                "top_unique": [
                    {"word": "raven", "affinity": 18.4},
                    {"word": "ulalume", "affinity": 12.1},
                ],
            },
            "author2": {
                "regex": "^Lovecraft,",
                "slug": "lovecraft-h-p",
                "top_unique": [
                    {"word": "cthulhu", "affinity": 22.0},
                    {"word": "eldritch", "affinity": 15.3},
                ],
            },
            "shared_high_affinity": [],
            "cosine_similarity": 0.012,
            "cosine_note": "low cosine...",
            "min_corpus_count": 100,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1_real_shape):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Lovecraft,",
                                 min_corpus_count=100)
        self.assertTrue(r.ok)
        # The wrapper normalizes nested → flat aliases
        self.assertEqual(len(r.data.get("top_unique_a") or []), 2,
                          "compare_authors didn't lift nested "
                          "author1.top_unique → flat top_unique_a")
        self.assertEqual(len(r.data.get("top_unique_b") or []), 2)
        # Slugs preserved
        self.assertEqual(r.data.get("slug_a"), "poe-edgar-allan")
        # cosine_is_structural_zero flag set (cosine < 0.05)
        self.assertTrue(r.data.get("cosine_is_structural_zero"))
        # No empty-side warnings (both sides have data)
        empty_warnings = [w for w in r.warnings
                           if w.code.endswith("_empty")]
        self.assertEqual(len(empty_warnings), 0)

    def test_one_side_empty_nested(self):
        from scripts.v2.tools.authors.affinity import compare_authors

        v1_nested_one_empty = {
            "author1": {"regex": "^Poe,", "slug": "poe",
                         "top_unique": [{"word": "raven", "affinity": 18.4}]},
            "author2": {"regex": "^Unknown,", "slug": "unknown",
                         "top_unique": []},  # empty side
            "shared_high_affinity": [], "cosine_similarity": 0.0,
            "cosine_note": "", "min_corpus_count": 100,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1_nested_one_empty):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Unknown,",
                                 min_corpus_count=100)
        self.assertTrue(r.ok)
        # author2_empty warning should fire
        codes = [w.code for w in r.warnings]
        self.assertIn("author2_empty", codes)

    def test_flat_test_mock_still_works(self):
        """Backwards-compat: pre-E15 mocks use flat keys directly."""
        from scripts.v2.tools.authors.affinity import compare_authors

        v1_legacy_flat = {
            "top_unique_a": [{"word": "raven", "affinity": 18.4}],
            "top_unique_b": [{"word": "cthulhu", "affinity": 22.0}],
            "shared_high_affinity": [],
            "cosine_similarity": 0.5,
            "min_corpus_count": 100,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1_legacy_flat):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Lovecraft,",
                                 min_corpus_count=100)
        self.assertTrue(r.ok)
        # Flat shape passes through unmodified
        self.assertEqual(len(r.data["top_unique_a"]), 1)
        self.assertEqual(len(r.data["top_unique_b"]), 1)


class TestAffinityByAuthorWordsAlias(unittest.TestCase):
    """Phase 0 — affinity_by_author exposes `words` alias for plan-spec
    `$s2.words[N]` interpolation.

    Negative test (R2): before Phase 0 the LLM-planner emitted
    `$s2.words[0]` and the wrapper had no `words` key in its output,
    so the ref returned None and the literal placeholder leaked into
    the renderer (audit scenario 1). After the alias landed, ref
    resolves to the filtered row.
    """

    def test_words_key_exposed_alongside_top(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author

        v1_top_shape = {
            "top": [
                {"word": "ernest", "affinity": 1.4, "author_count": 12},
                {"word": "dorian", "affinity": 1.2, "author_count": 9},
            ],
            "n_books": 7,
        }
        with mock.patch("scripts.rag_tools.affinity_by_author",
                         return_value=v1_top_shape):
            r = affinity_by_author(author_regex="^Wilde, Oscar")
        self.assertTrue(r.ok)
        # The original v1 key stays.
        self.assertIn("top", r.data)
        # NEW Phase 0 alias.
        self.assertIn("words", r.data)
        # words mirrors the (possibly filtered) top list.
        self.assertEqual(r.data["words"], r.data["top"])

    def test_words_is_empty_list_when_v1_returns_no_rows(self):
        """Edge case: even with no rows, `words` must be an empty list,
        never absent or None. Otherwise plan-spec refs trip on a missing
        key instead of degrading to an empty fan-out.
        """
        from scripts.v2.tools.authors.affinity import affinity_by_author

        with mock.patch("scripts.rag_tools.affinity_by_author",
                         return_value={"top": [], "n_books": 0}):
            r = affinity_by_author(author_regex="^Unknown,")
        # ok=True with empty rows is a valid degraded-result state in
        # this wrapper (it returns a TOP_N_TABLE empty-state view).
        self.assertIn("words", r.data)
        self.assertEqual(r.data["words"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
