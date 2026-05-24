"""W-3 (2026-05-23) — contract: tool output ⊇ typed-view required fields.

For each affected tool, run with a realistic v1 mock and assert the
resulting RenderableView carries all REQUIRED_FIELDS (declared in
view_types.REQUIRED_FIELDS) populated. This is the «view sees real data»
contract — without it, fields silently default to None and the renderer
either hides the column (if W-3 normalizer strips it) or shows «—».

Coverage matches the W-3 task list: affinity_by_author, compare_authors,
learning_words, word_collocates, emotion_collocates, book_emotion_profile,
book_readability, enrich_word, word_freq_timeline.

The test is structural — it doesn't hit real corpus; it pins «if v1
returns its declared shape, the view fields the renderer relies on are
all populated». Locks the failure class that motivated W-3.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ---------------------------------------------------------------------
# Helper — assert view honours REQUIRED_FIELDS for its view_type.
# ---------------------------------------------------------------------

def _assert_view_required_fields(testcase: unittest.TestCase, result):
    testcase.assertIsNotNone(result.view,
                              f"{result.tool}: view must be attached")
    issues = result.view.validate()
    structural = [i for i in issues
                  if not i.startswith("required_field_missing:")]
    testcase.assertEqual(
        structural, [],
        f"{result.tool}: view validate() has structural issues: {structural}",
    )
    missing = result.view.missing_required_fields()
    testcase.assertEqual(
        missing, [],
        f"{result.tool}: view payload is missing required fields {missing}. "
        f"This is the W-3 «column empty in render» class — tool not "
        f"surfacing what the typed-view contract requires.",
    )


# ---------------------------------------------------------------------
# affinity_by_author — TOP_N_TABLE: columns + rows
# ---------------------------------------------------------------------


class AffinityByAuthorViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        v1 = {
            "author_regex": "^Doyle,", "slug": "doyle",
            "effective_min_corpus_count": 500,
            "total_unique_words": 18000, "n_books": 50,
            "top": [
                {"word": "investigate", "author_count": 234,
                 "corpus_count": 1900, "affinity": 0.812},
                {"word": "deduction", "author_count": 178,
                 "corpus_count": 1230, "affinity": 0.756},
                {"word": "elementary", "author_count": 102,
                 "corpus_count": 890, "affinity": 0.701},
            ],
            "cached": False, "proper_noun_filter": "",
        }
        with mock.patch("scripts.rag_tools.affinity_by_author",
                         return_value=v1):
            r = affinity_by_author(author_regex="^Doyle,")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)


# ---------------------------------------------------------------------
# compare_authors — COMPARISON_PANEL: entities + metrics
# ---------------------------------------------------------------------


class CompareAuthorsViewContract(unittest.TestCase):
    def test_v1_real_nested_shape_yields_complete_view(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        v1 = {
            "author1": {
                "regex": "^Poe,", "slug": "poe",
                "top_unique": [
                    {"word": "raven", "affinity": 0.91},
                    {"word": "lenore", "affinity": 0.83},
                ],
            },
            "author2": {
                "regex": "^Lovecraft,", "slug": "lovecraft",
                "top_unique": [
                    {"word": "cthulhu", "affinity": 0.88},
                    {"word": "eldritch", "affinity": 0.71},
                ],
            },
            "shared_high_affinity": [
                {"word": "horror", "affinity_1": 0.7, "affinity_2": 0.8},
            ],
            "cosine_similarity": 0.142,
            "cosine_note": "small corpus, structural near-zero possible",
            "min_corpus_count": 500,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Lovecraft,")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)
        # Specific to comparison_panel: entities must carry the per-entity
        # metric dict so the renderer's Cosine Similarity / Shared High
        # Affinity columns aren't all-«—» (E38 regression class).
        entities = r.view.payload.get("entities") or []
        self.assertEqual(len(entities), 2)
        for e in entities:
            metrics = e.get("metrics") or {}
            # E38 fix — title-cased keys must be present
            self.assertIn("Cosine Similarity", metrics)
            self.assertIn("Shared High Affinity", metrics)


# ---------------------------------------------------------------------
# learning_words — LEARNING_WORDS: words + scope_label
# ---------------------------------------------------------------------


class LearningWordsViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.learning.learning_words import learning_words
        v1 = {
            "scope": "{'book': 'PG1342'}", "level": "intermediate",
            "band_min": 100, "band_max": 10000, "top_n": 30,
            "candidates": 200, "n_books": 1,
            "results": [
                {"word": "amiable", "lemma": "amiable", "pos": "ADJ",
                 "scope_count": 12, "corpus_count": 350, "affinity": 0.34,
                 "score": 0.81},
                {"word": "candour", "lemma": "candour", "pos": "NOUN",
                 "scope_count": 8, "corpus_count": 210, "affinity": 0.39,
                 "score": 0.78},
            ],
        }
        with mock.patch("scripts.learning_tools.learning_words",
                         return_value=v1):
            r = learning_words(scope={"book": "PG1342"})
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)


# ---------------------------------------------------------------------
# word_collocates — COLLOCATES: word + collocates + scope_label
# ---------------------------------------------------------------------


class WordCollocatesViewContract(unittest.TestCase):
    def test_count_metric_yields_complete_view(self):
        from scripts.v2.tools.words.collocates import word_collocates
        v1 = {
            "scope": "{'book': 'PG345'}", "word": "fog", "window": 4,
            "total_occurrences": 200, "books_with_hits": 1,
            "top_collocates": [
                {"word": "thick", "count": 47},
                {"word": "dense", "count": 31},
                {"word": "yellow", "count": 22},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                         return_value=v1):
            r = word_collocates(scope={"book": "PG345"}, word="fog")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)


# ---------------------------------------------------------------------
# emotion_collocates — COLLOCATES (treating emotion as word anchor)
# ---------------------------------------------------------------------


class EmotionCollocatesViewContract(unittest.TestCase):
    def test_v1_shape_yields_complete_view(self):
        from scripts.v2.tools.words.emotion import emotion_collocates
        v1 = {
            "scope": "{'book': 'PG84'}", "emotion": "fear",
            "anchor_pool_in_lexicon": 50, "anchors_in_scope": [],
            "top_collocates": [
                {"word": "trembling", "count": 14},
                {"word": "dread", "count": 11},
                {"word": "darkness", "count": 9},
            ],
            "total_anchor_hits": 34, "anchor_pool_size": 50,
        }
        with mock.patch("scripts.rag_tools.emotion_collocates",
                         return_value=v1):
            r = emotion_collocates(scope={"book": "PG84"}, emotion="fear")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)


# ---------------------------------------------------------------------
# book_emotion_profile — EMOTION_PROFILE: book_title + pg_id + emotions
# ---------------------------------------------------------------------


class BookEmotionProfileViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.books.top_books import book_emotion_profile
        v1 = {
            "id": "PG84", "title": "Frankenstein", "author": "Shelley, Mary",
            "total_tokens": 78231, "emotion_bearing_tokens": 12000,
            "emotion_coverage_pct": 15.3,
            "share_among_primary_emotions": {
                "fear": 0.302, "sadness": 0.201, "anticipation": 0.181,
                "trust": 0.113, "anger": 0.089, "joy": 0.058,
                "disgust": 0.031, "surprise": 0.025,
            },
            "per_million": {
                "fear": 4823.1, "sadness": 3210.4, "anticipation": 2901.7,
                "trust": 1810.5, "anger": 1421.0, "joy": 921.2,
                "disgust": 502.3, "surprise": 388.1,
            },
            "sample_anchor_words": {},
        }
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                         return_value=v1):
            r = book_emotion_profile(pg_id="PG84")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)
        # E24 — count must be populated when per_million is available
        emotions = r.view.payload.get("emotions") or []
        fear = next((e for e in emotions if e["emotion"] == "fear"), None)
        self.assertIsNotNone(fear)
        self.assertIsNotNone(fear["count"],
                              "count must be populated from per_million "
                              "(persona Q4/Q12 «Вхождений» column)")


# ---------------------------------------------------------------------
# book_readability — READABILITY_SUMMARY: book_title + pg_id (CEFR slot)
# ---------------------------------------------------------------------


class BookReadabilityViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.books.readability import book_readability
        v1 = {
            "id": "PG1342", "pg_id": "PG1342",
            "title": "Pride and Prejudice", "author": "Austen, Jane",
            "user_uploaded": False, "sampled_chars": 200000,
            "sentences": 8123, "words": 32100,
            "avg_sentence_length_words": 16.4,
            "avg_syllables_per_word": 1.48,
            "flesch_reading_ease": 78.2,
            "flesch_kincaid_grade": 7.4,
            "cefr_heuristic": "B1",
        }
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value=v1):
            r = book_readability(pg_id="PG1342")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)
        # CEFR slot must be populated (E23)
        self.assertEqual((r.view.payload or {}).get("cefr"), "B1")


# ---------------------------------------------------------------------
# enrich_word — ETYMOLOGY_BUNDLE: word + translation_ru + ipa + pos
# ---------------------------------------------------------------------


class EnrichWordViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.learning.enrich import enrich_word
        v1 = {
            "word": "amiable", "translation_ru": "дружелюбный",
            "translation_en": "friendly", "translation": "дружелюбный",
            "definition_en": "having or showing a friendly nature",
            "definition": "having a friendly nature",
            "pos": "ADJ", "pos_tag": "ADJ",
            "cefr_estimate": "B2", "lemma": "amiable",
            "example_sentence": "She greeted him with an amiable smile.",
            "etymology": "From Old French aimable, from Latin amabilis.",
            "proper_noun": False, "archaic": False,
            "primary_family": "Latin",
            "family_chain": ["English", "Old French", "Latin"],
            "ipa": "ˈeɪmiəbəl",
            "related_forms": [], "cognates": [], "derived_from": [],
            "_cached": False, "_lookup_ms": 5.0,
        }
        with mock.patch("scripts.learning_tools.enrich_word",
                         return_value=v1):
            r = enrich_word(word="amiable")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)


# ---------------------------------------------------------------------
# word_freq_timeline — TIMELINE_CHART: word + series + basis
# ---------------------------------------------------------------------


class WordFreqTimelineViewContract(unittest.TestCase):
    def test_v1_real_shape_yields_complete_view(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        v1 = {
            "word": "fog", "bucket_years": 25, "basis": "auto",
            "axis_basis": "authoryearofbirth+30",
            "timeline": [
                {"period": "1800-1824", "books": 50, "total_tokens": 1_000_000,
                 "occurrences": 142, "per_million": 142.0},
                {"period": "1825-1849", "books": 80, "total_tokens": 2_000_000,
                 "occurrences": 308, "per_million": 154.0},
                {"period": "1850-1874", "books": 120, "total_tokens": 3_000_000,
                 "occurrences": 510, "per_million": 170.0},
            ],
        }
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                         return_value=v1):
            r = word_freq_timeline(word="fog")
        self.assertTrue(r.ok)
        _assert_view_required_fields(self, r)
        # «Частота» column maps to series[*].freq_per_million; ensure it's
        # populated (Stan persona Q12 «timeline factory» rendered «—»)
        series = (r.view.payload or {}).get("series") or []
        self.assertTrue(all(s.get("freq_per_million") is not None
                            for s in series),
                         "every bucket must carry per_million → freq_per_million")


if __name__ == "__main__":
    unittest.main(verbosity=2)
