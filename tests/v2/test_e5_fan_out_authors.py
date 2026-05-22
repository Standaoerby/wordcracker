"""E5 (R-22 P5) — multi-author fan-out for word-scope plan builders.

ROOT CAUSE: plan_word_emotion, plan_word_collocates used only
`e.author_regex` (primary), ignoring `e.multi_author_regex`. For
queries like «слова страха у По и Лавкрафта одновременно» the
secondary author was silently dropped.

Fix: `_fan_out_authors_steps` helper builds N+1 PlanSteps (primary
required + multi[:3] optional). Refactored 2 plan builders to use it.
(_plan_word_contexts already had fan-out; _plan_author_vocab too.
We extend to _plan_word_emotion and _plan_word_collocates.)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.plan import (
    _fan_out_authors_steps,
    _plan_word_collocates,
    _plan_word_emotion,
)


class FanOutHelperUnit(unittest.TestCase):
    def test_primary_only(self):
        e = Entities(author_regex="^Poe,", multi_author_regex=[])
        steps = _fan_out_authors_steps(
            e, tool="emotion_collocates",
            base_args={"emotion": "fear"}, scope_field="scope",
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].args["scope"], {"author": "^Poe,"})
        self.assertFalse(steps[0].optional)

    def test_primary_plus_two_multi(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,", "^Hawthorne,"],
        )
        steps = _fan_out_authors_steps(
            e, tool="emotion_collocates",
            base_args={"emotion": "fear"}, scope_field="scope",
        )
        self.assertEqual(len(steps), 3)
        scopes = [s.args["scope"]["author"] for s in steps]
        self.assertEqual(scopes, ["^Poe,", "^Lovecraft,", "^Hawthorne,"])
        # Primary required, others optional
        self.assertFalse(steps[0].optional)
        self.assertTrue(steps[1].optional)
        self.assertTrue(steps[2].optional)

    def test_caps_at_three_multi(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^A,", "^B,", "^C,", "^D,", "^E,"],
        )
        steps = _fan_out_authors_steps(
            e, tool="emotion_collocates",
            base_args={}, scope_field="scope", cap=3,
        )
        # 1 primary + 3 multi = 4 total
        self.assertEqual(len(steps), 4)

    def test_carries_filters(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            country="US", year_from=1800,
        )
        steps = _fan_out_authors_steps(
            e, tool="emotion_collocates",
            base_args={}, scope_field="scope",
        )
        self.assertEqual(steps[0].args["scope"]["country"], "US")
        self.assertEqual(steps[0].args["scope"]["year_from"], 1800)
        # Filters propagated to multi step too
        self.assertEqual(steps[1].args["scope"]["country"], "US")

    def test_author_field_mode(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
        )
        steps = _fan_out_authors_steps(
            e, tool="affinity_by_author",
            base_args={"top": 30}, author_field="author_regex",
        )
        # Should set author_regex directly, not scope dict
        self.assertEqual(steps[0].args["author_regex"], "^Poe,")
        self.assertEqual(steps[1].args["author_regex"], "^Lovecraft,")
        self.assertNotIn("scope", steps[0].args)

    def test_empty_when_no_primary(self):
        e = Entities(author_regex=None)
        steps = _fan_out_authors_steps(
            e, tool="emotion_collocates",
            base_args={}, scope_field="scope",
        )
        self.assertEqual(steps, [])


class PlanWordEmotionFanOut(unittest.TestCase):
    """E5 critical case — «слова страха у По и Лавкрафта одновременно»
    must produce 2 emotion_collocates calls, not 1."""

    def test_multi_author_fans_out(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            emotion="fear",
            raw_misc={"raw_text": "слова страха у По и Лавкрафта одновременно"},
        )
        plan = _plan_word_emotion(e)
        self.assertEqual(plan.intent, "word_emotion")
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["emotion_collocates", "emotion_collocates"])
        scopes = [s.args["scope"]["author"] for s in plan.steps]
        self.assertEqual(scopes, ["^Poe,", "^Lovecraft,"])

    def test_single_author_no_fanout(self):
        e = Entities(
            author_regex="^Poe,", multi_author_regex=[],
            emotion="fear",
            raw_misc={"raw_text": "слова страха у По"},
        )
        plan = _plan_word_emotion(e)
        self.assertEqual(len(plan.steps), 1)


class PlanWordCollocatesFanOut(unittest.TestCase):
    """E5 — same fan-out for word_collocates."""

    def test_multi_author_fans_out(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            word="fog",
            raw_misc={"raw_text": "соседи fog у По и Лавкрафта"},
        )
        plan = _plan_word_collocates(e)
        self.assertEqual(plan.intent, "word_collocates")
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["word_collocates", "word_collocates"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
