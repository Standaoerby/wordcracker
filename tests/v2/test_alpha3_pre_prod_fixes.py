"""Sprint 21 (v3.2.0-alpha3) — pre-prod bugfix iteration.

Closes B100 (PG ids → titles), B101 (word display completeness),
B102 (copyright refusal mentions upload option), B104 (network error
soft fix).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.planner.entities import extract
from scripts.v2.planner.plan import (
    _copyright_refusal_if_book_under_copyright,
    _plan_word_contexts,
    _plan_word_etymology,
)


class B100_LexicalSearchTitleEnrichment(unittest.TestCase):
    """When lexical_search returns matches, each row should carry `title`
    + `author` from v1 metadata when the pg_id is resolvable. Closes
    the «renderer echoes PG2554 instead of Crime and Punishment» bug."""

    def test_title_lookup_attaches_to_matches(self):
        from scripts.v2.tools.search import lexical
        # Fake v1 metadata: return a synthetic title-lookup map.
        fake_lookup = {
            "PG1342": {"title": "Pride and Prejudice", "author": "Austen, Jane"},
            "PG2554": {"title": "Crime and Punishment", "author": "Dostoyevsky, Fyodor"},
        }
        # Fake DB rows (only id/score/snippet — title gets added by lookup)
        class FakeRow:
            def __init__(self, d): self._d = d
            def __getitem__(self, k): return self._d[k]
        fake_rows = [
            FakeRow({"id": "PG1342", "score": -1.5, "snippet": "[universally] acknowledged"}),
            FakeRow({"id": "PG2554", "score": -1.8, "snippet": "[axe] in the [pawnbroker]"}),
            FakeRow({"id": "PG9999", "score": -0.9, "snippet": "unknown"}),
        ]
        class FakeConn:
            def execute(self, sql, args): return self
            def fetchall(self): return fake_rows
        with mock.patch.object(lexical, "_connect", return_value=FakeConn()), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = lexical.lexical_search("axe", k=3)
        matches = r.data["matches"]
        self.assertEqual(len(matches), 3)
        # Resolvable pg_ids get title/author
        self.assertEqual(matches[0]["title"], "Pride and Prejudice")
        self.assertEqual(matches[0]["author"], "Austen, Jane")
        self.assertEqual(matches[1]["title"], "Crime and Punishment")
        # Unknown pg_id keeps base shape without title/author
        self.assertEqual(matches[2]["pg_id"], "PG9999")
        self.assertNotIn("title", matches[2])

    def test_title_lookup_handles_missing_v1(self):
        """When v1 import fails, _title_lookup returns empty dict —
        matches still carry pg_id but no title (renderer note rule 16
        handles this gracefully)."""
        from scripts.v2.tools.search import lexical
        # Lookup just returns {} — same as v1 import failure path.
        class FakeRow:
            def __init__(self, d): self._d = d
            def __getitem__(self, k): return self._d[k]
        fake_rows = [FakeRow({"id": "PG1", "score": -1.0, "snippet": "x"})]
        class FakeConn:
            def execute(self, sql, args): return self
            def fetchall(self): return fake_rows
        with mock.patch.object(lexical, "_connect", return_value=FakeConn()), \
             mock.patch.object(lexical, "_title_lookup", return_value={}):
            r = lexical.lexical_search("x", k=1)
        m = r.data["matches"][0]
        self.assertEqual(m["pg_id"], "PG1")
        self.assertNotIn("title", m)


class B100_RenderPromptRule(unittest.TestCase):
    """RENDER_PROMPT must teach the LLM to prefer titles over PG ids."""

    def test_rule_16_present_with_keywords(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Book titles > PG ids", RENDER_PROMPT)
        self.assertIn("отдавать не айдишники", RENDER_PROMPT)


class B101_WordPlanComposite(unittest.TestCase):
    """When user asks about a word with no author scope, the plan should
    fan out hybrid_search (examples) + enrich_word (translation +
    etymology) in parallel. Closes B101."""

    def test_word_contexts_no_author_fans_to_enrich(self):
        e = extract("примеры слова tuppence")
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("hybrid_search", tools)
        self.assertIn("enrich_word", tools)

    def test_word_etymology_with_word_fans_to_contexts(self):
        e = extract("этимология слова tuppence")
        plan = _plan_word_etymology(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("word_etymology", tools)
        # hybrid_search added for parallel context fetch
        self.assertIn("hybrid_search", tools)

    def test_enrich_word_step_is_optional(self):
        """enrich_word Wiktionary outage shouldn't kill the context query."""
        e = extract("найди слово stitching")
        plan = _plan_word_contexts(e)
        enrich_steps = [s for s in plan.steps if s.tool == "enrich_word"]
        self.assertTrue(enrich_steps)
        self.assertTrue(enrich_steps[0].optional)

    def test_word_contexts_with_author_unchanged(self):
        """Author-scoped path is unchanged — enrich_word fan-out only
        kicks in for the no-author general lookup case (where user
        asked about a word standalone)."""
        e = extract("примеры ajar у Доила")
        # Doyle may or may not resolve depending on aliases; force it
        e.author_regex = "^Doyle, Arthur"
        e.word = "ajar"
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("word_contexts", tools)
        # No enrich_word fan-out for author-scoped queries — kept focused
        self.assertNotIn("enrich_word", tools)


class B101_RenderPromptRule(unittest.TestCase):

    def test_rule_17_word_bundle_present(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("translation + примеры + этимология", RENDER_PROMPT)
        self.assertIn("B101", RENDER_PROMPT)


class B102_CopyrightRefusalUploadMention(unittest.TestCase):
    """The OOS refusal now lists BOTH options the user requested:
    «либо в базе ограниченно (через uploads), либо только мета»."""

    def test_lotr_refusal_mentions_upload(self):
        e = extract('слова из "The Lord of the Rings"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan)
        reason = plan.out_of_scope_reason
        # Both options surfaced
        self.assertIn("Мета-информация", reason)
        self.assertIn("загруженной локально", reason)
        self.assertIn("fair use", reason)
        # And the upload-via-admin path is hinted
        self.assertIn("/admin/", reason)

    def test_1984_refusal_same_shape(self):
        e = extract('частоты в "1984"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan)
        self.assertIn("Мета-информация", plan.out_of_scope_reason)
        self.assertIn("загруженной локально", plan.out_of_scope_reason)


class B104_FriendlyRenderError(unittest.TestCase):
    """Renderer LLM failure now yields a user-friendly message + tool
    data summary instead of `[renderer error: <traceback>]`."""

    def test_timeout_message_is_friendly(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(TimeoutError("read timed out"))
        self.assertIn("Ollama", m)
        self.assertIn("ещё раз", m)

    def test_connection_error_message(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(
            ConnectionError("connection refused"))
        self.assertIn("Ollama", m)
        self.assertIn("недоступен", m)

    def test_generic_error_fallback(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(ValueError("weird state"))
        self.assertIn("ValueError", m)

    def test_friendly_render_error_includes_tool_summaries(self):
        """When the renderer dies, the user should still see SOMETHING
        from the tools that succeeded — not just an error string."""
        from scripts.v2.rag_v2 import _friendly_render_error
        fake_result = ToolResult.success(
            tool="top_books_by_downloads",
            data={"top": [{"pg_id": "PG1342", "title": "Pride and Prejudice"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        msg = _friendly_render_error(TimeoutError("ollama timed out"),
                                      [fake_result])
        self.assertIn("Ollama", msg)
        self.assertIn("top_books_by_downloads", msg)

    def test_friendly_render_error_no_tools_succeeded(self):
        from scripts.v2.rag_v2 import _friendly_render_error
        msg = _friendly_render_error(ConnectionError("refused"), [])
        self.assertIn("инструменты тоже не дали", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
