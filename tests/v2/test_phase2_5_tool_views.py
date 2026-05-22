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
        # Phase 2 — V1AuthorMetadata canonical keys: author_regex,
        # books_matched, authors_matched, year_of_birth_min,
        # year_of_death_max. No `books_total` / `nationality`.
        fake = {"author_regex": "^Doyle,",
                "authors_matched": ["Doyle, Arthur Conan"],
                "year_of_birth_min": 1859, "year_of_death_max": 1930,
                "books_matched": 22}
        with mock.patch("scripts.rag_tools.author_metadata", return_value=fake):
            from scripts.v2.tools.authors.author_metadata import author_metadata
            r = author_metadata("^Doyle,")
        _assert_view(self, r, ViewType.AUTHOR_METADATA)
        self.assertEqual(r.view.payload["birth_year"], 1859)
        self.assertEqual(r.view.payload["death_year"], 1930)

    def test_author_profile(self):
        # Phase 2 — V1AuthorProfile composite: metadata/signature/diversity/
        # influences are nested dicts, each with its sub-tool's canonical
        # shape. metadata.books_matched; signature.top; influences.top;
        # diversity.ttr_aggregate.
        fake = {"author_regex": "^Doyle,",
                "metadata": {"authors_matched": ["Doyle"],
                              "year_of_birth_min": 1859,
                              "year_of_death_max": 1930,
                              "books_matched": 22},
                "signature": {"top": [{"word": "holmes"},
                                       {"word": "watson"}]},
                "influences": {"top": [{"author": "Stevenson, Robert Louis",
                                         "delta": 0.4}]},
                "diversity": {"ttr_aggregate": 0.32}}
        with mock.patch("scripts.v2.profiles.author.get_or_build", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_profile
            r = author_profile("^Doyle,")
        _assert_view(self, r, ViewType.AUTHOR_PROFILE)
        self.assertIn("holmes", r.view.payload["signature_words"])

    def test_author_influences(self):
        # Phase 2 — V1AuthorInfluences canonical: pivot_author, top.
        fake = {"pivot_author": "Doyle, Arthur Conan",
                "top": [
                    {"author": "Stevenson, Robert Louis", "delta": 0.4385,
                      "books_in_training": 13},
                    {"author": "Le Fanu", "delta": 0.51,
                      "books_in_training": 8},
                ]}
        with mock.patch("scripts.rag_tools.author_influences", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_influences
            r = author_influences("^Doyle,", top=10)
        _assert_view(self, r, ViewType.TOP_N_TABLE)
        rows = r.view.payload["rows"]
        self.assertEqual(rows[0]["author"], "Stevenson, Robert Louis")
        self.assertIn("0.4385", rows[0]["delta"])

    def test_author_attribution(self):
        # Phase 2 — V1AuthorAttribution canonical: top (not `candidates`),
        # rows {author, delta, books_in_training}.
        fake = {"top": [
            {"author": "Doyle, Arthur Conan", "delta": 0.32,
             "books_in_training": 22},
            {"author": "Stevenson", "delta": 0.51,
             "books_in_training": 13},
        ], "tokens_in_text": 600}
        with mock.patch("scripts.rag_tools.author_attribution", return_value=fake):
            from scripts.v2.tools.authors.author_profile import author_attribution
            r = author_attribution(text="x" * 600, top=5)
        _assert_view(self, r, ViewType.ATTRIBUTION_RESULT)

    def test_top_ngrams(self):
        # Phase 2 — V1TopNgramsByAuthor canonical: `top` (not `top_ngrams`).
        fake = {"author_regex": "^Doyle,",
                "top": [
                    {"ngram": "of the", "count": 5000},
                    {"ngram": "in the", "count": 3500},
                ], "books_used": 22}
        with mock.patch("scripts.rag_tools.top_ngrams_by_author", return_value=fake):
            from scripts.v2.tools.authors.top_ngrams import top_ngrams_by_author
            r = top_ngrams_by_author("^Doyle,", n=2, top=20)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_lexical_diversity(self):
        # Phase 2 — V1LexicalDiversity author-scope canonical:
        # scope, ttr_aggregate, books_used, top_5_most_varied.
        fake = {"scope": "author:^Doyle,", "ttr_aggregate": 0.32,
                "books_used": 22, "top_5_most_varied": []}
        with mock.patch("scripts.rag_tools.lexical_diversity", return_value=fake):
            from scripts.v2.tools.authors.top_ngrams import lexical_diversity
            r = lexical_diversity(scope={"author": "^Doyle,"})
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_affinity_by_book(self):
        # Phase 2 — V1AffinityByBook canonical: pg_id, title, top.
        # learning_tools.py needs `from rag_tools import …` which only
        # resolves when scripts/ is on sys.path — not guaranteed here.
        # Stub at sys.modules level so the wrapper's `from
        # scripts.learning_tools import affinity_by_book` picks our fake.
        fake = {"pg_id": "PG1342", "title": "Pride and Prejudice",
                "top": [{"word": "elopement", "book_count": 4,
                          "corpus_count": 1000, "affinity": 0.9}]}
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
        # Phase 2 — V1BookReadability canonical keys.
        fake = {"pg_id": "PG1342", "title": "Pride and Prejudice",
                "flesch_reading_ease": 58.8,
                "flesch_kincaid_grade": 10.9,
                "cefr_heuristic": "B2"}
        with mock.patch("scripts.rag_tools.book_readability",
                        return_value=fake):
            from scripts.v2.tools.books.readability import book_readability
            r = book_readability("PG1342")
        _assert_view(self, r, ViewType.READABILITY_SUMMARY)
        self.assertEqual(r.view.payload["flesch"], 58.8)

    def test_book_archaic_words(self):
        # Phase 2 — V1BookArchaicWords canonical: id, top (rows with
        # word, book_count, source, note).
        fake = {"id": "PG345", "top": [
            {"word": "ye", "book_count": 12, "source": "seed", "note": ""},
            {"word": "thou", "book_count": 8, "source": "seed", "note": ""},
        ]}
        with mock.patch("scripts.learning_tools.book_archaic_words",
                        return_value=fake):
            from scripts.v2.tools.books.readability import book_archaic_words
            r = book_archaic_words("PG345", top=30)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_book_emotion_profile(self):
        # Phase 2 — V1BookEmotionProfile canonical: id, title,
        # share_among_primary_emotions, per_million, sample_anchor_words.
        fake = {"id": "PG84", "title": "Frankenstein",
                "share_among_primary_emotions":
                    {"fear": 0.5, "joy": 0.2, "trust": 0.3},
                "per_million": {"fear": 120, "joy": 50, "trust": 80}}
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
        # Phase 2 — V1WordsDisappearingAfter canonical: year_cutoff, top
        # (rows: word, pre_per_million, post_per_million, drop_ratio,
        #        pre_count, post_count), pre_bucket, post_bucket.
        fake = {"year_cutoff": 1920, "top": [
            {"word": "betwixt", "drop_ratio": 8.5, "pre_per_million": 50,
             "post_per_million": 5, "pre_count": 5000, "post_count": 590},
            {"word": "ere", "drop_ratio": 6.2, "pre_per_million": 30,
             "post_per_million": 5, "pre_count": 3000, "post_count": 484},
        ], "pre_bucket": {"books": 50, "total_tokens": 100_000},
           "post_bucket": {"books": 80, "total_tokens": 200_000}}
        with mock.patch("scripts.rag_tools.words_disappearing_after",
                        return_value=fake):
            from scripts.v2.tools.words.timeline import words_disappearing_after
            r = words_disappearing_after(year=1920, top=25)
        _assert_view(self, r, ViewType.TOP_N_TABLE)

    def test_word_pos_distribution(self):
        # Phase 2 — V1WordPosDistribution canonical: word, pos_distribution
        # is a LIST of {pos, count, share, samples}.
        fake = {"word": "set", "pos_distribution": [
            {"pos": "VERB", "count": 50, "share": 0.625, "samples": []},
            {"pos": "NOUN", "count": 25, "share": 0.3125, "samples": []},
            {"pos": "ADJ",  "count": 5,  "share": 0.0625, "samples": []},
        ], "total_occurrences": 80}
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
        # Phase 2 — V1EmotionCollocates canonical: emotion, top_collocates
        # (rows: word, count). `top` was phantom alias dropped.
        fake = {"emotion": "fear", "top_collocates": [
            {"word": "darkness", "count": 25},
        ], "anchor_pool_in_lexicon": 22, "anchors_in_scope": []}
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
        # Phase 2 — V1EnrichWord canonical: word, translation_ru, ipa, pos,
        # definition_en (not `definition`), primary_family.
        fake = {"word": "ajar", "translation_ru": "приоткрытый",
                "ipa": "əˈdʒɑːr", "pos": "ADJ",
                "definition_en": "slightly open",
                "primary_family": "Germanic"}
        with mock.patch("scripts.learning_tools.enrich_word",
                        return_value=fake):
            from scripts.v2.tools.learning.enrich import enrich_word
            r = enrich_word("ajar")
        _assert_view(self, r, ViewType.ETYMOLOGY_BUNDLE)
        self.assertTrue(r.view.payload["slots_available"]["translation"])
        self.assertTrue(r.view.payload["slots_available"]["etymology"])

    def test_export_word_list(self):
        # Phase 2 — V1ExportWordList canonical: out_path, format, entries,
        # content. `filename` was phantom; out_path is canonical.
        fake = {"out_path": "wordcracker_export.csv",
                "format": "anki_csv", "entries": 1,
                "content": "word,translation\nheart,сердце"}
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
        # Phase 2 — V1CorpusStatsByAuthor canonical: author_regex,
        # books_matched (not `books_total`), total_tokens (not
        # `tokens_total`), unique_words (not `vocab_size`).
        fake = {"author_regex": "^Doyle,",
                "books_matched": 22, "total_tokens": 1_200_000,
                "unique_words": 25000, "avg_book_length_words": 54545}
        with mock.patch("scripts.rag_tools.corpus_stats_by_author",
                        return_value=fake):
            from scripts.v2.tools.corpus_meta.stats_by_author import \
                corpus_stats_by_author
            r = corpus_stats_by_author("^Doyle,")
        _assert_view(self, r, ViewType.TOP_N_TABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
