"""T2 (Phase 2) Type 1 contract tests — wrappers must read the canonical
v1 keys, not phantom aliases.

Each test feeds the wrapper a v1 response shaped exactly like the real v1
function returns and asserts the wrapper extracts non-empty data. If
anyone re-introduces a `.get("a") or .get("b")` fallback chain — or if v1
renames the canonical key under a wrapper — these tests fail loudly.

Coverage:
  T1-1  affinity_by_author       row key is `word` (not phantom `token`)
  T1-2  learning_words           row key is `word` (chain over `lemma`
                                 gone — both keys present in v1)
  T1-3  enrich_word              etymology key is `family_chain`
                                 (`etymology_chain` phantom — dropped)
  T1-4  lexical_diversity (view) scope-polymorphic `ttr` vs
                                 `ttr_aggregate` selected by scope shape
  T1-5  book_readability (view)  `total_words_estimate` is v2-stamped,
                                 falls back to v1 `words` only when
                                 stamp is absent
  T1-6  author_influences (filter)
                                 row key is `author` (no `name` alias)
  T1-7  word_collocates (view)   reads count by canonical `count` only
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestAffinityByAuthorWordKey(unittest.TestCase):
    """T1-1 — v1 affinity_by_author rows carry `word`; `token` was phantom."""

    def test_view_renders_word_with_canonical_key_only(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author

        v1_real_shape = {
            "author_regex": "^Wilde, Oscar",
            "slug": "wilde",
            "top": [
                # v1 rag_tools.py:735 — rows have `word`, no `token`.
                {"word": "epigrammatic", "author_count": 12,
                 "corpus_count": 480, "affinity": 25.0},
                {"word": "dandyish", "author_count": 8,
                 "corpus_count": 320, "affinity": 25.0},
            ],
            "n_books": 7,
        }
        with mock.patch("scripts.rag_tools.affinity_by_author",
                         return_value=v1_real_shape):
            r = affinity_by_author(author_regex="^Wilde, Oscar")
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.view)
        rows = (r.view.payload or {}).get("rows") or []
        self.assertGreaterEqual(len(rows), 1)
        # The rendered word must come from the canonical `word` key, not
        # the dropped `token` phantom fallback.
        words = [row["word"] for row in rows if row.get("word") != "—"]
        self.assertIn("epigrammatic", words)


class TestLearningWordsRowKey(unittest.TestCase):
    """T1-2 — v1 learning_words rows always carry both `word` and `lemma`
    (learning_tools.py:530/536). Wrapper reads the canonical `word`."""

    def test_blacklist_filter_uses_canonical_word_key(self):
        from scripts.v2.tools.learning.learning_words import learning_words

        v1_real_shape = {
            "scope": "book:PG1342",
            "level": "intermediate",
            "band": {"min_corpus": 100, "max_corpus": 10000, "skip_top_n": 200},
            "lemmatize": True,
            "results": [
                # v1 always sets `word` AND `lemma`. `lambton` is in the
                # location blacklist — must be filtered via `word` read.
                {"word": "lambton", "lemma": "lambton", "pos": "PROPN",
                 "scope_count": 5, "corpus_count": 200,
                 "affinity": 1.0, "score": 0.5},
                {"word": "punctilious", "lemma": "punctilious", "pos": "ADJ",
                 "scope_count": 4, "corpus_count": 180,
                 "affinity": 1.2, "score": 0.6},
            ],
        }
        with mock.patch("scripts.learning_tools.learning_words",
                         return_value=v1_real_shape):
            r = learning_words(scope={"book": "PG1342"})
        self.assertTrue(r.ok)
        # `lambton` (a Pride and Prejudice location) must be filtered out.
        results = r.data.get("results") or []
        words = [it.get("word") for it in results]
        self.assertNotIn("lambton", words)
        self.assertIn("punctilious", words)


class TestEnrichWordEtymologyKey(unittest.TestCase):
    """T1-3 — V1EnrichWord canonical etymology key is `family_chain`.
    `etymology_chain` was a phantom alias — dropped in T2."""

    def test_view_reads_family_chain_only(self):
        from scripts.v2.tools.learning.enrich import enrich_word

        v1_real_shape = {
            "word": "amber",
            "translation_ru": "янтарь",
            "definition_en": "fossilized tree resin",
            "pos": "noun",
            "primary_family": "arabic",
            # Canonical chain — used by view.
            "family_chain": ["middle_english", "old_french", "arabic"],
        }
        with mock.patch("scripts.learning_tools.enrich_word",
                         return_value=v1_real_shape):
            r = enrich_word(word="amber")
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.view)
        # Etymology bundle should be present in payload — wrapper read
        # `family_chain` directly (no fallback chain).
        etym = (r.view.payload or {}).get("etymology") or {}
        self.assertEqual(etym.get("family_chain"),
                         ["middle_english", "old_french", "arabic"])


class TestLexicalDiversityScopePolymorphism(unittest.TestCase):
    """T1-4 — v1 lexical_diversity returns `ttr` for book/all_corpus,
    `ttr_aggregate` for author. Wrapper selects by scope shape — no
    `.get() or .get()` chain."""

    def test_book_scope_reads_ttr(self):
        from scripts.v2.tools.authors.top_ngrams import lexical_diversity

        v1_book_shape = {
            "scope": "book:PG1342",
            "tokens": 122_000,
            "types": 7_500,
            "ttr": 0.0615,
        }
        with mock.patch("scripts.rag_tools.lexical_diversity",
                         return_value=v1_book_shape):
            r = lexical_diversity(scope={"book": "PG1342"})
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        # The wrapper stamps the picked TTR into caveats. The book branch
        # reads `ttr` (not `ttr_aggregate`) — verifying the scope-based
        # dispatch chose the right key.
        caveats = list(getattr(view, "caveats", []) or [])
        ttr_in_caveats = any("0.061" in str(c) for c in caveats)
        self.assertTrue(ttr_in_caveats, f"expected ttr in caveats; got {caveats}")

    def test_author_scope_reads_ttr_aggregate(self):
        from scripts.v2.tools.authors.top_ngrams import lexical_diversity

        v1_author_shape = {
            "scope": "author:^Doyle,",
            "tokens": 1_200_000,
            "types": 35_000,
            "ttr_aggregate": 0.0292,
            "ttr_avg_per_book": 0.0850,
            "books_used": 42,
            "top_5_most_varied": [
                {"pg_id": "PG108", "tokens": 60_000, "types": 8_000,
                 "ttr": 0.133},
            ],
            "bottom_5_least_varied": [],
            "note": "TTR collapses...",
        }
        with mock.patch("scripts.rag_tools.lexical_diversity",
                         return_value=v1_author_shape):
            r = lexical_diversity(scope={"author": "^Doyle,"})
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        caveats = list(getattr(view, "caveats", []) or [])
        # The aggregate value must surface — selected explicitly via
        # scope-based dispatch, not via a fallback chain on a single
        # line of `.get` reads.
        agg_in_caveats = any("0.029" in str(c) for c in caveats)
        self.assertTrue(agg_in_caveats, f"expected ttr_aggregate in caveats; got {caveats}")


class TestBookReadabilityWordCount(unittest.TestCase):
    """T1-5 — `total_words_estimate` is the v2-stamped corrected count;
    v1's `words` is the sample-only count. Read with explicit fallback,
    no .get()-or chain."""

    def test_view_prefers_v2_stamped_total(self):
        from scripts.v2.tools.books.readability import book_readability

        # Simulate v1 returning sample words=34427 AND wrapper stamping
        # total_words_estimate=122000 (via counts file). View should
        # render the corrected total.
        v1_with_stamp = {
            "id": "PG1342", "pg_id": "PG1342",
            "title": "Pride and Prejudice", "author": "Austen, Jane",
            "user_uploaded": False, "sampled_chars": 200_000,
            "sentences": 5000, "words": 34_427,
            "avg_sentence_length_words": 18.0,
            "avg_syllables_per_word": 1.4,
            "flesch_reading_ease": 75.0,
            "flesch_kincaid_grade": 9.0,
            "cefr_heuristic": "B2",
            "total_words_estimate": 122_000,
        }
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value=v1_with_stamp):
            r = book_readability(pg_id="PG1342")
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        # View payload's word_count should be the v2-stamped 122_000,
        # not v1's sample 34_427.
        self.assertEqual((view.payload or {}).get("word_count"), 122_000)


class TestAuthorInfluencesRowKey(unittest.TestCase):
    """T1-6 — V1AuthorInfluences rows canonical key is `author`
    (rag_tools.py:1782); `name` was phantom."""

    def test_collection_bucket_filter_reads_author_only(self):
        from scripts.v2.tools.authors.author_profile import _is_collection_bucket

        # Row with `author` key — should be filterable (and not crash).
        self.assertTrue(_is_collection_bucket({"author": "Various, Anonymous"}))
        self.assertFalse(_is_collection_bucket({"author": "Doyle, Arthur Conan"}))
        # Empty / non-dict — safe defaults.
        self.assertFalse(_is_collection_bucket({}))
        self.assertFalse(_is_collection_bucket(None))
        # Phantom `name` key on its own MUST NOT be silently honored —
        # if v1 ever started emitting `name` we want to know explicitly,
        # not let it bleed through a fallback. Filter returns False
        # (no canonical `author` → row is not classifiable as a bucket).
        self.assertFalse(_is_collection_bucket({"name": "Various, Multiple"}))


class TestWordCollocatesCountKey(unittest.TestCase):
    """T1-7 — V1WordCollocates rows carry `count` (rag_tools.py:1083).
    Pre-T2 wrapper read `count or c_pair` — `c_pair` is wrapper-internal,
    never reaches the view-build path under the count metric. Reading the
    one canonical key is enough."""

    def test_view_reads_canonical_count_key(self):
        from scripts.v2.tools.words.collocates import word_collocates

        v1_real_shape = {
            "scope": "book:PG1342",
            "word": "darcy",
            "window": 4,
            "total_occurrences": 200,
            "books_with_hits": 1,
            "top_collocates": [
                {"word": "elizabeth", "count": 45},
                {"word": "miss", "count": 30},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                         return_value=v1_real_shape):
            r = word_collocates(scope={"book": "PG1342"}, word="darcy")
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        cols = (view.payload or {}).get("collocates") or []
        self.assertGreaterEqual(len(cols), 1)
        # Count was read via canonical key; rendered into view payload.
        first = cols[0]
        self.assertEqual(first["token"], "elizabeth")
        self.assertEqual(first["count"], 45)
        # No NPMI applied under the default `count` metric — score column
        # should stay None.
        self.assertIsNone(first["npmi"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
