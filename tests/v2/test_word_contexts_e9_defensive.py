"""E9 (R-22 P9) — word_contexts must not render snippet=None as literal.

ROOT CAUSE: v1 `rag_tools.word_contexts` returns samples with field
`context` (NOT `snippet` or `text`). The v2 wrapper looked for
`snippet`/`text` only — empty string default propagated as None
through view payload, renderer stringified to «None».

These tests lock in the defensive contract at THREE layers:
  1. Wrapper reads `context` field (v1's actual key)
  2. View builder filters empty/None snippets
  3. Template renderer guards against None.strip() failure
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class WordContextsReadsContextField(unittest.TestCase):
    """v1 returns `samples[i].context`. Wrapper must read it (was reading
    `snippet`/`text` only)."""

    def test_v1_context_field_propagated_to_view(self):
        from scripts.v2.tools.words.contexts import _attach_word_contexts_view
        from scripts.v2._types import ToolResult

        result = ToolResult.success(tool="word_contexts", data={})
        samples = [
            {"pg_id": "PG108", "title": "Return of Sherlock Holmes",
             "context": "the heart of darkness lay before him"},
            {"pg_id": "PG108", "title": "Return of Sherlock Holmes",
             "context": "with a faint heart she advanced"},
        ]
        _attach_word_contexts_view(result, samples, word="heart",
                                     scope_label="Doyle")
        self.assertIsNotNone(result.view)
        ctxs = result.view.payload.get("contexts", [])
        self.assertEqual(len(ctxs), 2)
        # CRITICAL: snippet field has actual text, not empty
        self.assertIn("heart of darkness", ctxs[0]["snippet"])
        self.assertIn("faint heart", ctxs[1]["snippet"])


class WordContextsFiltersEmptyOrNone(unittest.TestCase):
    """When v1 returns samples with None/empty text — filter them out
    before view, don't propagate."""

    def test_none_snippet_filtered(self):
        from scripts.v2.tools.words.contexts import _attach_word_contexts_view
        from scripts.v2._types import ToolResult

        result = ToolResult.success(tool="word_contexts", data={})
        samples = [
            {"pg_id": "PG1", "title": "Book A", "context": None},
            {"pg_id": "PG2", "title": "Book B", "context": "valid snippet text"},
            {"pg_id": "PG3", "title": "Book C", "context": ""},
            {"pg_id": "PG4", "title": "Book D", "context": "  "},  # whitespace
        ]
        _attach_word_contexts_view(result, samples, word="heart",
                                     scope_label="test")
        ctxs = result.view.payload.get("contexts", [])
        # Only 1 valid sample retained
        self.assertEqual(len(ctxs), 1)
        self.assertEqual(ctxs[0]["snippet"], "valid snippet text")

    def test_all_empty_emits_empty_state(self):
        from scripts.v2.tools.words.contexts import _attach_word_contexts_view
        from scripts.v2._types import ToolResult

        result = ToolResult.success(tool="word_contexts", data={})
        samples = [
            {"pg_id": "PG1", "title": "X", "context": None},
            {"pg_id": "PG2", "title": "Y", "context": ""},
        ]
        _attach_word_contexts_view(result, samples, word="heart",
                                     scope_label="test")
        # View should have empty_state set (no contexts at all)
        self.assertIsNotNone(result.view)
        self.assertIsNotNone(
            result.view.empty_state,
            "All-empty samples must produce empty_state, not silent empty",
        )


class RendererGuardsAgainstNone(unittest.TestCase):
    """Even if view payload has None somewhere (defense in depth),
    renderer must NOT output literal «None»."""

    def test_render_skips_none_snippet(self):
        from scripts.v2.template_executor import _render_word_contexts
        from scripts.v2.view_types import RenderableView, ViewType

        view = RenderableView(
            view_type=ViewType.WORD_CONTEXTS,
            payload={
                "word": "heart",
                "scope_label": "Doyle",
                "contexts": [
                    {"snippet": None, "title": "X", "author": "Doyle"},
                    {"snippet": "real text", "title": "Y", "author": "Doyle"},
                    {"snippet": "", "title": "Z", "author": "Doyle"},
                ],
            },
            headline=None,
            caveats=[],
            language="ru",
        )
        out = _render_word_contexts(view)
        # MUST NOT contain literal «None»
        self.assertNotIn("None", out,
                          f"Rendered output should not contain «None»:\n{out}")
        # Should contain the real snippet
        self.assertIn("real text", out)

    def test_render_with_all_none_shows_fallback(self):
        from scripts.v2.template_executor import _render_word_contexts
        from scripts.v2.view_types import RenderableView, ViewType

        view = RenderableView(
            view_type=ViewType.WORD_CONTEXTS,
            payload={
                "word": "heart",
                "scope_label": "Doyle",
                "contexts": [
                    {"snippet": None, "title": "X", "author": "Doyle"},
                    {"snippet": None, "title": "Y", "author": "Doyle"},
                ],
            },
            headline=None,
            caveats=[],
            language="ru",
        )
        out = _render_word_contexts(view)
        self.assertNotIn("None", out)
        # Should explicitly note that text wasn't extracted
        self.assertIn("не извлёкся", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
