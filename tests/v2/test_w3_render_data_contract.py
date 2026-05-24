"""W-3 follow-up (2026-05-24) — LLM-render data contract per tool.

The existing test_w3_tool_view_field_contract.py pins the TYPED-view
contract: `ToolResult.view.payload` carries the fields the
template_executor templates read. That covers the typed-rendering path.

The LIVE path is `_llm_render` — it sends `ToolResult.data` (NOT the
typed view) to the LLM. So a wrapper can satisfy the typed-view contract
and STILL leave the LLM render with «—» on every cell, because the LLM
only iterates dict keys on `data` — it can't see the typed view.

These tests pin the renderer-data contract: for each tool whose user-
facing columns regressed in the 2026-05-24 prod run, assert that the
`ToolResult.data` (what the LLM sees, after `_normalize_data_for_render`)
carries either:

  * a list-of-rows where every renderer-relevant column is populated, OR
  * the column is stripped by `_strip_empty_keys_from_rows` so the LLM
    cannot render a dash-filled column.

Symptom coverage (from the W-3 follow-up TZ):
  - book_emotion       → «Вхождения» column populated
  - word_freq_timeline → «Частота» column populated
  - word_collocates    → «NPMI» column populated when reranked, stripped otherwise
  - compare_authors    → «Cosine / Shared Affinity» entities row populated
  - author_metadata    → typed view present on bare-name resolve path
  - learning_words     → «Перевод/Пример» columns either populated or
                          explicitly declared absent (no dash column)
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# =====================================================================
# Helpers
# =====================================================================


def _normalize_for_llm(tool_name: str, data) -> dict:
    """Apply the same payload normalization the renderer applies before
    handing data to the LLM. Mirrors `_llm_render` semantics."""
    from scripts.v2.rag_v2 import _normalize_payload_tool_results
    payload = {"tool_results": [{"tool": tool_name, "data": data}]}
    out = _normalize_payload_tool_results(payload)
    return out["tool_results"][0]["data"]


# =====================================================================
# book_emotion_profile — «Вхождения» (count) column populated
# =====================================================================


class BookEmotionDataContract(unittest.TestCase):
    def test_data_exposes_emotion_rows_with_count(self):
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
        # Raw `data` must include an `emotions` list with count per row.
        emotions = (r.data or {}).get("emotions")
        self.assertIsInstance(emotions, list, "data['emotions'] must exist")
        self.assertEqual(len(emotions), 8)
        for row in emotions:
            self.assertIn("emotion", row)
            self.assertIn("share", row)
            self.assertIsNotNone(row["count"],
                                  f"count must be populated for {row['emotion']}")
        # After LLM normalize, the count column survives (not stripped).
        norm = _normalize_for_llm("book_emotion_profile", r.data)
        for row in norm["emotions"]:
            self.assertIn("count", row,
                          "count column survives normalization "
                          "(LLM will render «Вхождений»)")

    def test_data_exposes_emotion_rows_when_only_per_million(self):
        """v1 sometimes ships per_million WITHOUT share (e.g. when the
        share computation tripped). Row builder must still populate
        share (recomputed) AND count."""
        from scripts.v2.tools.books.top_books import book_emotion_profile
        v1 = {
            "id": "PG345", "title": "Dracula", "author": "Stoker, Bram",
            "total_tokens": 100000, "emotion_bearing_tokens": 12000,
            "emotion_coverage_pct": 12.0,
            "share_among_primary_emotions": None,
            "per_million": {
                "fear": 5000.0, "sadness": 2500.0, "joy": 500.0,
            },
            "sample_anchor_words": {},
        }
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                         return_value=v1):
            r = book_emotion_profile(pg_id="PG345")
        self.assertTrue(r.ok)
        emotions = (r.data or {}).get("emotions")
        self.assertIsInstance(emotions, list)
        self.assertEqual(len(emotions), 3)
        # Sorted descending by share
        self.assertEqual(emotions[0]["emotion"], "fear")
        for row in emotions:
            self.assertIsNotNone(row["share"])
            self.assertIsNotNone(row["count"])


# =====================================================================
# compare_authors — «Cosine Similarity / Shared High Affinity» row
# =====================================================================


class CompareAuthorsDataContract(unittest.TestCase):
    def test_data_exposes_per_author_entities_with_metrics(self):
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
            "cosine_note": "small corpus",
            "min_corpus_count": 500,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Lovecraft,")
        self.assertTrue(r.ok)
        entities = (r.data or {}).get("entities")
        self.assertIsInstance(entities, list,
                               "data['entities'] must be a list of rows")
        self.assertEqual(len(entities), 2)
        for row in entities:
            self.assertIn("name", row)
            # Renderer reads these per-row directly
            self.assertEqual(row["cosine_similarity"], 0.142)
            self.assertEqual(row["shared_high_affinity"], 1)
            self.assertGreaterEqual(row["signature_words_count"], 1)
        # After normalize: each entity row keeps cosine + shared columns
        norm = _normalize_for_llm("compare_authors", r.data)
        for row in norm["entities"]:
            self.assertIn("cosine_similarity", row)
            self.assertIn("shared_high_affinity", row)


# =====================================================================
# word_freq_timeline — «Частота» (freq_per_million) column populated
# =====================================================================


class WordFreqTimelineDataContract(unittest.TestCase):
    def test_data_timeline_rows_carry_freq_per_million_and_count(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        v1 = {
            "word": "telegraph", "bucket_years": 25, "basis": "auto",
            "axis_basis": "authoryearofbirth+30",
            "timeline": [
                {"period": "1800-1824", "books": 30, "total_tokens": 500_000,
                 "occurrences": 12, "per_million": 24.0},
                {"period": "1825-1849", "books": 60, "total_tokens": 1_200_000,
                 "occurrences": 89, "per_million": 74.17},
                {"period": "1850-1874", "books": 90, "total_tokens": 2_000_000,
                 "occurrences": 210, "per_million": 105.0},
            ],
        }
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                         return_value=v1):
            r = word_freq_timeline(word="telegraph")
        self.assertTrue(r.ok)
        timeline = (r.data or {}).get("timeline") or []
        self.assertEqual(len(timeline), 3)
        for row in timeline:
            # Renderer-friendly aliases must be populated on every row.
            self.assertIsNotNone(row.get("freq_per_million"))
            self.assertIsNotNone(row.get("count"))
            # Period label has en-dash (display ready), not the raw v1 ASCII
            self.assertIn("–", row.get("period", ""))
        norm = _normalize_for_llm("word_freq_timeline", r.data)
        for row in norm["timeline"]:
            self.assertIn("freq_per_million", row)
            self.assertIn("count", row)


# =====================================================================
# word_collocates — «NPMI» column populated when reranked, else stripped
# =====================================================================


class WordCollocatesDataContract(unittest.TestCase):
    def test_count_metric_strips_npmi_after_normalize(self):
        """metric=count → no rerank → rows have no npmi key → normalize
        strips it. LLM will not render an NPMI column."""
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
            r = word_collocates(scope={"book": "PG345"}, word="fog",
                                  metric="count")
        self.assertTrue(r.ok)
        norm = _normalize_for_llm("word_collocates", r.data)
        for row in norm["top_collocates"]:
            self.assertNotIn("npmi", row,
                              "no npmi when metric=count — column must be absent")
            self.assertIn("count", row)


# =====================================================================
# learning_words — column contract surfaced in data
# =====================================================================


class LearningWordsDataContract(unittest.TestCase):
    def test_data_declares_render_columns_and_note(self):
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
        # Render-column allow-list surfaced so the LLM can't invent
        # «Перевод» / «Пример» columns when user query mentions them.
        cols = (r.data or {}).get("_render_columns") or []
        self.assertIn("word", cols)
        self.assertIn("lemma", cols)
        self.assertIn("level", cols)
        self.assertNotIn("translation", cols)
        self.assertNotIn("translation_ru", cols)
        self.assertNotIn("example", cols)
        note = (r.data or {}).get("_render_note") or ""
        self.assertIn("COLUMNS", note)
        # Rows themselves carry no translation/example keys — so the
        # LLM render normalizer keeps them stripped.
        for row in (r.data or {}).get("results") or []:
            self.assertNotIn("translation_ru", row)
            self.assertNotIn("example", row)


# =====================================================================
# author_metadata — typed view always attached (bare-name path)
# =====================================================================


class AuthorMetadataViewAlwaysAttached(unittest.TestCase):
    def test_marlowe_christopher_resolves_to_typed_view(self):
        """Bare-name «Marlowe, Christopher» → author_metadata tool runs
        with author_regex='^Marlowe,'; view must be attached so the
        renderer never falls into the dead «no typed view» branch."""
        from scripts.v2.tools.authors.author_metadata import author_metadata
        v1 = {
            "author_regex": "^Marlowe,", "books_matched": 7,
            "authors_matched": ["Marlowe, Christopher"],
            "year_of_birth_min": 1564, "year_of_death_max": 1593,
            "total_downloads": 12000, "languages": ["en"],
            "sample_titles": [
                "Doctor Faustus", "Tamburlaine the Great",
                "The Jew of Malta",
            ],
        }
        with mock.patch("scripts.rag_tools.author_metadata",
                         return_value=v1):
            r = author_metadata(author_regex="^Marlowe,")
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.view, "AUTHOR_METADATA view must be attached")
        self.assertEqual(r.view.view_type.value, "author_metadata")
        self.assertEqual(r.view.payload.get("author_canonical"),
                          "Marlowe, Christopher")
        self.assertEqual(r.view.payload.get("birth_year"), 1564)
        self.assertEqual(r.view.payload.get("death_year"), 1593)


if __name__ == "__main__":
    unittest.main(verbosity=2)
