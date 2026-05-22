"""E16 — «примеры использования слова X» (word_contexts intent)
rendering bug.

ROOT CAUSE (2026-05-22 Stan prod):
Query «примеры использования слова "ajar" в английской литературе»
returned a useless ETYMOLOGY_BUNDLE card with translation + POS but
NO examples and a critic-flagged «Этимология не извлеклась» line.

THREE architectural bugs in cascade:

  1. hybrid_search returned snippets but NEVER emitted a view.
     scripts/v2/tools/search/hybrid.py returned ToolResult with
     `data["matches"]` populated but no RenderableView → select_primary_view
     ignored it.

  2. ETYMOLOGY_BUNDLE (priority 90) unconditionally beat WORD_CONTEXTS
     (priority 70). For intent=word_contexts the renderer picked
     enrich_word's bundle over hybrid_search's contexts even when
     contexts had data.

  3. Template `_render_etymology_bundle` printed «Этимология не
     извлеклась» whenever slots_available[etymology] was False —
     unsolicited meta-noise that critic correctly flagged as an
     unsupported claim.

Fixes:
  A. hybrid_search emits WORD_CONTEXTS view with snippets/title/author
  B. select_primary_view takes `intent` kwarg; for intent=word_contexts
     applies +40 bonus to WORD_CONTEXTS view-type, beating
     ETYMOLOGY_BUNDLE (70+40=110 > 90).
  C. Drop the «Этимология не извлеклась» line — replaced with silence;
     hallucination prevention via prose-audit + dedicated word_etymology
     empty_state.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult
from scripts.v2 import view_builders as vb
from scripts.v2.view_types import DataValidity, ViewType
from scripts.v2.render_v5 import (
    select_primary_view, _intent_alignment_bonus,
)


class HybridSearchEmitsView(unittest.TestCase):
    """Fix A — hybrid_search must emit a WORD_CONTEXTS view."""

    def test_hybrid_search_emits_word_contexts_view(self):
        from scripts.v2.tools.search.hybrid import hybrid_search

        # Mock both retrievers — lexical + semantic
        from scripts.v2._types import ToolResult as TR, Coverage as Cov

        lex_result = TR.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG1342", "snippet": "She left the door ajar.",
                 "title": "Pride and Prejudice", "author": "Austen"},
                {"pg_id": "PG345", "snippet": "The casement stood ajar.",
                 "title": "Dracula", "author": "Stoker"},
            ]},
            coverage=Cov(books_matched=2, books_total=-1),
            query={"query": "ajar", "k": 50},
        )

        with mock.patch("scripts.v2.tools.search.hybrid.v2_dispatch",
                         return_value=lex_result), \
             mock.patch("scripts.v2.tools.search.hybrid.dispatch_any",
                         return_value=TR.fail(tool="semantic_search",
                                              err_type="internal",
                                              message="mocked off")):
            r = hybrid_search(query="ajar", k=12)

        self.assertTrue(r.ok)
        # CRITICAL: r.view must exist (was None before E16)
        self.assertIsNotNone(r.view,
                              "hybrid_search must emit a WORD_CONTEXTS view")
        self.assertEqual(r.view.view_type, ViewType.WORD_CONTEXTS)
        # Contexts pulled from snippet field
        contexts = (r.view.payload or {}).get("contexts") or []
        self.assertGreaterEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["snippet"], "She left the door ajar.")
        self.assertEqual(contexts[0]["author"], "Austen")


class IntentAlignmentBonus(unittest.TestCase):
    """Fix B — intent-aware view selection bonus."""

    def test_word_contexts_intent_bonus_for_word_contexts_view(self):
        bonus = _intent_alignment_bonus("word_contexts", ViewType.WORD_CONTEXTS)
        self.assertEqual(bonus, 40)

    def test_word_contexts_intent_no_bonus_for_etymology_bundle(self):
        bonus = _intent_alignment_bonus("word_contexts", ViewType.ETYMOLOGY_BUNDLE)
        self.assertEqual(bonus, 0)

    def test_none_intent_no_bonus(self):
        self.assertEqual(_intent_alignment_bonus(None,
                                                  ViewType.WORD_CONTEXTS), 0)

    def test_unknown_intent_no_bonus(self):
        self.assertEqual(_intent_alignment_bonus("randomthing",
                                                  ViewType.WORD_CONTEXTS), 0)


class SelectPrimaryViewWithIntent(unittest.TestCase):
    """Fix B integration — select_primary_view honors intent for the
    «examples vs bundle» contest."""

    def _word_contexts_result(self, n=2):
        view = vb.build_word_contexts(
            word="ajar",
            contexts=[
                {"snippet": "door ajar", "pg_id": "PG1",
                 "title": "T1", "author": "A1"}
                for _ in range(n)
            ],
            scope_label="corpus",
        )
        r = ToolResult.success(
            tool="hybrid_search", data={"matches": []},
            coverage=Coverage(books_matched=n, books_total=-1),
        )
        vb.attach_view(r, view, data_validity=DataValidity.OK)
        return r

    def _etym_bundle_result(self, with_etym=False, with_snippets=False):
        etym = ({"primary_family": "Germanic", "family_chain": ["ang"]}
                if with_etym else None)
        snippets = ([{"snippet": "x", "title": "t", "author": "a"}]
                    if with_snippets else None)
        view = vb.build_etymology_bundle(
            word="ajar",
            translation_ru="приоткрытый",
            pos="ADJ",
            definition_en="slightly open",
            etymology=etym,
            snippets=snippets,
        )
        r = ToolResult.success(
            tool="enrich_word", data={},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        vb.attach_view(r, view, data_validity=DataValidity.OK)
        return r

    def test_word_contexts_intent_picks_contexts_over_etym_bundle(self):
        """The Stan bug: intent=word_contexts, both views present,
        ETYMOLOGY_BUNDLE wins without intent bonus."""
        ctx_r = self._word_contexts_result(n=3)
        etym_r = self._etym_bundle_result(with_etym=False, with_snippets=False)
        # Without intent, ETYMOLOGY_BUNDLE (90) > WORD_CONTEXTS (70) — bug
        _, no_intent_pick = select_primary_view([ctx_r, etym_r])
        self.assertEqual(no_intent_pick.view_type, ViewType.ETYMOLOGY_BUNDLE,
                          "static priority makes ETYMOLOGY_BUNDLE win")
        # With intent=word_contexts, WORD_CONTEXTS (70+40=110) > ETYMOLOGY_BUNDLE (90)
        _, with_intent_pick = select_primary_view(
            [ctx_r, etym_r], intent="word_contexts")
        self.assertEqual(with_intent_pick.view_type, ViewType.WORD_CONTEXTS,
                          "intent=word_contexts must flip to WORD_CONTEXTS")

    def test_word_etymology_intent_keeps_etym_bundle(self):
        """For intent=word_etymology, ETYMOLOGY_BUNDLE should still win."""
        ctx_r = self._word_contexts_result(n=3)
        etym_r = self._etym_bundle_result(with_etym=True)
        _, pick = select_primary_view(
            [ctx_r, etym_r], intent="word_etymology")
        self.assertEqual(pick.view_type, ViewType.ETYMOLOGY_BUNDLE)

    def test_word_contexts_intent_empty_contexts_still_wins(self):
        """If WORD_CONTEXTS view is empty BUT has an empty_state
        explanation (built via vb.build_word_contexts with empty list),
        it's an «explained empty» view — the canonical answer for
        intent=word_contexts. The empty_state message «no occurrences
        of X in scope Y» IS the user-facing answer; better than an
        unrelated ETYMOLOGY_BUNDLE.

        This is intentional B-R14-3-class behavior: explained-empty
        views are not junk-empty (no -200 penalty), and intent bonus
        keeps WORD_CONTEXTS as primary."""
        empty_view = vb.build_word_contexts(
            word="ajar", contexts=[], scope_label="corpus",
        )
        # build_word_contexts auto-attaches empty_state via _legacy_empty
        self.assertIsNotNone(empty_view.empty_state)
        empty_r = ToolResult.success(
            tool="hybrid_search", data={},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        vb.attach_view(empty_r, empty_view,
                        data_validity=DataValidity.EMPTY_EXPECTED)
        etym_r = self._etym_bundle_result(with_etym=True)
        _, pick = select_primary_view(
            [empty_r, etym_r], intent="word_contexts")
        # WORD_CONTEXTS empty_state IS the answer for intent=word_contexts
        self.assertEqual(pick.view_type, ViewType.WORD_CONTEXTS)


class EtymologyLineSuppressed(unittest.TestCase):
    """Fix C — «Этимология не извлеклась» line removed from template."""

    def test_no_etymology_no_apology_line(self):
        from scripts.v2 import template_executor as te
        v = vb.build_etymology_bundle(
            word="ajar", translation_ru="приоткрытый", pos="ADJ",
        )
        s = te.render_view(v)
        self.assertNotIn("Этимология не извлеклась", s)
        self.assertNotIn("не извлеклась", s)
        # Translation + POS still render
        self.assertIn("приоткрытый", s)
        self.assertIn("ADJ", s)

    def test_etymology_present_still_renders(self):
        from scripts.v2 import template_executor as te
        v = vb.build_etymology_bundle(
            word="ajar", translation_ru="приоткрытый", pos="ADJ",
            etymology={"primary_family": "Germanic",
                       "family_chain": ["ang", "gem-pro"]},
        )
        s = te.render_view(v)
        self.assertIn("**Этимология:**", s)
        self.assertIn("Germanic", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
