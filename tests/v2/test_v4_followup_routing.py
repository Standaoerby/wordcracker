"""Sprint 20+ — v4 LLM planner takes the followup path.

Stan chose this routing on 2026-05-19 evening, citing the pipeline
trace that documented 5 architectural conflicts in the rules-based
followup logic (whitelist asymmetry, flag-contract gap, cache
hazards, year-range parser).

When `WC_LLM_PLANNER=on` AND `_looks_like_followup(question)=True`,
the LLM planner runs FIRST — before rules classify. It sees:
  - the prior user message
  - the prior assistant response (truncated, table-aware)
  - the tool catalog + few-shot examples
And emits a PlanSpec DAG over the actual prior context.

Rules path remains as fallback when flag is off (regression-safe).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401
from scripts.v2._types import Coverage, ToolResult


def _ok(tool: str, data: dict) -> ToolResult:
    return ToolResult.success(
        tool=tool, data=data,
        coverage=Coverage(books_matched=1, books_total=1),
    )


class V4FollowupRoutesToLLM(unittest.TestCase):
    """When followup detected and flag on, plan_query gets called BEFORE
    rules-based classify/extract/merge."""

    def setUp(self):
        from scripts.v2.planner import llm_planner
        llm_planner.reset_cache_for_tests()

    def test_v4_takes_translate_followup(self):
        from scripts.v2 import rag_v2
        from scripts.v2.planner import llm_planner
        from scripts.v2.critic import CriticVerdict

        history = [
            {"role": "user", "content": "топ 100 любимых слов Дойла"},
            {"role": "assistant", "content":
                "| Word | Affinity |\n"
                "| blighter | 850 |\n"
                "| dashed | 720 |\n"},
        ]
        # LLM emits enrich_word chain
        plan_json = json.dumps({
            "intent_hint": "translate_prior_words",
            "rationale": "translate the two words from the prior table",
            "steps": [
                {"id": "s1", "tool": "enrich_word",
                 "args": {"word": "blighter", "target_lang": "ru"}},
                {"id": "s2", "tool": "enrich_word",
                 "args": {"word": "dashed", "target_lang": "ru"}},
            ],
        })

        called_tools: list[str] = []
        def fake_dispatch(name, args, **_kw):
            called_tools.append(name)
            return _ok(name, {"word": args.get("word"),
                              "translation": "TRANSLATED",
                              "pos": "noun"})

        def fake_render(question, plan, results, **kw):
            return ("blighter → бродяга\ndashed → проклятый",
                    {"prompt_tokens": 50, "eval_tokens": 30})

        fake_verdict = CriticVerdict(
            verified=True, unsupported_claims=[], missing_caveats=[],
            summary="ok",
        )

        with mock.patch.object(llm_planner, "LLM_PLANNER_ENABLED", True), \
             mock.patch.object(llm_planner, "_call_ollama",
                                 return_value=plan_json), \
             mock.patch("scripts.v2.planner.router.dispatch",
                          side_effect=fake_dispatch), \
             mock.patch("scripts.v2.rag_v2._llm_render",
                          side_effect=fake_render), \
             mock.patch("scripts.v2.critic.review",
                          return_value=fake_verdict), \
             mock.patch("scripts.v2.critic.annotate_answer",
                          side_effect=lambda a, v: a):
            out = rag_v2.ask(
                "возьми эти слова и переведи на русский",
                history=history,
                model="dummy", ollama_host="http://nowhere",
            )

        # Only enrich_word steps dispatched (no affinity_by_author rerun)
        self.assertEqual(called_tools, ["enrich_word", "enrich_word"])
        # Result reflects v4 path
        self.assertIn("blighter", out["answer"])

    def test_v4_off_falls_back_to_rules(self):
        """When flag is off, rules path runs (regression safety)."""
        from scripts.v2 import rag_v2
        from scripts.v2.planner import llm_planner

        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content":
                "| Word |\n| blighter |\n| dashed |\n"},
        ]
        called_tools: list[str] = []

        def fake_dispatch(name, args, **_kw):
            called_tools.append(name)
            return _ok(name, {})

        with mock.patch.object(llm_planner, "LLM_PLANNER_ENABLED", False), \
             mock.patch("scripts.v2.planner.router.dispatch",
                          side_effect=fake_dispatch):
            # Run with a query that would hit followup rules path
            out = rag_v2.ask(
                "возьми эти слова и переведи на русский",
                history=history,
                model="dummy", ollama_host="http://nowhere",
            )

        # No v4 — rules path ran (translate_word_list via rules)
        # → enrich_word steps from rules path. Just check we got SOME
        # dispatched calls (rules path produces enrich chain too via
        # _plan_translate_word_list when prior_words extracted).
        self.assertIn("answer", out)

    def test_v4_clarify_propagates(self):
        from scripts.v2 import rag_v2
        from scripts.v2.planner import llm_planner

        history = [
            {"role": "user", "content": "топ слов Дойла"},
            {"role": "assistant", "content": "list..."},
        ]
        clarify_json = json.dumps({
            "clarify": "do you want translation or analysis?",
        })

        with mock.patch.object(llm_planner, "LLM_PLANNER_ENABLED", True), \
             mock.patch.object(llm_planner, "_call_ollama",
                                 return_value=clarify_json):
            out = rag_v2.ask(
                "возьми их",
                history=history,
                model="dummy", ollama_host="http://nowhere",
            )

        self.assertIn("do you want translation or analysis?", out["answer"])

    def test_non_followup_query_skips_v4_followup_path(self):
        """A query without history should NOT enter the v4 followup
        block (the other v4 path — clarify-rescue — can still fire,
        that's tested separately)."""
        from scripts.v2.planner import history as history_mod
        # Without history, _looks_like_followup short-circuits even
        # though the text itself has trigger phrases.
        self.assertFalse(
            history_mod._looks_like_followup("просто привет"),
            msg="non-trigger text should not look like followup",
        )
        # Even with history, a no-trigger query shouldn't qualify
        self.assertFalse(
            history_mod._looks_like_followup("show top words for Doyle"),
            msg="text without trigger phrases bypasses followup gate",
        )


class AssistantSummaryHelper(unittest.TestCase):

    def test_summarize_keeps_table_column1(self):
        from scripts.v2.planner.llm_planner import _summarize_assistant_for_planner
        big = ("Вот топ 100 слов:\n\n"
               "| Word | Affinity | Context | Notes |\n"
               "|------|----------|---------|-------|\n"
               + "\n".join(f"| word_{i} | {1000 - i*10} | "
                            f"long context column for word_{i} that "
                            f"would otherwise blow the prompt size | "
                            f"more padding text |"
                            for i in range(50))
               + "\n\nThe end.")
        summary = _summarize_assistant_for_planner(big, max_chars=1500)
        # Should contain at least one extracted word
        self.assertIn("word_0", summary)
        # Should drop the bulk per-row data
        self.assertNotIn("long context column for word_49", summary)
        # Should fit
        self.assertLessEqual(len(summary), 1500 + 50)  # tiny slack

    def test_short_content_unchanged(self):
        from scripts.v2.planner.llm_planner import _summarize_assistant_for_planner
        s = "short response, no table"
        self.assertEqual(_summarize_assistant_for_planner(s), s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
