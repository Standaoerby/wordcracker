"""E16 — «примеры использования слова X» (word_contexts intent)
rendering bug.

ROOT CAUSE (2026-05-22 Stan prod):
Query «примеры использования слова "ajar" в английской литературе»
returned a useless ETYMOLOGY_BUNDLE card with translation + POS but
NO examples and a critic-flagged «Этимология не извлеклась» line.

THREE architectural bugs in cascade — the v5-renderer pieces have been
deleted in Phase 1 (2026-05-22, R1), so what remains testable here is
the tool-side fix (A) and the template-side fix (C). View selection
priority (B) lived in the deleted render_v5 — its tests are moot in
the legacy-only render path.

Surviving fixes:
  A. hybrid_search emits WORD_CONTEXTS view with snippets/title/author
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
