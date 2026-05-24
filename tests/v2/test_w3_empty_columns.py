"""W-3 (2026-05-23) — empty-column normalization for LLM render payload.

Cross-cutting prod bug: tools emit list-of-dict rows where some columns
are None across ALL rows (npmi when metric=count, translation when no
enrich, etc.). The LLM dutifully renders every key as a column → user
sees a column of «—» cells.

These tests lock the deterministic strip-before-LLM contract:

  1. `_strip_empty_keys_from_rows` drops keys empty in ALL rows.
  2. Per-row «—» (mixed populated/missing) is PRESERVED — the column
     stays, only the empty cells render as «—». Honest «no data here».
  3. Identity fields (pg_id/word/lemma/author/...) are NEVER stripped
     even when all rows have None — they're semantically required.
  4. `_normalize_payload_tool_results` walks the full payload, descends
     one level into dict-valued keys (compare_authors-style nesting).
  5. Original ToolResult.data is NOT mutated (renderer-only transform).
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class StripEmptyKeysFromRows(unittest.TestCase):
    def test_drops_key_empty_in_all_rows(self):
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"token": "fog", "count": 713, "npmi": None},
            {"token": "mist", "count": 502, "npmi": None},
            {"token": "darkness", "count": 481, "npmi": None},
        ]
        out = _strip_empty_keys_from_rows(rows)
        self.assertEqual(len(out), 3)
        for r in out:
            self.assertNotIn("npmi", r, "all-None column must be stripped")
            self.assertIn("count", r, "populated column must stay")
        # token is identity — stays even if it were None
        self.assertIn("token", out[0])

    def test_keeps_key_with_mixed_populated_and_missing(self):
        """Per-row «—» allowed; only entire column dropped."""
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"token": "a", "npmi": 0.7, "count": 10},
            {"token": "b", "npmi": None, "count": 5},
            {"token": "c", "npmi": 0.3, "count": 3},
        ]
        out = _strip_empty_keys_from_rows(rows)
        # npmi has values in 2/3 rows → stays
        self.assertIn("npmi", out[0])
        self.assertIsNone(out[1].get("npmi"))
        self.assertEqual(out[2]["npmi"], 0.3)

    def test_identity_keys_never_stripped(self):
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"pg_id": None, "word": None, "author": None, "count": 10},
            {"pg_id": None, "word": None, "author": None, "count": 5},
        ]
        out = _strip_empty_keys_from_rows(rows)
        # All three are identity fields — preserved even when None
        self.assertIn("pg_id", out[0])
        self.assertIn("word", out[0])
        self.assertIn("author", out[0])

    def test_internal_underscore_keys_preserved(self):
        """`_word_for_filter` / `_render_note` etc. flow through —
        rag_v2 strips them at the JSON layer, not here."""
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"word": "a", "_internal": None, "count": 10},
            {"word": "b", "_internal": None, "count": 5},
        ]
        out = _strip_empty_keys_from_rows(rows)
        self.assertIn("_internal", out[0])

    def test_empty_string_counts_as_empty(self):
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"token": "a", "definition": "", "count": 1},
            {"token": "b", "definition": "  ", "count": 2},
            {"token": "c", "definition": None, "count": 3},
        ]
        out = _strip_empty_keys_from_rows(rows)
        self.assertNotIn("definition", out[0])

    def test_string_literal_none_counts_as_empty(self):
        """Defense in depth — if a tool stringifies None somewhere."""
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"token": "a", "x": "None", "count": 1},
            {"token": "b", "x": "none", "count": 2},
        ]
        out = _strip_empty_keys_from_rows(rows)
        self.assertNotIn("x", out[0])

    def test_zero_is_not_empty(self):
        """0 is a real value — don't strip count=0 columns."""
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = [
            {"token": "a", "count": 0},
            {"token": "b", "count": 0},
        ]
        out = _strip_empty_keys_from_rows(rows)
        self.assertIn("count", out[0])
        self.assertEqual(out[0]["count"], 0)

    def test_empty_list_input_passes_through(self):
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        self.assertEqual(_strip_empty_keys_from_rows([]), [])

    def test_non_dict_rows_passed_through(self):
        from scripts.v2.rag_v2 import _strip_empty_keys_from_rows
        rows = ["string", 42, None]
        self.assertEqual(_strip_empty_keys_from_rows(rows), rows)


class NormalizeDataForRender(unittest.TestCase):
    def test_strips_top_level_list_columns(self):
        """word_collocates style — `top_collocates` with all-None npmi."""
        from scripts.v2.rag_v2 import _normalize_data_for_render
        data = {
            "word": "fog", "window": 4,
            "top_collocates": [
                {"word": "thick", "count": 100, "npmi": None},
                {"word": "dense", "count": 80, "npmi": None},
            ],
            "metric": "count",
        }
        out = _normalize_data_for_render(data)
        for row in out["top_collocates"]:
            self.assertNotIn("npmi", row)
        # Scalar top-level fields preserved
        self.assertEqual(out["word"], "fog")
        self.assertEqual(out["metric"], "count")

    def test_descends_one_level_into_dict(self):
        """compare_authors nests `top_unique` inside author1 / author2."""
        from scripts.v2.rag_v2 import _normalize_data_for_render
        data = {
            "author1": {
                "regex": "^Doyle,", "slug": "doyle",
                "top_unique": [
                    {"word": "holmes", "affinity": 0.9, "fake_col": None},
                    {"word": "watson", "affinity": 0.8, "fake_col": None},
                ],
            },
            "author2": {
                "regex": "^Wells,", "slug": "wells",
                "top_unique": [
                    {"word": "morlock", "affinity": 0.7, "fake_col": None},
                ],
            },
            "cosine_similarity": 0.12,
        }
        out = _normalize_data_for_render(data)
        for r in out["author1"]["top_unique"]:
            self.assertNotIn("fake_col", r)
        for r in out["author2"]["top_unique"]:
            self.assertNotIn("fake_col", r)
        # Scalars preserved through descent
        self.assertEqual(out["cosine_similarity"], 0.12)
        self.assertEqual(out["author1"]["slug"], "doyle")

    def test_non_dict_input_passes_through(self):
        from scripts.v2.rag_v2 import _normalize_data_for_render
        self.assertEqual(_normalize_data_for_render("string"), "string")
        self.assertEqual(_normalize_data_for_render(None), None)
        self.assertEqual(_normalize_data_for_render(42), 42)
        self.assertEqual(_normalize_data_for_render([1, 2, 3]), [1, 2, 3])

    def test_does_not_mutate_original(self):
        """Renderer-only transform; cache/observability rely on the
        ToolResult.data shape staying intact."""
        from scripts.v2.rag_v2 import _normalize_data_for_render
        data = {
            "top": [
                {"word": "a", "affinity": 0.5, "_legacy_field": None},
                {"word": "b", "affinity": 0.4, "_legacy_field": None},
            ],
        }
        original = copy.deepcopy(data)
        _ = _normalize_data_for_render(data)
        self.assertEqual(data, original,
                         "_normalize must not mutate the input")


class NormalizePayloadToolResults(unittest.TestCase):
    def test_walks_each_tool_result(self):
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        payload = {
            "intent": "word_collocates",
            "tool_results": [
                {
                    "tool": "word_collocates",
                    "data": {
                        "top_collocates": [
                            {"word": "x", "count": 1, "npmi": None},
                            {"word": "y", "count": 2, "npmi": None},
                        ],
                    },
                },
                {
                    "tool": "affinity_by_author",
                    "data": {
                        "top": [
                            {"word": "h", "affinity": 0.9, "stale": None},
                        ],
                    },
                },
            ],
        }
        out = _normalize_payload_tool_results(payload)
        coll = out["tool_results"][0]["data"]["top_collocates"]
        self.assertNotIn("npmi", coll[0])
        aff = out["tool_results"][1]["data"]["top"]
        self.assertNotIn("stale", aff[0])

    def test_preserves_non_data_fields(self):
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        payload = {
            "intent": "x",
            "tool_results": [
                {"tool": "t", "data": {}, "ok": True,
                 "coverage": {"books_matched": 5}},
            ],
            "render_instructions": ["foo"],
        }
        out = _normalize_payload_tool_results(payload)
        self.assertEqual(out["intent"], "x")
        self.assertEqual(out["render_instructions"], ["foo"])
        self.assertEqual(out["tool_results"][0]["ok"], True)
        self.assertEqual(out["tool_results"][0]["coverage"]["books_matched"], 5)

    def test_returns_new_payload(self):
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        original = {"tool_results": [
            {"tool": "t", "data": {"top": [{"word": "a", "x": None}]}},
        ]}
        before = copy.deepcopy(original)
        _ = _normalize_payload_tool_results(original)
        self.assertEqual(original, before)


class IntegrationWithRealToolShapes(unittest.TestCase):
    """End-to-end: tool wrappers stamp rows with the keys they declare;
    normalize strips the all-None columns; LLM sees only meaningful keys.
    These tests construct payloads that mirror real prod shapes."""

    def test_word_collocates_count_metric_strips_npmi(self):
        """Reproduces Stan prod «fog» bug: metric=count → npmi=None for
        every row → LLM rendered «NPMI» column with «—» in every cell.
        After W-3 the column is gone."""
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        payload = {
            "tool_results": [{
                "tool": "word_collocates",
                "data": {
                    "scope": {"book": "PG345"}, "word": "fog", "window": 4,
                    "metric": "count",
                    "top_collocates": [
                        {"word": "thick", "count": 47, "npmi": None},
                        {"word": "dense", "count": 31, "npmi": None},
                        {"word": "yellow", "count": 22, "npmi": None},
                    ],
                },
            }],
        }
        out = _normalize_payload_tool_results(payload)
        rows = out["tool_results"][0]["data"]["top_collocates"]
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertNotIn("npmi", r,
                              "npmi all-None must be stripped pre-LLM")
            # count survives
            self.assertIn("count", r)

    def test_learning_words_without_translation_strips_translation_column(self):
        """V1LearningWords rows don't include translation/example fields.
        Without enrichment, the column is empty everywhere."""
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        payload = {
            "tool_results": [{
                "tool": "learning_words",
                "data": {
                    "scope": {"book": "PG1342"},
                    "level": "intermediate",
                    "top_returned": 3,
                    "results": [
                        {"word": "amiable", "lemma": "amiable", "pos": "ADJ",
                         "scope_count": 12, "corpus_count": 50, "affinity": 0.8,
                         "translation_ru": None, "example": None},
                        {"word": "candour", "lemma": "candour", "pos": "NOUN",
                         "scope_count": 8, "corpus_count": 30, "affinity": 0.7,
                         "translation_ru": None, "example": None},
                        {"word": "civility", "lemma": "civility", "pos": "NOUN",
                         "scope_count": 6, "corpus_count": 25, "affinity": 0.65,
                         "translation_ru": None, "example": None},
                    ],
                },
            }],
        }
        out = _normalize_payload_tool_results(payload)
        rows = out["tool_results"][0]["data"]["results"]
        for r in rows:
            self.assertNotIn("translation_ru", r)
            self.assertNotIn("example", r)
            # word + lemma + pos + counts survive
            self.assertIn("word", r)
            self.assertIn("scope_count", r)

    def test_compare_authors_nested_lists_normalized(self):
        """compare_authors nests `top_unique` inside author1/author2.
        Normalizer must descend one level to find the rows."""
        from scripts.v2.rag_v2 import _normalize_payload_tool_results
        payload = {
            "tool_results": [{
                "tool": "compare_authors",
                "data": {
                    "author1": {
                        "regex": "^Poe,", "slug": "poe",
                        "top_unique": [
                            {"word": "raven", "affinity": 0.9, "fake": None},
                            {"word": "tomb", "affinity": 0.8, "fake": None},
                        ],
                    },
                    "author2": {
                        "regex": "^Lovecraft,", "slug": "lovecraft",
                        "top_unique": [
                            {"word": "cthulhu", "affinity": 0.7, "fake": None},
                        ],
                    },
                    "cosine_similarity": 0.04,
                    "shared_high_affinity": [],
                },
            }],
        }
        out = _normalize_payload_tool_results(payload)
        a1 = out["tool_results"][0]["data"]["author1"]["top_unique"]
        a2 = out["tool_results"][0]["data"]["author2"]["top_unique"]
        for r in a1 + a2:
            self.assertNotIn("fake", r)
            self.assertIn("word", r)
            self.assertIn("affinity", r)
        # Scalars through descent
        self.assertEqual(
            out["tool_results"][0]["data"]["cosine_similarity"], 0.04)


if __name__ == "__main__":
    unittest.main(verbosity=2)
