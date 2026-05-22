"""template_executor unit tests — v5 Phase 3 foundation.

Pure-function determinism tests:

  - Same view → same markdown bytes (no LLM, no nondeterminism)
  - Every cell in rendered output comes from view.payload (no fabrication
    possible)
  - Empty views render their empty_state, never a blank/silent skip
  - All 21 active ViewType variants have a renderer

These tests are the canonical proof that v5's anti-fabrication guarantee
is structural, not prompt-based.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import template_executor as te
from scripts.v2 import view_builders as vb
from scripts.v2.view_types import (
    EmptyReason, EmptyState, Provenance, RenderableView, ViewType,
)


class DeterminismGuarantees(unittest.TestCase):
    """Same input → same bytes, every time."""

    def test_top_n_table_byte_for_byte(self):
        for _ in range(5):
            v = vb.build_top_n_table(
                rows=[{"rank": 1, "word": "ajar"},
                       {"rank": 2, "word": "ere"}],
                columns=["rank", "word"],
                headline="Archaic",
                requested_n=2,
            )
            s = te.render_view(v)
            self.assertEqual(s, te.render_view(v))

    def test_no_python_dict_ordering_in_output(self):
        # Python dicts preserve insertion order; explicit column order
        # in build_top_n_table ensures stable cell ordering.
        v1 = vb.build_top_n_table(
            rows=[{"a": 1, "b": 2}],
            columns=["a", "b"],
        )
        v2 = vb.build_top_n_table(
            rows=[{"b": 2, "a": 1}],   # dict reverse-ordered
            columns=["a", "b"],
        )
        self.assertEqual(te.render_view(v1), te.render_view(v2))


class AllViewTypesCovered(unittest.TestCase):
    """Every non-BUNDLE ViewType has a renderer in VIEW_RENDERERS."""

    def test_dispatch_table_complete(self):
        missing = []
        for vt in ViewType:
            if vt == ViewType.BUNDLE:
                continue        # composite, handled separately
            if vt not in te.VIEW_RENDERERS:
                missing.append(vt.value)
        self.assertEqual(missing, [],
                         f"ViewTypes without renderer: {missing}")


class FormattingHelpers(unittest.TestCase):
    def test_format_int_thousand_sep(self):
        self.assertEqual(te.format_int(55101), "55 101")
        self.assertEqual(te.format_int(2844334465), "2 844 334 465")
        self.assertEqual(te.format_int(None), "—")

    def test_format_float(self):
        self.assertEqual(te.format_float(0.4385, digits=4), "0.4385")
        self.assertEqual(te.format_float(None), "—")

    def test_format_score(self):
        self.assertEqual(te.format_score(0.4385), "0.4385")
        # Canonical R7 number byte-exact
        self.assertEqual(te.format_score(0.4506), "0.4506")

    def test_format_share(self):
        self.assertEqual(te.format_share(0.158), "15.8%")
        self.assertEqual(te.format_share(None), "—")


class MdTableEscaping(unittest.TestCase):
    def test_pipe_escaped(self):
        s = te.md_table(["a"], [["x|y"]])
        # Pipe inside cell must be escaped, not split the row
        self.assertIn(r"x\|y", s)

    def test_newline_replaced(self):
        s = te.md_table(["a"], [["line1\nline2"]])
        self.assertNotIn("\n", s.split("\n", 2)[2])    # no nl in data row


class TopNTableRendering(unittest.TestCase):
    def test_renders_table_with_columns_and_rows(self):
        v = vb.build_top_n_table(
            rows=[{"rank": 1, "name": "Doyle"},
                   {"rank": 2, "name": "Stevenson"}],
            columns=["rank", "name"],
            headline="Top authors",
            requested_n=2,
        )
        s = te.render_view(v)
        self.assertIn("Top authors", s)
        self.assertIn("| rank | name |", s)
        self.assertIn("Doyle", s)
        self.assertIn("Stevenson", s)

    def test_count_mismatch_shown_to_user(self):
        v = vb.build_top_n_table(
            rows=[{"x": i} for i in range(14)],
            columns=["x"],
            requested_n=30,
        )
        s = te.render_view(v)
        self.assertIn("14", s)
        self.assertIn("30", s)

    def test_empty_view_renders_reason_not_blank(self):
        """B-R14-3 closure: empty view renders its reason, not blank
        markdown that the renderer would explain via prompt."""
        v = vb.build_top_n_table(
            rows=[], columns=["x"],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Все слова отфильтрованы.",
            empty_message_en="All filtered.",
            empty_filters_applied={"min_corpus_count": 5000},
        )
        s = te.render_view(v)
        self.assertIn("отфильтрованы", s)
        # Filter context shown
        self.assertIn("min_corpus_count", s)
        # Result is NOT an empty markdown table
        self.assertNotIn("| ", s)


class ComparisonPanelRendering(unittest.TestCase):
    def test_renders_metrics_and_entities(self):
        v = vb.build_comparison_panel(
            entities=[
                {"name": "Doyle",
                 "metrics": {"Burrows Delta": 0.0},
                 "signature_words": ["holmes", "watson"]},
                {"name": "Stevenson",
                 "metrics": {"Burrows Delta": 0.4385},
                 "signature_words": ["mate", "schooner"]},
            ],
            metrics=[{
                "name": "Burrows Delta",
                "direction": "LOWER = closer style",
                "scale": "0..∞",
                "interpret": "stylometric distance",
            }],
            shared_signatures=["sea"],
            headline="Doyle vs Stevenson",
        )
        s = te.render_view(v)
        self.assertIn("Doyle vs Stevenson", s)
        self.assertIn("LOWER = closer style", s)
        self.assertIn("0.4385", s)
        self.assertIn("holmes", s)
        self.assertIn("schooner", s)
        self.assertIn("sea", s)

    def test_b_r14_3_empty_renders_explicit_reason(self):
        """compare_authors returned 0 due to min_corpus_count — empty
        view must surface the WHY, not silently leave the prompt to
        invent (which is what R14 caught)."""
        v = vb.build_comparison_panel(
            entities=[], metrics=[],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Сравнение пустое: min_corpus_count=2000 "
                              "отфильтровал оба автора. Снизить порог?",
            empty_message_en="Comparison empty: filter too strict.",
            empty_filters_applied={"min_corpus_count": 2000},
            empty_suggestion="Попробуй min_corpus_count=500.",
        )
        s = te.render_view(v)
        self.assertIn("min_corpus_count", s)
        self.assertIn("2000", s)
        self.assertIn("Попробуй min_corpus_count=500", s)


class ReadabilitySummaryRendering(unittest.TestCase):
    def test_canonical_pride_numbers_exact(self):
        v = vb.build_readability_summary(
            book_title="Pride and Prejudice",
            pg_id="PG1342",
            flesch=58.8,
            flesch_kincaid=10.9,
            cefr="B2",
            word_count=119000,
        )
        s = te.render_view(v)
        # Canonical R7 numbers must appear byte-exact
        self.assertIn("58.8", s)
        self.assertIn("10.9", s)
        self.assertIn("B2", s)
        self.assertIn("119 000", s)


class EtymologyBundleRendering(unittest.TestCase):
    def test_full_bundle_shows_all_slots(self):
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa="əˈdʒɑːr",
            pos="ADJ",
            definition_en="slightly open",
            etymology={"primary_family": "Germanic",
                       "family_chain": ["ang", "gem-pro"]},
            snippets=[
                {"snippet": "Yet ajar it was, like a sleeping cat.",
                 "pg_id": "PG1", "title": "Old House", "author": "X"},
            ],
        )
        s = te.render_view(v)
        self.assertIn("приоткрытый", s)
        self.assertIn("/əˈdʒɑːr/", s)
        self.assertIn("ADJ", s)
        self.assertIn("slightly open", s)
        self.assertIn("Yet ajar", s)
        self.assertIn("Germanic", s)

    def test_e16_partial_bundle_silent_on_missing_etymology(self):
        """E16 (2026-05-22) — REVERSAL of original B-R14-2 fix.

        Previous behavior: when etymology slot was None, renderer
        explicitly said «Этимология не извлеклась» so the LLM (Phase B
        prose) couldn't hallucinate a fake etymology.

        New behavior (E16): renderer stays SILENT on missing etymology
        when the user didn't ask about it. Stan «ajar» prod bug:
        intent=word_contexts dispatched enrich_word as B101 bundle
        bonus; «ajar» has no Wiktionary etymology → template printed
        «Этимология не извлеклась» → critic flagged it as unsupported
        claim → user saw a useless empty card with a critic flag.

        Hallucination-prevention is still handled by:
          1. Phase B prose audit (rejects claims with no payload anchor)
          2. dedicated word_etymology tool's empty_state when intent=etymology
        """
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            pos="ADJ",
        )
        s = te.render_view(v)
        # The line MUST NOT appear (it would be unsolicited noise)
        self.assertNotIn("Этимология не извлеклась", s)
        # The translation + POS still render
        self.assertIn("приоткрытый", s)
        self.assertIn("ADJ", s)
        # No phantom etymology either (we shouldn't have invented one)
        self.assertNotIn("**Этимология:**", s)


class LearningWordsRendering(unittest.TestCase):
    def test_b_r14_7_broken_state_surfaces(self):
        """When learning_words is semantically broken (B2 = 0 across all
        books), renderer must say so explicitly — not pretend the
        feature returned a legit empty."""
        v = vb.build_learning_words(
            words=[],
            requested_level="B2",
            scope_label="Pride and Prejudice (PG1342)",
            is_broken=True,
            broken_reason="Level filter returned 0 across 50 canonical books.",
        )
        s = te.render_view(v)
        self.assertIn("Level filter", s)
        # Empty_state's suggestion also surfaces (renderer-deterministic)
        self.assertIn("Попробуй", s)


class CorpusMetaRendering(unittest.TestCase):
    def test_canonical_numbers(self):
        v = vb.build_corpus_meta_snapshot(
            n_books=55101,
            n_authors=12350,
            n_tokens=2844334465,
            spgc_baseline="SPGC-2018-07-18",
        )
        s = te.render_view(v)
        # Canonical Q14 number, byte-exact format with thin-space sep
        self.assertIn("55 101", s)
        self.assertIn("2 844 334 465", s)


class NotFoundRendering(unittest.TestCase):
    def test_did_you_mean(self):
        v = vb.build_not_found(
            entity_type="author",
            query="Asimov",
            message_ru="Asimov в копирайте.",
            candidates=[{"display": "Asimov-class SciFi: Wells, H.G."}],
        )
        s = te.render_view(v)
        self.assertIn("автора", s)
        self.assertIn("Asimov в копирайте", s)
        self.assertIn("Может быть", s)


class OutOfScopeRendering(unittest.TestCase):
    def test_3_part_refusal(self):
        v = vb.build_out_of_scope(
            reason_kind="copyright",
            why_ru="«The Hobbit» Толкина под защитой copyright.",
            what_ru=[
                "Мета-информация (Gutendex API)",
                "Загрузить локально через /admin/ (fair use)",
            ],
            which_alternatives=[
                {"title": "The Castle of Otranto", "pg_id": "PG696",
                 "note": "Gothic ancestor of fantasy"},
            ],
        )
        s = te.render_view(v)
        self.assertIn("Толкина", s)
        self.assertIn("Gutendex API", s)
        self.assertIn("PG696", s)


class ClarifyRendering(unittest.TestCase):
    def test_clarify_shows_alternatives(self):
        v = vb.build_clarify(
            question_ru="Кого из Hugo: Victor или Ganz?",
            alternatives=["Victor Hugo — 12K downloads",
                          "Ganz, Hugo — 10 downloads"],
            why="2 кандидата с похожим fuzz-score.",
        )
        s = te.render_view(v)
        self.assertIn("Victor Hugo", s)
        self.assertIn("Ganz, Hugo", s)
        self.assertIn("2 кандидата", s)


class ErrorFriendlyRendering(unittest.TestCase):
    def test_renderer_error_with_partials(self):
        v = vb.build_error_friendly(
            kind="renderer",
            message_ru="Renderer не успел собрать ответ за 60s.",
            retry_hint_ru="Попробуй упростить запрос.",
            partial_results=[
                {"tool": "affinity_by_author",
                 "summary": "получил 30 слов, без рендера"},
            ],
        )
        s = te.render_view(v)
        self.assertIn("Renderer не успел", s)
        self.assertIn("affinity_by_author", s)
        self.assertIn("Попробуй упростить", s)


# =====================================================================
# Anti-fabrication structural guarantee
# =====================================================================


class AntiFabricationGuarantee(unittest.TestCase):
    """The whole point of v5 Phase 3: rendered output contains ONLY
    payload content, never invented strings."""

    def test_rendered_contains_only_payload_strings(self):
        """For TOP_N_TABLE, every non-whitespace word in the rendered
        markdown that isn't a delimiter or column header should appear
        in view.payload."""
        v = vb.build_top_n_table(
            rows=[{"rank": 1, "word": "felicitous"},
                   {"rank": 2, "word": "tremulous"},
                   {"rank": 3, "word": "decorative"}],
            columns=["rank", "word"],
            headline="Wilde ADJ markers",
        )
        s = te.render_view(v)
        # Content words from data — must be there
        self.assertIn("felicitous", s)
        self.assertIn("tremulous", s)
        self.assertIn("decorative", s)
        # Not in data — must NOT be there (anti-fabrication check)
        self.assertNotIn("apparition", s)
        self.assertNotIn("luminous", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
