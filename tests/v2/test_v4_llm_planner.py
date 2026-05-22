"""v4 — LLM planner module.

We don't run real Ollama in unit tests — patch the `_call_ollama` helper
to return canned JSON. That keeps coverage focused on parsing, retry,
validation, and cache behavior, not on Ollama latency.

The integration test (real Ollama call) is a separate, opt-in script
behind WC_INTEGRATION_TESTS=1 — not part of the unit suite.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401 — register tools


class DisabledByMonkeyPatch(unittest.TestCase):
    """Phase 1 (2026-05-22) — WC_LLM_PLANNER env gate was removed; the
    flag is now a constant True. The early-return path stays in place
    for safety and can still be exercised by monkey-patching
    `LLM_PLANNER_ENABLED` to False (admin emergency disable)."""

    def test_returns_none_when_disabled(self):
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "LLM_PLANNER_ENABLED", False):
            res = llm_planner.plan_query("anything")
        self.assertIsNone(res)


def _enable_flag():
    # Phase 1 — flag is a constant True; helper kept as a no-op so test
    # body diffs stay small.
    pass


def _disable_flag():
    pass


class JsonParsing(unittest.TestCase):

    def setUp(self):
        _enable_flag()

    def tearDown(self):
        _disable_flag()

    def test_strict_json_parsed(self):
        from scripts.v2.planner.llm_planner import _parse_json
        obj = _parse_json('{"steps": [{"id": "s1", "tool": "find_book", '
                          '"args": {"title": "x"}}]}')
        self.assertIsNotNone(obj)
        self.assertEqual(obj["steps"][0]["id"], "s1")

    def test_markdown_fence_stripped(self):
        from scripts.v2.planner.llm_planner import _parse_json
        wrapped = '```json\n{"clarify": "what?"}\n```'
        obj = _parse_json(wrapped)
        self.assertEqual(obj, {"clarify": "what?"})

    def test_leading_prose_recovered(self):
        from scripts.v2.planner.llm_planner import _parse_json
        text = "Here is the plan: {\"clarify\": \"what?\"}"
        obj = _parse_json(text)
        self.assertEqual(obj, {"clarify": "what?"})

    def test_unbalanced_returns_none(self):
        from scripts.v2.planner.llm_planner import _parse_json
        self.assertIsNone(_parse_json('{"steps": [unclosed'))


class HappyPath(unittest.TestCase):

    def setUp(self):
        _enable_flag()
        from scripts.v2.planner import llm_planner
        llm_planner.reset_cache_for_tests()

    def tearDown(self):
        _disable_flag()

    def test_valid_plan_returns_ok(self):
        valid = json.dumps({
            "intent_hint": "book_readability",
            "steps": [
                {"id": "s1", "tool": "resolve_book_title",
                 "args": {"query": "Pride and Prejudice"}},
                {"id": "s2", "tool": "book_readability",
                 "args": {"pg_id": "$s1.pg_id"},
                 "needs": ["s1"]},
            ],
        })
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "_call_ollama",
                                return_value=valid):
            res = llm_planner.plan_query("уровень сложности P&P")
        self.assertIsNotNone(res)
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts, 1)
        self.assertEqual(len(res.plan.steps), 2)
        self.assertEqual(res.plan.steps[0].tool, "resolve_book_title")

    def test_clarify_only_plan_returns_clarify(self):
        clarify = json.dumps({"clarify": "what book do you mean?"})
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "_call_ollama",
                                return_value=clarify):
            res = llm_planner.plan_query("расскажи про вампиров")
        self.assertIsNotNone(res)
        self.assertFalse(res.ok)  # ok=False because clarify present
        self.assertEqual(res.clarify, "what book do you mean?")
        self.assertEqual(res.plan.clarify, "what book do you mean?")

    def test_cache_hit_on_second_call(self):
        valid = json.dumps({
            "steps": [{"id": "s1", "tool": "resolve_book_title",
                       "args": {"query": "Beowulf"}}],
        })
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "_call_ollama",
                                return_value=valid) as patched:
            r1 = llm_planner.plan_query("test query")
            r2 = llm_planner.plan_query("test query")
        self.assertEqual(patched.call_count, 1)
        self.assertTrue(r1.ok)
        self.assertTrue(r2.ok)


class RetryAndFallback(unittest.TestCase):

    def setUp(self):
        _enable_flag()
        from scripts.v2.planner import llm_planner
        llm_planner.reset_cache_for_tests()

    def tearDown(self):
        _disable_flag()

    def test_invalid_then_valid_succeeds(self):
        """First call returns garbage JSON; retry recovers."""
        from scripts.v2.planner import llm_planner
        valid = json.dumps({
            "steps": [{"id": "s1", "tool": "resolve_book_title",
                       "args": {"query": "Beowulf"}}],
        })
        responses = iter(["not json at all { broken", valid])
        with mock.patch.object(llm_planner, "_call_ollama",
                                side_effect=lambda _msg: next(responses)):
            res = llm_planner.plan_query("test query for retry")
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts, 2)

    def test_unknown_tool_then_valid_succeeds(self):
        from scripts.v2.planner import llm_planner
        bad = json.dumps({
            "steps": [{"id": "s1", "tool": "made_up_tool", "args": {}}],
        })
        good = json.dumps({
            "steps": [{"id": "s1", "tool": "resolve_book_title",
                       "args": {"query": "Beowulf"}}],
        })
        responses = iter([bad, good])
        with mock.patch.object(llm_planner, "_call_ollama",
                                side_effect=lambda _msg: next(responses)):
            res = llm_planner.plan_query("test unknown then good")
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts, 2)

    def test_all_attempts_fail_returns_clarify(self):
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "_call_ollama",
                                return_value="absolute garbage"):
            res = llm_planner.plan_query("hopeless query")
        self.assertIsNotNone(res)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.clarify)
        self.assertEqual(res.attempts, 2)

    def test_empty_response_treated_as_failure(self):
        from scripts.v2.planner import llm_planner
        with mock.patch.object(llm_planner, "_call_ollama",
                                return_value=""):
            res = llm_planner.plan_query("query that gets no reply")
        self.assertIsNotNone(res)
        self.assertFalse(res.ok)


class HistoryThreaded(unittest.TestCase):

    def setUp(self):
        _enable_flag()
        from scripts.v2.planner import llm_planner
        llm_planner.reset_cache_for_tests()

    def tearDown(self):
        _disable_flag()

    def test_previous_user_message_in_prompt(self):
        from scripts.v2.planner import llm_planner
        captured_msgs: list[str] = []
        def fake_call(user_msg: str) -> str:
            captured_msgs.append(user_msg)
            return json.dumps({"clarify": "ok"})
        with mock.patch.object(llm_planner, "_call_ollama",
                                side_effect=fake_call):
            llm_planner.plan_query(
                "а у Wodehouse?",
                history=[
                    {"role": "user", "content": "фирменные слова Doyle"},
                    {"role": "assistant", "content": "..."},
                ],
            )
        self.assertEqual(len(captured_msgs), 1)
        self.assertIn("Doyle", captured_msgs[0])
        self.assertIn("Wodehouse", captured_msgs[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
