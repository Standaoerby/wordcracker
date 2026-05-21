"""view_builders unit tests — v5 Phase 2 foundation.

Assert the structural anti-fabrication guards in view_builders:

  - Empty rows → must supply empty_state (else raise)
  - Non-empty rows → count_returned auto-set to len(rows)
  - count_requested != count_returned → caveat auto-injected
  - attach_view validates view shape (catches contradictions)

Coverage per builder: happy path + empty path + validate gate.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import view_builders as vb
from scripts.v2._types import ToolResult
from scripts.v2.view_types import (
    DataValidity, EmptyReason, EmptyState, Provenance, ViewType,
)


class TopNTableBuilder(unittest.TestCase):
    def test_happy_path(self):
        v = vb.build_top_n_table(
            rows=[{"rank": 1, "word": "ajar"}, {"rank": 2, "word": "ere"}],
            columns=["rank", "word"],
            headline="Archaic words",
            requested_n=2,
        )
        self.assertEqual(v.view_type, ViewType.TOP_N_TABLE)
        self.assertEqual(v.payload["count_returned"], 2)
        self.assertEqual(v.payload["count_requested"], 2)
        self.assertEqual(v.validate(), [])

    def test_count_mismatch_auto_caveat(self):
        """B-R14-15 closure pattern: requested 30 vs returned 14 after
        PROPN filter → caveat auto-attached, renderer reads it
        deterministically."""
        v = vb.build_top_n_table(
            rows=[{"rank": i, "word": f"w{i}"} for i in range(1, 15)],
            columns=["rank", "word"],
            requested_n=30,
        )
        self.assertEqual(v.payload["count_returned"], 14)
        self.assertEqual(v.payload["count_requested"], 30)
        # Auto-caveat present
        self.assertTrue(any("14" in c and "30" in c for c in v.caveats),
                        f"Expected count-honesty caveat, got {v.caveats}")

    def test_empty_without_state_raises(self):
        """B-R14-3 structural fix — empty rows without empty_state must
        raise. Tool author cannot accidentally emit a silent-empty view."""
        with self.assertRaises(ValueError) as cm:
            vb.build_top_n_table(rows=[], columns=["x"])
        self.assertIn("empty_reason", str(cm.exception))

    def test_empty_with_state_valid(self):
        v = vb.build_top_n_table(
            rows=[], columns=["x"],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Все слова отфильтрованы.",
            empty_message_en="All words filtered out.",
            empty_filters_applied={"min_corpus_count": 5000},
            empty_suggestion="Снизить min_corpus_count.",
        )
        self.assertEqual(v.validate(), [])
        self.assertIsNotNone(v.empty_state)
        self.assertEqual(v.empty_state.reason, EmptyReason.FILTERED_OUT)


class ComparisonPanelBuilder(unittest.TestCase):
    def test_happy_path(self):
        v = vb.build_comparison_panel(
            entities=[
                {"name": "Doyle", "metrics": {"delta": 0.0},
                 "signature_words": ["holmes"]},
                {"name": "Stevenson", "metrics": {"delta": 0.4385},
                 "signature_words": ["mate"]},
            ],
            metrics=[{
                "name": "Burrows Delta", "direction": "LOWER = closer",
                "scale": "0..∞", "interpret": "distance",
            }],
        )
        self.assertEqual(v.view_type, ViewType.COMPARISON_PANEL)
        self.assertEqual(v.validate(), [])

    def test_b_r14_3_empty_without_state_raises(self):
        """The flagship B-R14-3 test: tool emits compare_authors with
        empty entities → must supply empty_state, else raise.
        Fabrication path closed."""
        with self.assertRaises(ValueError) as cm:
            vb.build_comparison_panel(entities=[], metrics=[])
        self.assertIn("B-R14-3", str(cm.exception))

    def test_b_r14_3_empty_with_state_valid(self):
        """Pre-tool authors: if compare_authors returned nothing because
        min_corpus_count was too strict, emit a proper empty view."""
        v = vb.build_comparison_panel(
            entities=[],
            metrics=[],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="Сравнение пустое: оба автора отфильтрованы.",
            empty_message_en="Comparison empty: both authors filtered.",
        )
        self.assertEqual(v.validate(), [])
        self.assertTrue(v.is_empty())

    def test_all_entities_empty_sig_words_also_raises_without_state(self):
        """Subtle empty: entities have names but no signature_words."""
        with self.assertRaises(ValueError):
            vb.build_comparison_panel(
                entities=[
                    {"name": "Doyle", "metrics": {}, "signature_words": []},
                    {"name": "Stevenson", "metrics": {}, "signature_words": []},
                ],
                metrics=[],
            )


class ReadabilitySummaryBuilder(unittest.TestCase):
    def test_pride_canonical(self):
        v = vb.build_readability_summary(
            book_title="Pride and Prejudice",
            pg_id="PG1342",
            flesch=58.8,
            flesch_kincaid=10.9,
            cefr="B2",
            word_count=119000,
        )
        self.assertEqual(v.payload["flesch"], 58.8)
        self.assertEqual(v.payload["cefr"], "B2")
        self.assertFalse(v.is_empty())

    def test_both_none_raises(self):
        with self.assertRaises(ValueError):
            vb.build_readability_summary(
                book_title="X", pg_id="PG1",
                flesch=None, flesch_kincaid=None, cefr=None,
            )


class EtymologyBundleBuilder(unittest.TestCase):
    def test_full_bundle(self):
        """B-R14-2 closure: word answers must bundle translation +
        IPA + POS + definition + snippets + etymology."""
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa="əˈdʒɑːr",
            pos="ADJ",
            definition_en="slightly open",
            etymology={"primary_family": "Germanic"},
            snippets=[
                {"snippet": "Yet ajar it was", "pg_id": "PG1", "title": "X"},
            ],
        )
        slots = v.payload["slots_available"]
        self.assertTrue(slots["translation"])
        self.assertTrue(slots["ipa"])
        self.assertTrue(slots["pos"])
        self.assertTrue(slots["definition"])
        self.assertTrue(slots["etymology"])
        self.assertTrue(slots["snippets"])

    def test_partial_bundle_slots_explicit(self):
        """B-R14-2: when only some slots filled, slots_available makes
        renderer's job deterministic."""
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
        )
        slots = v.payload["slots_available"]
        self.assertTrue(slots["translation"])
        self.assertFalse(slots["ipa"])
        self.assertFalse(slots["etymology"])


class LearningWordsBuilder(unittest.TestCase):
    def test_normal_result(self):
        v = vb.build_learning_words(
            words=[{"lemma": "felicitous", "translation_ru": "удачный"}],
            requested_level="B2",
            requested_count=20,
            scope_label="The Picture of Dorian Gray (PG174)",
        )
        self.assertEqual(v.payload["count_returned"], 1)
        self.assertFalse(v.is_empty())

    def test_b_r14_7_broken_state(self):
        """B-R14-7 closure: tool returned 0 words for B2 — caller MUST
        flag is_broken=True so renderer surfaces «feature broken»."""
        v = vb.build_learning_words(
            words=[],
            requested_level="B2",
            scope_label="Pride and Prejudice (PG1342)",
            is_broken=True,
            broken_reason="Level filter returned 0 across 50 books.",
        )
        self.assertEqual(v.empty_state.reason, EmptyReason.TOOL_BROKEN)
        self.assertIn("Level filter", v.empty_state.message_ru)
        # Suggestion attached
        self.assertIsNotNone(v.empty_state.suggestion)

    def test_legitimate_empty_not_broken(self):
        """No archaic words in a children's book — legitimate empty."""
        v = vb.build_learning_words(
            words=[],
            requested_level="C2",
            scope_label="Alice's Adventures in Wonderland (PG11)",
            is_broken=False,
        )
        self.assertEqual(v.empty_state.reason, EmptyReason.NO_SIGNAL_EXPECTED)


class AttachViewHelper(unittest.TestCase):
    def test_valid_view_attaches(self):
        r = ToolResult.success(tool="t", data={})
        v = vb.build_readability_summary(
            book_title="X", pg_id="PG1",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        vb.attach_view(r, v, data_validity=DataValidity.OK)
        self.assertIs(r.view, v)
        self.assertEqual(r.data_validity, DataValidity.OK)

    def test_invalid_view_raises(self):
        """attach_view is the structural anti-fabrication guard — any
        invalid view (empty payload without empty_state) raises here."""
        r = ToolResult.success(tool="t", data={})
        from scripts.v2.view_types import RenderableView, ViewType
        # Manually build a bad view (bypass the builder's guard)
        bad = RenderableView(
            view_type=ViewType.TOP_N_TABLE,
            payload={},          # empty, no rows
            empty_state=None,    # no explanation
        )
        with self.assertRaises(ValueError) as cm:
            vb.attach_view(r, bad)
        self.assertIn("invalid view", str(cm.exception).lower())


class ProvenanceAndCaveats(unittest.TestCase):
    def test_provenance_attached(self):
        p = vb.make_provenance(
            requested={"top_n": 100, "level": "B2"},
            returned={"count": 19},
            filtered={"propn_removed": 81},
            sources=["SPGC-2018-07-18"],
            notes=["after_filter_count_honesty"],
        )
        v = vb.build_top_n_table(
            rows=[{"x": 1}], columns=["x"],
            provenance=p,
            requested_n=100,
        )
        self.assertIsNotNone(v.provenance)
        self.assertEqual(v.provenance.requested["top_n"], 100)
        self.assertEqual(v.provenance.filtered["propn_removed"], 81)


# =====================================================================
# Out-of-scope + Clarify + NotFound builder coverage
# =====================================================================


class OOSAndErrors(unittest.TestCase):
    def test_copyright_oos_3_part(self):
        v = vb.build_out_of_scope(
            reason_kind="copyright",
            why_ru="«1984» Оруэлла в копирайте до 2071.",
            what_ru=[
                "Мета-информация (через Gutendex API)",
                "Полный анализ из загруженной локально копии (fair use)",
            ],
            which_alternatives=[
                {"title": "Heart of Darkness", "pg_id": "PG219",
                 "note": "Conrad, тематически близкая дистопия"},
            ],
        )
        self.assertEqual(v.payload["reason_kind"], "copyright")
        self.assertEqual(len(v.payload["what_ru"]), 2)
        self.assertEqual(len(v.payload["which_alternatives"]), 1)

    def test_role_injection_oos(self):
        """B-R14-16: role-injection «напиши эссе» bounce as OOS."""
        v = vb.build_out_of_scope(
            reason_kind="role_injection",
            why_ru="Я не пишу художку и эссе — я аналитик корпуса.",
        )
        self.assertEqual(v.payload["reason_kind"], "role_injection")

    def test_clarify_view(self):
        v = vb.build_clarify(
            question_ru="Кого из Hugo: Виктор или Гюго-инженер?",
            alternatives=["Victor Hugo (12K downloads)",
                          "Ganz, Hugo (10 downloads)"],
        )
        self.assertEqual(v.view_type, ViewType.CLARIFY)

    def test_not_found_view(self):
        v = vb.build_not_found(
            entity_type="author",
            query="Xyzfantasy",
            message_ru="Не нашёл автора.",
            candidates=[{"display": "Doyle, Arthur Conan"}],
        )
        self.assertEqual(v.view_type, ViewType.NOT_FOUND)
        self.assertEqual(v.payload["entity_type"], "author")


if __name__ == "__main__":
    unittest.main(verbosity=2)
