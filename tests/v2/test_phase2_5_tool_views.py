"""Phase 2.5 — view emission across the remaining ~16 tools.

Smoke per tool: with mocked v1, assert result.view is a RenderableView of
the expected ViewType and result.data_validity is set. Closes the
v5 "every tool emits a view" architectural invariant.

Tools covered:
  - author_metadata, author_profile, author_influences, author_attribution,
    affinity_by_book, top_ngrams_by_author, lexical_diversity
  - book_readability, book_archaic_words, book_emotion_profile,
    top_books_by_downloads, top_books_by_recency, find_book
  - word_etymology, find_words_by_etymology, word_contexts,
    word_contexts_global, word_collocates, word_freq_timeline,
    words_disappearing_after, word_pos_distribution, lemma_profile,
    emotion_collocates
  - enrich_word, export_word_list
  - corpus_overview, corpus_stats_by_author
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.view_types import DataValidity, RenderableView, ViewType


def _assert_view(tc, result, expected_vt):
    tc.assertIsNotNone(result.view, f"{result.tool}: view is None")
    tc.assertIsInstance(result.view, RenderableView)
    tc.assertEqual(result.view.view_type, expected_vt,
                   f"{result.tool}: got {result.view.view_type}")
    tc.assertIsNotNone(result.data_validity)


# =====================================================================
# Authors group
# =====================================================================


class AuthorsViews(unittest.TestCase):
    def test_author_metadata(self):
        fake = {"author": "Doyle, Arthur Conan",
                "year_of_birth_min": 1859, "year_of_death_max": 1930,
                "books_total": 22, "nationality": "GB"}
        with mock.patch("scripts.rag_tools.author_metadata", return_value=fake):
            from scripts.v2.tools.authors.author_metadata import author_metadata
            r = author_metadata("^Doyle,")
        _assert_view(self, r, ViewType.AUTHOR_METADATA)
        self.assertEqual(r.view.payload["birth_year"], 1859)
        self.assertEqual(r.view.payload["death_year"], 1930)

    def test_author_profile(self):
        fake = {"metadata": {"author": "Doyle", "year_of_birth_min": 1859,
                              "year_of_death_max": 1930, "books_total": 22},
                "signature_words": [{"word": "holmes"}, {"word": "watson"}],
                "influences": [{"author": "Stevenson, Robert Louis"}],
                "lexical_diversity": {"ttr": 0.32}}
        with mock.patch("scripts.v2.profiles.author.get_or_build", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_profile
            r = author_profile("^Doyle,")
        _assert_view(self, r, ViewType.AUTHOR_PROFILE)
        self.assertIn("holmes", r.view.payload["signature_words"])

    def test_author_influences(self):
        fake = {"closest": [
            {"author": "Stevenson, Robert Louis", "delta": 0.4385},
            {"author": "Le Fanu", "delta": 0.51},
        ]}
        with mock.patch("scripts.rag_tools.author_influences", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_influences
            r = author_influences("^Doyle,", top=10)
        _assert_view(self, r, ViewType.TOP_N_TABLE)
        rows = r.view.payload["rows"]
        self.assertEqual(rows[0]["author"], "Stevenson, Robert Louis")
        self.assertIn("0.4385", rows[0]["delta"])

    def test_author_attribution(self):
        fake = {"candidates": [
            {"author": "Doyle, Arthur Conan", "delta": 0.32, "books_matched": 22},
            {"author": "Stevenson", "delta": 0.51, "books_matched": 13},
        ]}
        with mock.patch("scripts.rag_tools.author_attribution", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_attribution
            r = author_attribution(text="x" * 600, top=5)
        _assert_view(self, r, ViewType.ATTRIBUTION_RESULT)

    def test_top_ngrams(self):
        fake = {"top_ngrams": [
            {"ngram": "of the", "count": 5000},
            {"ngram": "in the", "count": 3500},
        ], "books_used": 22}
        with mock.patch("scripts.rag_tools.top_ngrams_by_author", return_value=fake):
            from scripts.v2.tools.authors.top_ngrams import top_ngrams_by_author
            r = top_ngrams_by_author("^Doyle,", n=2, top=20)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_lexical_diversity(self):
        fake = {"ttr": 0.32, "books_total": 22, "per_book": []}
        with mock.patch("scripts.rag_tools.lexical_diversity", return_value=fake):
            from scripts.v2.tools.authors.top_ngrams import lexical_diversity
            r = lexical_diversity(scope={"author": "^Doyle,"})
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_affinity_by_book(self):
        # learning_tools has Windows-incompatible relative imports; mock
        # at sys.modules level so we don't need it to load successfully.
        fake = {"top_words": [{"word": "elopement", "affinity": 0.9}],
                "book_title": "Pride and Prejudice"}
        fake_module = mock.MagicMock()
        fake_module.affinity_by_book = mock.MagicMock(return_value=fake)
        with mock.patch.dict(sys.modules,
                              {"scripts.learning_tools": fake_module}):
            from scripts.v2.tools.books.affinity_book import affinity_by_book
            r = affinity_by_book(pg_id="PG1342", top=50)
        _assert_view(self, r, ViewType.TOP_N_TABLE)


# =====================================================================
# Books group
# =====================================================================


class BooksViews(unittest.TestCase):
    def test_book_readability(self):
        fake = {"flesch": 58.8, "flesch_kincaid": 10.9, "cefr": "B2",
                "book_title": "Pride and Prejudice"}
        with mock.patch("scripts.rag_tools.book_readability",
                        return_value=fake):
            from scripts.v2.tools.books.readability import book_readability
            r = book_readability("PG1342")
        _assert_view(self, r, ViewType.READABILITY_SUMMARY)
        self.assertEqual(r.view.payload["flesch"], 58.8)

    def test_book_archaic_words(self):
        fake = {"archaic_words": [
            {"word": "ye", "count": 12},
            {"word": "thou", "count": 8},
        ], "book_title": "Dracula"}
        with mock.patch("scripts.learning_tools.book_archaic_words",
                        return_value=fake):
            from scripts.v2.tools.books.readability import book_archaic_words
            r = book_archaic_words("PG345", top=30)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_book_emotion_profile(self):
        fake = {"emotions": {"fear": 120, "joy": 50, "trust": 80},
                "book_title": "Frankenstein"}
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                        return_value=fake):
            from scripts.v2.tools.books.top_books import book_emotion_profile
            r = book_emotion_profile("PG84")
        _assert_view(self, r, ViewType.EMOTION_PROFILE)
        self.assertEqual(len(r.view.payload["emotions"]), 3)

    def test_top_books_by_downloads(self):
        fake = {"top": [
            {"title": "Frankenstein", "author": "Shelley", "downloads": 5000},
        ]}
        with mock.patch("scripts.rag_tools.top_books_by_downloads",
                        return_value=fake):
            from scripts.v2.tools.books.top_books import top_books_by_downloads
            r = top_books_by_downloads(top=20)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_top_books_by_recency(self):
        fake = {"top": [
            {"title": "Recent Book", "author": "X", "pub_year": 2010},
        ]}
        with mock.patch("scripts.rag_tools.top_books_by_recency",
                        return_value=fake):
            from scripts.v2.tools.books.top_books import top_books_by_recency
            r = top_books_by_recency(top=20, metric="pub_year")
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_find_book(self):
        fake = {"matches": [
            {"id": "PG1342", "title": "Pride and Prejudice",
             "author": "Austen, Jane", "downloads": 12000},
        ], "total_matches": 1, "title_query": "pride"}
        with mock.patch("scripts.rag_tools.find_book", return_value=fake):
            from scripts.v2.tools.books.find_book import find_book
            r = find_book("pride")
        _assert_view(self, r, ViewType.BOOK_LOOKUP)


# =====================================================================
# Words group
# =====================================================================


class WordsViews(unittest.TestCase):
    def test_word_etymology(self):
        fake = {"primary_family": "Germanic",
                "family_chain": ["ang", "gem-pro"]}
        with mock.patch("scripts.rag_tools.word_etymology", return_value=fake):
            from scripts.v2.tools.words.etymology import word_etymology
            r = word_etymology("sword")
        _assert_view(self, r, ViewType.ETYMOLOGY_BUNDLE)
        self.assertEqual(r.view.payload["etymology"]["primary_family"],
                         "Germanic")

    def test_find_words_by_etymology(self):
        fake = {"matched": [
            {"word": "swerd", "corpus_count": 50},
            {"word": "ich",   "corpus_count": 40},
        ], "books_total": 22}
        with mock.patch("scripts.rag_tools.find_words_by_etymology",
                        return_value=fake):
            from scripts.v2.tools.words.etymology import find_words_by_etymology
            r = find_words_by_etymology(scope={"author": "^Tolkien,"},
                                          family="germanic", top=30)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_word_contexts(self):
        fake = {"samples": [
            {"snippet": "She felt her heart pound...",
             "pg_id": "PG1342", "title": "Pride and Prejudice",
             "author": "Austen, Jane"},
        ]}
        with mock.patch("scripts.rag_tools.word_contexts", return_value=fake):
            from scripts.v2.tools.words.contexts import word_contexts
            r = word_contexts("^Austen,", word="heart")
        _assert_view(self, r, ViewType.WORD_CONTEXTS)

    def test_word_contexts_global(self):
        fake = {"samples": [
            {"snippet": "...heart...", "pg_id": "PG1", "title": "X",
             "author": "Y"},
        ]}
        with mock.patch("scripts.rag_tools.word_contexts_global",
                        return_value=fake):
            from scripts.v2.tools.words.contexts import word_contexts_global
            r = word_contexts_global(word="heart")
        _assert_view(self, r, ViewType.WORD_CONTEXTS)

    def test_word_collocates(self):
        fake = {"top_collocates": [
            {"word": "sargasso", "count": 12, "npmi": 0.85},
        ], "books_total": 100}
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=fake):
            from scripts.v2.tools.words.collocates import word_collocates
            r = word_collocates(scope={"author": "^Wodehouse,"},
                                  word="sea", top=20)
        _assert_view(self, r, ViewType.COLLOCATES)

    def test_word_freq_timeline(self):
        fake = {"timeline": [
            {"bucket_start": 1850, "bucket_end": 1875,
             "freq_per_million": 0.5, "occurrences": 50},
            {"bucket_start": 1875, "bucket_end": 1900,
             "freq_per_million": 2.1, "occurrences": 280},
        ], "books_total": 200}
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                        return_value=fake):
            from scripts.v2.tools.words.timeline import word_freq_timeline
            r = word_freq_timeline(word="radio")
        _assert_view(self, r, ViewType.TIMELINE_CHART)

    def test_words_disappearing_after(self):
        fake = {"words": [
            {"word": "betwixt", "drop_factor": 8.5},
            {"word": "ere",     "drop_factor": 6.2},
        ], "books_before": 50, "books_after": 80}
        with mock.patch("scripts.rag_tools.words_disappearing_after",
                        return_value=fake):
            from scripts.v2.tools.words.timeline import words_disappearing_after
            r = words_disappearing_after(year=1920, top=25)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_word_pos_distribution(self):
        fake = {"distribution": {"VERB": 50, "NOUN": 25, "ADJ": 5}}
        with mock.patch("scripts.rag_tools.word_pos_distribution",
                        return_value=fake):
            from scripts.v2.tools.words.pos import word_pos_distribution
            r = word_pos_distribution(scope="all_corpus", word="set")
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_lemma_profile(self):
        from scripts.v2.profiles import lemma as lemma_mod
        fake_profile = {
            "global_count": 50000, "rarity": 0.1, "difficulty": "basic",
            "book_count": 12000,
        }
        with mock.patch.object(lemma_mod, "get_or_build",
                                 return_value=fake_profile):
            from scripts.v2.tools.words.lemma_profile import lemma_profile
            r = lemma_profile("heart")
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_emotion_collocates(self):
        fake = {"top": [
            {"word": "darkness", "count": 25, "npmi": 0.7},
        ], "n_books": 22}
        with mock.patch("scripts.rag_tools.emotion_collocates",
                        return_value=fake):
            from scripts.v2.tools.words.emotion import emotion_collocates
            r = emotion_collocates(scope={"author": "^Poe,"},
                                     emotion="fear", top=25)
        _assert_view(self, r, ViewType.COLLOCATES)


# =====================================================================
# Learning + Corpus + Meta
# =====================================================================


class LearningAndCorpusViews(unittest.TestCase):
    def test_enrich_word(self):
        fake = {"translation_ru": "приоткрытый", "ipa": "əˈdʒɑːr",
                "pos": "ADJ", "definition": "slightly open",
                "primary_family": "Germanic"}
        with mock.patch("scripts.learning_tools.enrich_word",
                        return_value=fake):
            from scripts.v2.tools.learning.enrich import enrich_word
            r = enrich_word("ajar")
        _assert_view(self, r, ViewType.ETYMOLOGY_BUNDLE)
        self.assertTrue(r.view.payload["slots_available"]["translation"])
        self.assertTrue(r.view.payload["slots_available"]["etymology"])

    def test_export_word_list(self):
        fake = {"content": "word,translation\nheart,сердце",
                "filename": "wordcracker_export.csv"}
        with mock.patch("scripts.learning_tools.export_word_list",
                        return_value=fake):
            from scripts.v2.tools.learning.enrich import export_word_list
            r = export_word_list(words=[{"lemma": "heart"}], format="anki_csv")
        _assert_view(self, r, ViewType.EXPORT_ARTIFACT)

    def test_corpus_overview(self):
        # corpus_overview reads filesystem — mock its module-level imports
        from scripts.v2.tools.corpus_meta import overview
        with mock.patch.object(overview.Path, "exists", return_value=False), \
             mock.patch.object(overview, "_pgrep_alive", return_value=False), \
             mock.patch.object(overview, "_read_reindex_progress",
                                return_value=None):
            from scripts.v2.tools.corpus_meta.overview import corpus_overview
            r = corpus_overview()
        _assert_view(self, r, ViewType.CORPUS_META_SNAPSHOT)

    def test_corpus_stats_by_author(self):
        fake = {"books_total": 22, "tokens_total": 1_200_000,
                "vocab_size": 25000, "ttr": 0.31}
        with mock.patch("scripts.rag_tools.corpus_stats_by_author",
                        return_value=fake):
            from scripts.v2.tools.corpus_meta.stats_by_author import \
                corpus_stats_by_author
            r = corpus_stats_by_author("^Doyle,")
        _assert_view(self, r, ViewType.TOP_N_TABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
