"""Cache view/data_validity roundtrip — v3.3.1 ROOT CAUSE FIX.

Stage 3 prod silent failure 2026-05-21 — every render_v5 call returned
ERROR_FRIENDLY "no_views_in_results". Diagnosis traced to
cache._from_payload silently dropping `view` and `data_validity` fields
when reconstructing ToolResult from disk JSON. Every cache HIT served
a ToolResult with view=None, even when the original tool result had
emitted a typed view.

These tests lock in the fix:
  1. RenderableView.from_dict roundtrips view_type / payload / caveats /
     empty_state / provenance / language
  2. EmptyState.from_dict roundtrips reason (enum) + messages
  3. Provenance.from_dict roundtrips lists + dicts
  4. cache.cache_put → cache_get roundtrip preserves .view + .data_validity
  5. CACHE_SCHEMA_VERSION bump invalidates pre-v5 entries (no view field)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import cache as cache_mod
from scripts.v2 import view_builders as vb
from scripts.v2._types import Coverage, SourceInfo, ToolResult, ToolWarning
from scripts.v2.view_types import (
    DataValidity, EmptyReason, EmptyState, Provenance,
    RenderableView, ViewType,
)


class RenderableViewFromDict(unittest.TestCase):
    def test_roundtrip_top_n_table(self):
        original = vb.build_top_n_table(
            rows=[{"rank": 1, "word": "ajar"}, {"rank": 2, "word": "ere"}],
            columns=["rank", "word"],
            headline="Archaic words",
            requested_n=2,
            language="ru",
        )
        d = original.to_dict()
        restored = RenderableView.from_dict(d)
        self.assertEqual(restored.view_type, ViewType.TOP_N_TABLE)
        self.assertEqual(restored.payload, original.payload)
        self.assertEqual(restored.headline, original.headline)
        self.assertEqual(restored.language, original.language)

    def test_roundtrip_with_empty_state(self):
        original = vb.build_top_n_table(
            rows=[], columns=["x"],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Все слова отфильтрованы.",
            empty_message_en="All filtered.",
            empty_filters_applied={"min_corpus_count": 5000},
            empty_suggestion="Снизить порог.",
        )
        d = original.to_dict()
        restored = RenderableView.from_dict(d)
        self.assertIsNotNone(restored.empty_state)
        self.assertEqual(restored.empty_state.reason, EmptyReason.FILTERED_OUT)
        self.assertIn("отфильтрованы", restored.empty_state.message_ru)
        self.assertEqual(restored.empty_state.filters_applied,
                         {"min_corpus_count": 5000})
        self.assertEqual(restored.empty_state.suggestion, "Снизить порог.")

    def test_roundtrip_with_provenance(self):
        prov = vb.make_provenance(
            requested={"top_n": 100, "level": "B2"},
            returned={"count": 19},
            filtered={"propn_removed": 81},
            sources=["SPGC-2018-07-18"],
            notes=["after_filter_count_honesty"],
        )
        original = vb.build_top_n_table(
            rows=[{"x": 1}], columns=["x"], requested_n=100,
            provenance=prov,
        )
        d = original.to_dict()
        restored = RenderableView.from_dict(d)
        self.assertIsNotNone(restored.provenance)
        self.assertEqual(restored.provenance.requested["top_n"], 100)
        self.assertEqual(restored.provenance.filtered["propn_removed"], 81)
        self.assertIn("SPGC-2018-07-18", restored.provenance.sources)


class EmptyStateFromDict(unittest.TestCase):
    def test_known_reason_string(self):
        es = EmptyState.from_dict({
            "reason": "tool_broken",
            "message_ru": "broken",
            "message_en": "broken",
        })
        self.assertEqual(es.reason, EmptyReason.TOOL_BROKEN)

    def test_unknown_reason_falls_back(self):
        """Defensive: if cache has stale enum value (e.g. from a renamed
        EmptyReason), don't crash — use NO_SIGNAL_EXPECTED as fallback."""
        es = EmptyState.from_dict({
            "reason": "made_up_reason_doesnt_exist",
            "message_ru": "x", "message_en": "x",
        })
        self.assertEqual(es.reason, EmptyReason.NO_SIGNAL_EXPECTED)


# =====================================================================
# Cache roundtrip — the actual prod failure case
# =====================================================================


class CacheViewRoundtrip(unittest.TestCase):
    """Stage 3 silent failure 2026-05-21 — cache_get returned ToolResult
    with view=None. Fix in cache._from_payload restores view + data_validity."""

    def setUp(self):
        # Isolated tmp cache directory
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_root = cache_mod.CACHE_ROOT
        cache_mod.CACHE_ROOT = Path(self.tmp.name)
        cache_mod.cache_clear()

    def tearDown(self):
        cache_mod.CACHE_ROOT = self._saved_root
        cache_mod.cache_clear()
        self.tmp.cleanup()

    def test_view_survives_cache_roundtrip(self):
        """The structural fix: tool emits view → cache_put → cache_get →
        view is restored as RenderableView (not None)."""
        view = vb.build_top_n_table(
            rows=[{"rank": 1, "word": "ajar"}],
            columns=["rank", "word"],
            headline="Test",
            requested_n=1,
        )
        original = ToolResult.success(
            tool="probe", data={"top": [{"word": "ajar"}]},
            coverage=Coverage(books_matched=1, books_total=1),
            source_info=SourceInfo(corpus_version="v1", analytics_version="v1"),
        )
        vb.attach_view(original, view, data_validity=DataValidity.OK)

        # Clear LRU so we test disk roundtrip
        cache_mod.cache_clear()
        cache_mod.cache_put("probe", {"x": 1}, original)
        cache_mod.cache_clear()    # force disk reload
        restored = cache_mod.cache_get("probe", {"x": 1})

        self.assertIsNotNone(restored)
        # THE bug: previously this was None
        self.assertIsNotNone(
            restored.view,
            "Stage 3 root cause regression — view lost across cache roundtrip"
        )
        self.assertEqual(restored.view.view_type, ViewType.TOP_N_TABLE)
        self.assertEqual(restored.view.payload["rows"][0]["word"], "ajar")
        self.assertEqual(restored.data_validity, DataValidity.OK)

    def test_empty_state_survives_roundtrip(self):
        """B-R14-3 closure depends on this — empty COMPARISON_PANEL with
        empty_state must survive cache."""
        view = vb.build_comparison_panel(
            entities=[], metrics=[],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Сравнение пустое: фильтр слишком строгий.",
            empty_message_en="Comparison empty.",
            empty_filters_applied={"min_corpus_count": 2000},
        )
        result = ToolResult.success(
            tool="compare_authors", data={"top_unique_a": [], "top_unique_b": []},
            coverage=Coverage(books_matched=0, books_total=-1),
            source_info=SourceInfo(corpus_version="v1", analytics_version="v1"),
        )
        vb.attach_view(result, view, data_validity=DataValidity.EMPTY_UNEXPECTED)

        cache_mod.cache_clear()
        cache_mod.cache_put("compare_authors", {"a": "x", "b": "y"}, result)
        cache_mod.cache_clear()
        restored = cache_mod.cache_get("compare_authors", {"a": "x", "b": "y"})

        self.assertIsNotNone(restored.view)
        self.assertEqual(restored.view.view_type, ViewType.COMPARISON_PANEL)
        self.assertIsNotNone(restored.view.empty_state)
        self.assertEqual(restored.view.empty_state.reason,
                         EmptyReason.FILTERED_OUT)
        self.assertIn("фильтр", restored.view.empty_state.message_ru)
        self.assertEqual(restored.data_validity, DataValidity.EMPTY_UNEXPECTED)

    def test_pre_v5_cache_entries_unreachable(self):
        """v3.3.1 — CACHE_SCHEMA_VERSION="v2-views" folded into cache_key.
        Old "v1"-schema entries (with no view field) get different hash →
        unreachable → forced re-compute. Prevents stale view=None
        ToolResults from prod cache that was filled before Phase 2.5."""
        # Write a fake "old-schema" entry directly to disk, bypassing
        # current cache_key (simulating pre-v3.3.1 cache file).
        import json
        from scripts.v2 import corpus_version
        fake_old_key = "probe:0000000000000000"
        fake_path = cache_mod.CACHE_ROOT / "probe" / "00" / f"{fake_old_key}.json"
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        fake_path.write_text(json.dumps({
            "ok": True, "tool": "probe", "query": {"x": 1},
            "data": {"top": [{"word": "stale"}]},
            "_cached_at": 1234567890,
            "_corpus_version": "v1",
            # No "view" / "data_validity" — like pre-Phase-2.5 cache
        }))
        # Now try to look up with current cache_key — should miss
        # (different hash because of schema version)
        result = cache_mod.cache_get("probe", {"x": 1})
        self.assertIsNone(
            result,
            "Pre-v5 cache entries should be unreachable after schema bump",
        )


class SchemaVersionBumping(unittest.TestCase):
    def test_schema_version_set(self):
        self.assertEqual(cache_mod.CACHE_SCHEMA_VERSION, "v2-views")

    def test_schema_version_in_cache_key(self):
        """The hash MUST depend on CACHE_SCHEMA_VERSION. Bumping the
        constant should change keys for every tool/args combo."""
        key1 = cache_mod.cache_key("probe", {"x": 1})
        with mock.patch.object(cache_mod, "CACHE_SCHEMA_VERSION", "v9-fake"):
            key2 = cache_mod.cache_key("probe", {"x": 1})
        self.assertNotEqual(key1, key2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
