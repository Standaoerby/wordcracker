"""Phase 6 — энфорс view-контракта.

Tests for the gate of Phase 6 (REFACTOR_BRIEF §3 фаза 6):

  * рендер этимологии без IPA даёт «IPA: недоступно», не пустую ячейку
  * `None` не рендерится как строка нигде
  * REQUIRED_FIELDS объявлены на каждый ViewType, validate() их проверяет
  * BUNDLE в dict-форме рендерится (бывший плейсхолдер заменён)

The tests fail on the pre-Phase-6 codebase and pass after the
view_types.py + template_executor.py + view_builders.py changes.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import template_executor as te
from scripts.v2 import view_builders as vb
from scripts.v2._types import ToolResult
from scripts.v2.view_types import (
    REQUIRED_FIELDS,
    DataValidity,
    EmptyReason,
    EmptyState,
    Provenance,
    RenderableView,
    ViewType,
    parse_required_field_missing,
    required_field_missing_issue,
)


# ---------------------------------------------------------------------
# Gate test 1 — ETYMOLOGY_BUNDLE without IPA
# ---------------------------------------------------------------------


class EtymologyMissingIpaSurfaceshUnavailable(unittest.TestCase):
    """Phase 6 gate (line 1): «рендер этимологии без IPA даёт
    «IPA: недоступно», не пустую ячейку»."""

    def test_missing_ipa_renders_unavailable_phrase(self):
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            pos="ADJ",
            # ipa intentionally omitted
        )
        s = te.render_view(v)
        # Both the field label AND the «недоступно» caveat must appear.
        self.assertIn("IPA", s)
        self.assertIn("недоступно", s)
        self.assertRegex(s, r"IPA\*\*?\s*:\s*недоступно")

    def test_missing_translation_renders_unavailable_phrase(self):
        v = vb.build_etymology_bundle(
            word="ajar",
            ipa="əˈdʒɑːr",
            pos="ADJ",
        )
        s = te.render_view(v)
        self.assertIn("перевод", s)
        self.assertIn("недоступно", s)

    def test_missing_pos_renders_unavailable_phrase(self):
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa="əˈdʒɑːr",
        )
        s = te.render_view(v)
        self.assertIn("POS", s)
        self.assertIn("недоступно", s)

    def test_full_bundle_does_NOT_show_unavailable(self):
        """The «недоступно» phrase appears ONLY when something is missing.
        With every slot filled, the IPA value (in slashes) appears and
        no «недоступно» line is produced."""
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa="əˈdʒɑːr",
            pos="ADJ",
            definition_en="slightly open",
        )
        s = te.render_view(v)
        self.assertIn("/əˈdʒɑːr/", s)
        self.assertNotIn("недоступно", s)


# ---------------------------------------------------------------------
# Gate test 2 — None never renders as literal string
# ---------------------------------------------------------------------


_LITERAL_NONE_RE = re.compile(r"\bNone\b")


def _assert_no_literal_none(testcase, s: str, view_label: str) -> None:
    """Helper. Phase 6 invariant: rendered markdown must NEVER contain
    the literal token `None`. Word-boundary regex so legitimate
    substrings inside other words don't false-positive."""
    matches = _LITERAL_NONE_RE.findall(s or "")
    testcase.assertFalse(
        matches,
        f"{view_label}: literal 'None' leaked into output:\n{s}",
    )


class NoneNeverRendersAsString(unittest.TestCase):
    """Phase 6 gate (line 2): «`None` не рендерится как строка нигде».

    For every view type that has a renderer, build a view whose payload
    has None values in the optional slots — the rendered output must
    not contain the literal token `None`.
    """

    def test_etymology_bundle_with_partial_slots_unavailable_phrase(self):
        """Bundle with only `word` + one slot — the renderer must
        surface the unfilled head-line slots as «недоступно», not as
        a silent skip and not as the literal `None`. (When ALL slots are
        empty the bundle is structurally empty and the builder is
        expected to set empty_state — that path is exercised by
        is_empty + empty_state structural checks.)"""
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa=None,
            pos=None,
            definition_en=None,
            etymology=None,
            snippets=None,
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "etymology_bundle")
        # «недоступно» is the expected fallback string for missing
        # head-line facets (ipa, pos).
        self.assertIn("недоступно", s)
        self.assertIn("приоткрытый", s)

    def test_etymology_bundle_with_none_snippet_text(self):
        """Snippet rows with `None` as the text must be skipped, not
        rendered as «None» (regression: E9 pattern in audit §4.5)."""
        v = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            ipa="əˈdʒɑːr",
            pos="ADJ",
            snippets=[
                {"snippet": None, "title": None, "author": None,
                 "pg_id": "PG999"},
                {"snippet": "  ", "title": "Empty", "author": "Z"},
                {"snippet": "Real example here.", "title": "OK", "author": "Q"},
            ],
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "etymology_bundle.snippets")
        self.assertIn("Real example here", s)

    def test_readability_summary_with_none_title(self):
        v = RenderableView(
            view_type=ViewType.READABILITY_SUMMARY,
            payload={
                "book_title": None, "pg_id": None,
                "flesch": 58.8, "flesch_kincaid": 10.9,
                "cefr": None, "word_count": None,
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "readability_summary")

    def test_book_lookup_with_none_fields(self):
        v = RenderableView(
            view_type=ViewType.BOOK_LOOKUP,
            payload={"book": {
                "title": None, "pg_id": None, "author": None,
                "pub_year": None, "downloads": None,
            }},
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "book_lookup")

    def test_author_metadata_with_none_fields(self):
        v = RenderableView(
            view_type=ViewType.AUTHOR_METADATA,
            payload={
                "author_canonical": None,
                "birth_year": None, "death_year": None,
                "nationality": None, "books_in_corpus": 0,
                "bio_source": None,
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "author_metadata")

    def test_clarify_with_none_question(self):
        v = RenderableView(
            view_type=ViewType.CLARIFY,
            payload={"question": None, "alternatives": [None, "ok"], "why": None},
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "clarify")

    def test_not_found_with_none_query(self):
        v = RenderableView(
            view_type=ViewType.NOT_FOUND,
            payload={
                "entity_type": None, "query": None,
                "message_ru": None, "candidates": [{"display": None}],
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "not_found")

    def test_introduction_with_none_name(self):
        v = RenderableView(
            view_type=ViewType.INTRODUCTION,
            payload={"name": None, "capabilities": [None, "x"],
                     "examples": [None], "corpus_size_books": 1},
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "introduction")

    def test_error_friendly_with_none_message(self):
        v = RenderableView(
            view_type=ViewType.ERROR_FRIENDLY,
            payload={
                "kind": "renderer", "message_ru": None,
                "retry_hint_ru": None,
                "partial_results": [{"tool": None, "summary": None}],
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "error_friendly")

    def test_out_of_scope_with_none_fields(self):
        v = RenderableView(
            view_type=ViewType.OUT_OF_SCOPE,
            payload={
                "reason_kind": "copyright",
                "why_ru": None,
                "what_ru": [None, "x"],
                "which_alternatives": [{"title": None, "pg_id": None,
                                         "note": None}],
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "out_of_scope")

    def test_timeline_chart_with_none_buckets(self):
        v = RenderableView(
            view_type=ViewType.TIMELINE_CHART,
            payload={
                "word": "felicitous",
                "series": [{"bucket_start": None, "bucket_end": None,
                            "freq_per_million": None, "count": None}],
                "basis": "decade",
            },
        )
        s = te.render_view(v)
        _assert_no_literal_none(self, s, "timeline_chart")


# ---------------------------------------------------------------------
# Gate test 3 — REQUIRED_FIELDS declared per ViewType
# ---------------------------------------------------------------------


class RequiredFieldsRegistry(unittest.TestCase):
    """Every non-BUNDLE ViewType is mentioned in REQUIRED_FIELDS, even
    if the set is empty. Catches the «we added a new ViewType and
    forgot to declare its contract» regression."""

    def test_every_view_type_has_an_entry(self):
        missing = [vt for vt in ViewType if vt not in REQUIRED_FIELDS]
        self.assertEqual(missing, [],
                         f"ViewTypes without REQUIRED_FIELDS entry: {missing}")

    def test_etymology_bundle_requires_word_and_head_facets(self):
        req = REQUIRED_FIELDS[ViewType.ETYMOLOGY_BUNDLE]
        self.assertIn("word", req)
        self.assertIn("ipa", req)
        self.assertIn("translation_ru", req)
        self.assertIn("pos", req)


class ValidateChecksRequiredFields(unittest.TestCase):
    """validate() reports missing required fields with a parseable
    prefix; render_view downgrades them from fatal to caveat."""

    def test_validate_reports_missing_field_with_known_prefix(self):
        v = RenderableView(
            view_type=ViewType.ETYMOLOGY_BUNDLE,
            payload={
                "word": "ajar", "translation_ru": "приоткрытый",
                "ipa": None, "pos": "ADJ",
                "slots_available": {"translation": True, "ipa": False,
                                     "pos": True, "definition": False,
                                     "etymology": False, "snippets": False},
            },
        )
        issues = v.validate()
        self.assertTrue(any(
            i == required_field_missing_issue(
                ViewType.ETYMOLOGY_BUNDLE, "ipa")
            for i in issues
        ), f"expected required_field_missing for ipa, got {issues}")

    def test_missing_required_fields_helper(self):
        v = RenderableView(
            view_type=ViewType.READABILITY_SUMMARY,
            payload={"flesch": 58.8, "flesch_kincaid": 10.9, "cefr": "B2"},
        )
        # book_title + pg_id are required, neither present
        miss = set(v.missing_required_fields())
        self.assertEqual(miss, {"book_title", "pg_id"})

    def test_parse_required_field_missing_roundtrip(self):
        issue = required_field_missing_issue(ViewType.NOT_FOUND, "query")
        parsed = parse_required_field_missing(issue)
        self.assertEqual(parsed, ("not_found", "query"))
        self.assertIsNone(parse_required_field_missing("something else"))

    def test_render_view_does_NOT_fail_on_missing_required_field(self):
        """The render must continue even if validate() reports
        required-field-missing issues — the per-view renderer surfaces
        them as «<field>: недоступно». We provide one filled slot
        (definition_en) so the bundle is structurally non-empty and the
        check exercises the required-field path."""
        v = vb.build_etymology_bundle(
            word="ajar",
            definition_en="slightly open",
            # translation_ru, ipa, pos still missing
        )
        s = te.render_view(v)
        # NOT the «Internal: invalid view» fallback
        self.assertNotIn("Internal: invalid view", s)
        # IS the «недоступно» surface
        self.assertIn("недоступно", s)

    def test_attach_view_does_NOT_raise_on_required_field_missing(self):
        """attach_view stays strict on structural issues but lenient on
        required-field-missing — those are rendered as caveats, not
        attach-time blockers.

        We give the bundle one filled slot (definition_en) so the view
        is structurally non-empty; IPA / translation / POS missing →
        required-field-missing entries, which attach_view must tolerate."""
        r = ToolResult.success(tool="enrich_word", data={"word": "x"})
        v = vb.build_etymology_bundle(
            word="ajar",
            definition_en="slightly open",
            # IPA / translation_ru / POS missing — required-field-missing
            # but not structural (definition_en keeps slots_available non-empty).
        )
        vb.attach_view(r, v, data_validity=DataValidity.OK)
        self.assertIs(r.view, v)

    def test_attach_view_still_raises_on_structural_violation(self):
        """attach_view's structural guard (empty payload without
        empty_state) still raises — required-field check is in addition
        to that, not a replacement."""
        r = ToolResult.success(tool="t", data={})
        bad = RenderableView(
            view_type=ViewType.TOP_N_TABLE,
            payload={},
            empty_state=None,
        )
        with self.assertRaises(ValueError):
            vb.attach_view(r, bad)


# ---------------------------------------------------------------------
# Gate test 4 — BUNDLE dict-form sub-views render
# ---------------------------------------------------------------------


class BundleRendersDictFormSubViews(unittest.TestCase):
    """Phase 6 gate (line 3): «Доделать рендер BUNDLE в dict-форме».

    The pre-Phase-6 renderer emitted
    `_(bundle sub-view: dict-form, render in Phase 3.5)_` whenever a
    sub-view came in as a plain dict (which happens after cache
    round-trip). Phase 6 rehydrates it via RenderableView.from_dict.
    """

    def test_bundle_with_dataclass_sub_views(self):
        inner = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
            word_count=119000,
        )
        bundle = RenderableView(
            view_type=ViewType.BUNDLE,
            payload={"sub_views": [inner]},
            headline="Composite",
        )
        s = te.render_view(bundle)
        self.assertIn("58.8", s)
        self.assertIn("Pride and Prejudice", s)
        self.assertNotIn("not yet supported", s)

    def test_bundle_with_dict_form_sub_views(self):
        """Cache restore path — sub_views arrive as serialised dicts.
        Old behaviour: placeholder line. New behaviour: rehydrate +
        render fully."""
        inner = vb.build_readability_summary(
            book_title="Dorian", pg_id="PG174",
            flesch=70.1, flesch_kincaid=8.3, cefr="B1",
        )
        bundle = RenderableView(
            view_type=ViewType.BUNDLE,
            payload={"sub_views": [inner.to_dict()]},
            headline="Cache-roundtrip composite",
        )
        s = te.render_view(bundle)
        # The dict-form placeholder MUST be gone
        self.assertNotIn("dict-form, render in Phase 3.5", s)
        # The actual data MUST appear
        self.assertIn("70.1", s)
        self.assertIn("Dorian", s)

    def test_bundle_with_mixed_sub_views(self):
        a = vb.build_clarify(question_ru="Какой автор?")
        b = vb.build_not_found(
            entity_type="book", query="X-Файлы",
            message_ru="Нет в корпусе.",
        )
        bundle = RenderableView(
            view_type=ViewType.BUNDLE,
            payload={"sub_views": [a, b.to_dict()]},
        )
        s = te.render_view(bundle)
        self.assertIn("Какой автор", s)
        self.assertIn("Нет в корпусе", s)
        _assert_no_literal_none(self, s, "bundle.mixed")

    def test_bundle_is_empty_recognises_no_sub_views(self):
        v = RenderableView(
            view_type=ViewType.BUNDLE,
            payload={"sub_views": []},
            empty_state=EmptyState(
                reason=EmptyReason.NO_SIGNAL_EXPECTED,
                message_ru="Bundle пустой.",
                message_en="Empty bundle.",
            ),
        )
        self.assertTrue(v.is_empty())
        self.assertEqual(v.validate(), [])


# ---------------------------------------------------------------------
# safe_str — central None-guard utility
# ---------------------------------------------------------------------


class SafeStrHelper(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertEqual(te.safe_str(None), "—")
        self.assertEqual(te.safe_str(None, default=""), "")

    def test_literal_string_none_returns_default(self):
        """Defence in depth: if some upstream layer stringified None
        before reaching the renderer, we still detect and replace it."""
        self.assertEqual(te.safe_str("None"), "—")
        self.assertEqual(te.safe_str("none"), "—")

    def test_empty_and_whitespace_treated_as_missing(self):
        self.assertEqual(te.safe_str(""), "—")
        self.assertEqual(te.safe_str("   "), "—")
        self.assertEqual(te.safe_str("\t\n"), "—")

    def test_real_values_pass_through(self):
        self.assertEqual(te.safe_str("Доил"), "Доил")
        self.assertEqual(te.safe_str(42), "42")
        self.assertEqual(te.safe_str(3.14), "3.14")


if __name__ == "__main__":
    unittest.main(verbosity=2)
