"""W-6 (2026-05-24) — friendly tool-error rendering. No raw JSON in chat.

Stan prod 2026-05-22 «примеры слова factory»: a hybrid_search call
returned `{matches: [], lexical_n: 0, semantic_n: 0}` and the LLM
renderer, faced with that empty envelope, JSON-dumped the payload back
into the chat answer.

The fix has two layers:
  1. `_has_useful_data` correctly identifies empty-collection payloads
     as «no data» even when surrounded by diagnostic scalars.
  2. `_dispatch_render` short-circuits to `_friendly_render_error`
     when every result fails that check — the LLM never sees the
     empty envelope, so it can't dump it.
  3. `_friendly_render_error` itself NEVER calls `to_llm_string()`
     (that's what produced JSON in prod). It writes plain Russian
     prose + compact per-tool counts.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import (
    Coverage, ToolError, ToolResult, ToolWarning,
)


# ---------------------------------------------------------------------------
# _has_useful_data
# ---------------------------------------------------------------------------

class HasUsefulDataEmptyMatches(unittest.TestCase):
    """The Stan «factory» payload — ok=True but matches is empty.
    Must register as «no useful data» so the friendly path fires."""

    def test_hybrid_search_empty_matches_is_not_useful(self):
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult.success(
            tool="hybrid_search",
            data={"query": "factory", "matches": [],
                  "lexical_n": 0, "semantic_n": 0, "k_rrf": 60},
        )
        self.assertFalse(_has_useful_data(r))

    def test_hybrid_search_with_matches_is_useful(self):
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult.success(
            tool="hybrid_search",
            data={"query": "factory",
                  "matches": [{"pg_id": "PG1", "snippet": "...", "title": "X"}],
                  "lexical_n": 1, "semantic_n": 0},
        )
        self.assertTrue(_has_useful_data(r))

    def test_ok_false_is_not_useful_regardless_of_data(self):
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult(
            ok=False, tool="hybrid_search",
            data={"matches": [{"pg_id": "PG1"}]},
            error=ToolError(type="internal", message="boom"),
        )
        self.assertFalse(_has_useful_data(r))


class HasUsefulDataScalarPayloads(unittest.TestCase):
    """enrich_word / find_book / book_readability emit scalar fields
    with no list keys. They must count as useful when they carry real
    content; not when they're all-metadata / all-empty."""

    def test_enrich_word_with_translation_is_useful(self):
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult.success(
            tool="enrich_word",
            data={"word": "factory", "translation_ru": "фабрика",
                  "pos": "NOUN", "definition_en": "a building..."},
        )
        self.assertTrue(_has_useful_data(r))

    def test_metadata_only_dict_is_not_useful(self):
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult.success(
            tool="enrich_word",
            data={"word": "factory", "query": "factory", "lang": "en"},
        )
        # All keys are metadata — no real content.
        self.assertFalse(_has_useful_data(r))

    def test_flesch_zero_score_is_useful(self):
        """Numeric 0 is a legitimate Flesch score — must not be
        treated as «empty». Guard against the `v in (None, '', [], {})`
        pitfall (0 == False, but != None / "" / [] / {})."""
        from scripts.v2.rag_v2 import _has_useful_data
        r = ToolResult.success(
            tool="book_readability",
            data={"flesch": 0.0, "cefr": "C2"},
        )
        self.assertTrue(_has_useful_data(r))


# ---------------------------------------------------------------------------
# _dispatch_render short-circuit
# ---------------------------------------------------------------------------

class DispatchRenderShortCircuit(unittest.TestCase):
    """When no result carries useful data, the LLM must NOT be invoked.
    Stan's prod failure mode: LLM saw an empty envelope and dumped it."""

    def _plan(self):
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities
        return QueryPlan(intent="word_contexts", entities=Entities(),
                         steps=[], explain="test")

    def test_no_useful_results_calls_friendly_path_without_llm(self):
        from scripts.v2 import rag_v2
        results = [
            ToolResult.success(
                tool="hybrid_search",
                data={"matches": [], "lexical_n": 0, "semantic_n": 0},
            ),
            ToolResult(
                ok=False, tool="enrich_word",
                error=ToolError(type="internal",
                                 message="wiktionary lookup failed"),
            ),
        ]
        # Sentinel — if _llm_render is called the test fails.
        def _boom(*a, **k):
            raise AssertionError(
                "_llm_render must not run when results carry no data")
        import unittest.mock as mock
        with mock.patch.object(rag_v2, "_llm_render", side_effect=_boom):
            answer, meta = rag_v2._dispatch_render(
                "примеры слова factory",
                self._plan(), results,
                model="qwen3:14b", ollama_host="http://nowhere",
            )
        self.assertIn("инструмент", answer.lower())
        self.assertEqual(meta.get("fallback_reason"),
                          "tools_returned_no_data")

    def test_at_least_one_useful_result_goes_through_llm(self):
        from scripts.v2 import rag_v2
        results = [
            ToolResult.success(
                tool="hybrid_search",
                data={"matches": [{"pg_id": "PG1", "snippet": "x",
                                    "title": "T"}]},
            ),
        ]
        import unittest.mock as mock
        with mock.patch.object(rag_v2, "_llm_render",
                                  return_value=("rendered", {})) as m:
            answer, meta = rag_v2._dispatch_render(
                "примеры слова factory",
                self._plan(), results,
                model="qwen3:14b", ollama_host="http://nowhere",
            )
        self.assertEqual(answer, "rendered")
        self.assertEqual(m.call_count, 1)


# ---------------------------------------------------------------------------
# _friendly_render_error — no JSON in chat
# ---------------------------------------------------------------------------

class FriendlyRenderErrorNeverDumpsJSON(unittest.TestCase):
    """The new _friendly_render_error must not emit JSON (no `{` / `[`
    surrounded by JSON-y content). Stan «factory» case used to paste
    full to_llm_string() output."""

    def _shape(self, text: str) -> None:
        # No JSON-object literals — the regression we're guarding.
        self.assertNotIn('"matches":', text)
        self.assertNotIn('"data":', text)
        self.assertNotIn("{\n", text)

    def test_all_failed_emits_short_russian_message(self):
        from scripts.v2.rag_v2 import (
            _friendly_render_error, _ToolPipelineEmpty,
        )
        results = [
            ToolResult(
                ok=False, tool="hybrid_search",
                error=ToolError(type="internal", message="boom"),
            ),
        ]
        out = _friendly_render_error(
            _ToolPipelineEmpty("hybrid_search: boom"), results)
        self._shape(out)
        # Mentions the tool by name but in a sentence, not a JSON key.
        self.assertIn("hybrid_search", out)
        # The lead is the friendly Russian one-liner.
        self.assertTrue("инструмент" in out.lower()
                          or "ollama" in out.lower())

    def test_partial_success_lists_counts_not_payload(self):
        from scripts.v2.rag_v2 import _friendly_render_error
        results = [
            ToolResult.success(
                tool="hybrid_search",
                data={"matches": [
                    {"pg_id": "PG1", "snippet": "x", "title": "T1"},
                    {"pg_id": "PG2", "snippet": "y", "title": "T2"},
                ]},
            ),
        ]
        out = _friendly_render_error(RuntimeError("ollama down"), results)
        self._shape(out)
        # Compact count summary, not the snippets themselves.
        self.assertIn("hybrid_search", out)
        # The bullet should say "найдено совпадений: 2" (no JSON).
        self.assertIn("2", out)

    def test_scalar_payload_lists_field_names_not_values(self):
        from scripts.v2.rag_v2 import _friendly_render_error
        results = [
            ToolResult.success(
                tool="enrich_word",
                data={"word": "factory", "translation_ru": "фабрика",
                      "pos": "NOUN", "definition_en": "a building..."},
            ),
        ]
        out = _friendly_render_error(RuntimeError("renderer crash"), results)
        self._shape(out)
        # Tool name + a hint of field names — values are NOT pasted.
        self.assertIn("enrich_word", out)
        # The translation value itself isn't in the friendly message.
        self.assertNotIn("a building", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
