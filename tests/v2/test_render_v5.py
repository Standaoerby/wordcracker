"""render_v5 — Phase 3+4 integration tests.

Coverage:
  - select_primary_view priority + tie-breakers
  - render_v5 happy path (skeleton only — Phase B off)
  - render_v5 with terminal CLARIFY / OUT_OF_SCOPE / NOT_FOUND views
  - render_v5 with BROKEN data_validity → ERROR_FRIENDLY fallback
  - render_v5 with Phase B prose binding (mocked LLM)
  - meta dict shape — used by RequestTrace + log_request
"""
from __future__ import annotations

import json
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import prose_binder as pb
from scripts.v2 import render_v5 as r5
from scripts.v2 import view_builders as vb
from scripts.v2._types import ToolResult, Coverage
from scripts.v2.view_types import (
    DataValidity, EmptyReason, RenderableView, ViewType,
)


def _result_with_view(tool: str, view: RenderableView,
                      validity: DataValidity = DataValidity.OK) -> ToolResult:
    r = ToolResult.success(tool=tool, data={})
    r.view = view
    r.data_validity = validity
    return r


# =====================================================================
# View selection
# =====================================================================


class SelectPrimaryView(unittest.TestCase):
    def test_picks_final_shape_over_support(self):
        """Final-shape view (READABILITY_SUMMARY) wins over support
        view (BOOK_LOOKUP) from a chained find_book."""
        find_book_v = vb.build_book_lookup(
            book={"pg_id": "PG1342", "title": "Pride and Prejudice"},
        )
        readability_v = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        results = [
            _result_with_view("find_book", find_book_v),
            _result_with_view("book_readability", readability_v),
        ]
        _, primary = r5.select_primary_view(results)
        self.assertEqual(primary.view_type, ViewType.READABILITY_SUMMARY)

    def test_skips_broken_views(self):
        broken_v = vb.build_learning_words(
            words=[], requested_level="B2", scope_label="PG1342",
            is_broken=True,
        )
        readable_v = vb.build_top_n_table(
            rows=[{"rank": 1, "x": "y"}], columns=["rank", "x"],
        )
        results = [
            _result_with_view("learning_words", broken_v,
                               validity=DataValidity.BROKEN),
            _result_with_view("top_authors_by", readable_v),
        ]
        _, primary = r5.select_primary_view(results)
        self.assertIsNotNone(primary)
        self.assertEqual(primary.view_type, ViewType.TOP_N_TABLE)

    def test_prefers_non_empty(self):
        empty_v = vb.build_top_n_table(
            rows=[], columns=["x"],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="empty", empty_message_en="empty",
        )
        nonempty_v = vb.build_top_n_table(
            rows=[{"x": 1}], columns=["x"], requested_n=1,
        )
        results = [
            _result_with_view("a", empty_v),
            _result_with_view("b", nonempty_v),
        ]
        _, primary = r5.select_primary_view(results)
        self.assertFalse(primary.is_empty())

    def test_no_views_returns_none(self):
        r = ToolResult.success(tool="x", data={})    # no view
        _, primary = r5.select_primary_view([r])
        self.assertIsNone(primary)

    def test_empty_terminal_view_with_explanation_wins_over_probe(self):
        """Stage 3 prod test 2026-05-21 — «сравни По и Лавкрафта» plan is:
          step 1: author_metadata(Poe)       — probe
          step 2: author_metadata(Lovecraft) — probe
          step 3: compare_authors(...)       — REAL answer

        When compare_authors returns empty (no signature words at default
        min_corpus_count=2000), it emits COMPARISON_PANEL with empty_state
        explaining «filter too strict». Previously this view was penalized
        -200 → renderer picked AUTHOR_METADATA probe (Poe's metadata) as
        "primary", which is off-topic for a comparison query. The
        empty_state IS the answer — it explains why compare failed."""
        from scripts.v2.view_types import EmptyReason
        probe_view = vb.build_author_metadata(
            author_canonical="Poe, Edgar Allan",
            birth_year=1809, death_year=1849,
            nationality="US", books_in_corpus=22,
        )
        compare_empty = vb.build_comparison_panel(
            entities=[], metrics=[],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru=("Сравнение не построено: ни у По, ни у "
                              "Лавкрафта нет фирменных слов при "
                              "min_corpus_count=2000."),
            empty_message_en="Comparison empty: filter too strict.",
            empty_filters_applied={"min_corpus_count": 2000},
        )
        results = [
            _result_with_view("author_metadata", probe_view),
            _result_with_view("author_metadata", probe_view),
            _result_with_view("compare_authors", compare_empty),
        ]
        _, primary = r5.select_primary_view(results)
        self.assertEqual(
            primary.view_type, ViewType.COMPARISON_PANEL,
            "comparison_panel with empty_state must beat probe AUTHOR_METADATA — "
            "the empty_state IS the answer",
        )

    def test_truly_junk_empty_still_penalized(self):
        """Empty view WITHOUT empty_state (rare — builder enforces it) is
        still penalized. Non-empty AUTHOR_METADATA wins."""
        from scripts.v2.view_types import RenderableView, ViewType
        junk = RenderableView(
            view_type=ViewType.TOP_N_TABLE,
            payload={},        # truly empty
            empty_state=None,  # no explanation
        )
        legit = vb.build_author_metadata(
            author_canonical="Doyle", birth_year=1859, death_year=1930,
            nationality="GB", books_in_corpus=22,
        )
        results = [
            _result_with_view("x", junk),
            _result_with_view("author_metadata", legit),
        ]
        _, primary = r5.select_primary_view(results)
        self.assertEqual(primary.view_type, ViewType.AUTHOR_METADATA)


# =====================================================================
# render_v5 — happy paths
# =====================================================================


class RenderV5HappyPath(unittest.TestCase):
    def test_skeleton_only_no_phase_b(self):
        """Phase A only (prose disabled) — returns deterministic markdown."""
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        results = [_result_with_view("book_readability", view)]
        answer, meta = r5.render_v5(
            "how hard is P&P?", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        self.assertIn("Pride and Prejudice", answer)
        self.assertIn("58.8", answer)
        self.assertEqual(meta["view_type"], "readability_summary")
        self.assertFalse(meta["prose_used"])
        self.assertGreater(meta["skeleton_chars"], 0)
        self.assertIsNone(meta["fallback_reason"])

    def test_with_phase_b_prose(self):
        """Phase B prose binding — clean LLM output passes audit."""
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        results = [_result_with_view("book_readability", view)]
        # Mock LLM returning payload-safe prose
        def fake_llm(sp, up, /, *, timeout_s=8.0):
            return json.dumps({
                "intro": "Pride and Prejudice at B2 level — Flesch 58.8.",
                "next_steps": ["Try another book at this level?"],
            })
        # Force Phase B on regardless of env
        with mock.patch.object(pb, "V5_PROSE_BINDER_ENABLED", True):
            answer, meta = r5.render_v5(
                "how hard?", plan=None, results=results,
                model="x", ollama_host="x",
                llm_call=fake_llm, enable_prose=True,
            )
        self.assertIn("Pride and Prejudice", answer)
        self.assertIn("at B2 level", answer)
        self.assertIn("Что ещё", answer)    # next-steps section
        self.assertTrue(meta["prose_used"])

    def test_phase_b_dropped_on_fabrication(self):
        """LLM hallucinates Bronte — Phase B drops prose, skeleton stays."""
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        results = [_result_with_view("book_readability", view)]
        def fake_llm(sp, up, /, *, timeout_s=8.0):
            return json.dumps({
                "intro": "Pride and Prejudice was published in 1813 by Bronte.",
                "next_steps": [],
            })
        with mock.patch.object(pb, "V5_PROSE_BINDER_ENABLED", True):
            answer, meta = r5.render_v5(
                "how hard?", plan=None, results=results,
                model="x", ollama_host="x",
                llm_call=fake_llm, enable_prose=True,
            )
        # Skeleton remained
        self.assertIn("Pride and Prejudice", answer)
        self.assertIn("58.8", answer)
        # Fabricated prose did NOT leak
        self.assertNotIn("1813", answer)
        self.assertNotIn("Bronte", answer)
        # Meta reports the drop
        self.assertTrue(meta["prose_audit_failed"])
        self.assertTrue(any("1813" in f or "Bronte" in f
                            for f in meta["verification_failures"]))


# =====================================================================
# render_v5 — fallback paths
# =====================================================================


class RenderV5Fallbacks(unittest.TestCase):
    def test_clarify_view_terminal(self):
        clarify_v = vb.build_clarify(
            question_ru="Какого автора имел в виду?",
            alternatives=["Doyle", "Conan Doyle"],
        )
        results = [_result_with_view("resolve_author_name", clarify_v)]
        answer, meta = r5.render_v5(
            "найди doyle", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        self.assertIn("Какого автора", answer)
        self.assertEqual(meta["view_type"], "clarify")
        self.assertEqual(meta["fallback_reason"], "terminal_view:clarify")

    def test_out_of_scope_terminal(self):
        oos_v = vb.build_out_of_scope(
            reason_kind="copyright",
            why_ru="«1984» под копирайтом.",
            what_ru=["Мета через Gutendex"],
        )
        results = [_result_with_view("plan", oos_v)]
        answer, meta = r5.render_v5(
            "уровень 1984", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        self.assertIn("копирайтом", answer)
        self.assertIn("Gutendex", answer)
        self.assertEqual(meta["fallback_reason"], "terminal_view:out_of_scope")

    def test_not_found_terminal(self):
        nf_v = vb.build_not_found(
            entity_type="author", query="Xyz",
            message_ru="Не нашёл.",
        )
        results = [_result_with_view("resolve_author_name", nf_v)]
        answer, meta = r5.render_v5(
            "Xyz info", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        self.assertIn("Не нашёл", answer)
        self.assertEqual(meta["view_type"], "not_found")

    def test_broken_tool_falls_to_error_friendly(self):
        """B-R14-7 closure path: when any tool is BROKEN, renderer
        emits ERROR_FRIENDLY instead of pretending all is well."""
        broken_view = vb.build_learning_words(
            words=[], requested_level="B2",
            scope_label="Pride and Prejudice (PG1342)",
            is_broken=True,
            broken_reason="learning_words B2 returned 0 across 50 books",
        )
        results = [
            _result_with_view("learning_words", broken_view,
                               validity=DataValidity.BROKEN),
        ]
        answer, meta = r5.render_v5(
            "20 слов B2 из P&P", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        # ERROR_FRIENDLY view rendered (not the silent empty learning_words view)
        self.assertEqual(meta["view_type"], "error_friendly")
        self.assertEqual(meta["fallback_reason"], "tool_broken")
        self.assertIn("BROKEN", answer)
        self.assertIn("learning_words", answer)

    def test_no_views_fallback(self):
        """No ToolResult has a view → renderer reports it cleanly."""
        results = [ToolResult.success(tool="x", data={"x": 1})]
        answer, meta = r5.render_v5(
            "x", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        self.assertEqual(meta["fallback_reason"], "no_views_in_results")
        self.assertEqual(meta["view_type"], "error_friendly")


# =====================================================================
# Meta dict shape — invariant for RequestTrace consumption
# =====================================================================


class MetaShape(unittest.TestCase):
    def test_meta_has_all_keys(self):
        view = vb.build_top_n_table(
            rows=[{"rank": 1, "x": "y"}],
            columns=["rank", "x"], requested_n=1,
        )
        results = [_result_with_view("dummy", view)]
        _, meta = r5.render_v5(
            "x", plan=None, results=results,
            model="x", ollama_host="x", enable_prose=False,
        )
        for k in ("view_type", "skeleton_chars", "prose_used",
                  "prose_audit_failed", "verification_failures",
                  "phase_a_ms", "phase_b_ms", "fallback_reason"):
            self.assertIn(k, meta, f"missing meta key: {k}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
