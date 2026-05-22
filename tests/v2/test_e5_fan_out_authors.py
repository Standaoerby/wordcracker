"""E5 — multi-author fan-out as a router-level invariant.

Phase 4 (REFACTOR_BRIEF) — fan-out lives in `planner/invariants.py`
and runs at `router.execute()`. Builders emit a SINGLE step with a
`fan_out` marker; the invariant clones it per
`entities.multi_author_regex[:cap]`.

Closes E5 root cause structurally: every builder that opts in via the
marker gets identical fan-out behavior — no more per-builder copies
to drift apart (Sprint 17 R7 Q8 «примеры ajar у Остин/Диккенса/Дойла»
silently dropping secondaries was the historical motivation).

Tests below verify:
  * the `_fan_out_authors_steps` backward-compat shim still produces
    the same N+1 step list (since the invariant powers it);
  * builders mark their primary step correctly;
  * applying the invariant expands the marker into per-author clones;
  * POS and etymology now fan out alongside emotion / collocates /
    contexts / author_vocab (gate of Phase 4).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.invariants import apply_fan_out_invariant
from scripts.v2.planner.plan import (
    QueryPlan,
    _fan_out_authors_steps,
    _plan_author_vocab,
    _plan_word_collocates,
    _plan_word_contexts,
    _plan_word_emotion,
    _plan_word_etymology,
    _plan_word_pos,
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


def _expand(plan: QueryPlan) -> QueryPlan:
    """Apply the router-level fan-out invariant in tests."""
    return apply_fan_out_invariant(plan)


class PlanWordEmotionFanOut(unittest.TestCase):
    """E5 critical case — «слова страха у По и Лавкрафта одновременно»
    must produce 2 emotion_collocates calls after invariant expansion."""

    def test_builder_marks_primary(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            emotion="fear",
            raw_misc={"raw_text": "слова страха у По и Лавкрафта одновременно"},
        )
        plan = _plan_word_emotion(e)
        # Builder no longer fans out itself — it marks the primary step.
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].fan_out, "scope_author")

    def test_invariant_expands_to_two(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            emotion="fear",
            raw_misc={"raw_text": "слова страха у По и Лавкрафта одновременно"},
        )
        plan = _expand(_plan_word_emotion(e))
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
        plan = _expand(_plan_word_emotion(e))
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
        plan = _expand(_plan_word_collocates(e))
        self.assertEqual(plan.intent, "word_collocates")
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["word_collocates", "word_collocates"])


class PlanWordContextsFanOut(unittest.TestCase):
    """word_contexts author-scoped branch (Sprint 17 Q8) — same invariant."""

    def test_invariant_expands_for_word_contexts(self):
        e = Entities(
            author_regex="^Austen,",
            multi_author_regex=["^Dickens,", "^Doyle,"],
            word="ajar",
        )
        plan = _expand(_plan_word_contexts(e))
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["word_contexts"] * 3)
        authors = [s.args["author_regex"] for s in plan.steps]
        self.assertEqual(authors, ["^Austen,", "^Dickens,", "^Doyle,"])


class PlanAuthorVocabFanOut(unittest.TestCase):
    """author_vocab fan-out via the same invariant."""

    def test_invariant_expands_for_author_vocab(self):
        e = Entities(
            author_regex="^Melville,",
            multi_author_regex=["^Conrad,", "^Stevenson,"],
        )
        plan = _expand(_plan_author_vocab(e))
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["affinity_by_author"] * 3)
        authors = [s.args["author_regex"] for s in plan.steps]
        self.assertEqual(authors, ["^Melville,", "^Conrad,", "^Stevenson,"])


class GateNewBuildersGetFanOutForFree(unittest.TestCase):
    """Phase 4 gate — «запрос X у автора-1 и автора-2 работает для
    POS/этимологии/эмоций одинаково (закрывает E5 структурно)».

    POS and etymology were NOT fanned out before Phase 4. By adding
    only a `fan_out` marker to their builders' primary step, they now
    behave identically to emotion / collocates / contexts."""

    def test_word_pos_fans_out(self):
        e = Entities(
            author_regex="^Wodehouse,",
            multi_author_regex=["^Twain,"],
            word="light",
        )
        plan = _expand(_plan_word_pos(e))
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["word_pos_distribution"] * 2)
        authors = [s.args["scope"]["author"] for s in plan.steps]
        self.assertEqual(authors, ["^Wodehouse,", "^Twain,"])

    def test_word_etymology_fans_out_author_family(self):
        e = Entities(
            author_regex="^Tolkien,",
            multi_author_regex=["^Morris,"],
            etymology_family="germanic",
        )
        plan = _expand(_plan_word_etymology(e))
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["find_words_by_etymology"] * 2)
        authors = [s.args["scope"]["author"] for s in plan.steps]
        self.assertEqual(authors, ["^Tolkien,", "^Morris,"])


class FanOutInvariantIdempotency(unittest.TestCase):
    """Applying the invariant twice must be a no-op."""

    def test_idempotent(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            emotion="fear",
        )
        plan_once = _expand(_plan_word_emotion(e))
        first_tools = [s.tool for s in plan_once.steps]
        plan_twice = _expand(plan_once)
        second_tools = [s.tool for s in plan_twice.steps]
        self.assertEqual(first_tools, second_tools)


class NoBuilderImplementsFanOut(unittest.TestCase):
    """R6 gate — no `_plan_*` may re-implement fan-out. Verifies
    every builder hits the multi-author branch with len(steps) ≤ 2 raw
    (primary + maybe an unrelated step like enrich) BEFORE the
    invariant runs. After expansion the count grows.

    Strategy: pick a multi-author entity and assert the builder's RAW
    output has at most one author-bearing step before invariant.
    """

    def _assert_single_primary(self, plan: QueryPlan, *, field: str):
        # Find steps that reference the primary author and assert at
        # most one — anything more means the builder is doing its own
        # fan-out and bypassing the invariant (R6 violation).
        author_steps = [
            s for s in plan.steps
            if (field == "scope_author"
                and isinstance(s.args.get("scope"), dict)
                and "author" in s.args["scope"])
            or (field == "author_regex" and "author_regex" in s.args)
        ]
        self.assertLessEqual(
            len(author_steps), 1,
            f"builder re-implemented fan-out: {len(author_steps)} "
            f"author-bearing steps in raw plan (must be ≤1)",
        )

    def test_word_emotion_single_primary(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,", "^Hawthorne,"],
            emotion="fear",
        )
        self._assert_single_primary(_plan_word_emotion(e), field="scope_author")

    def test_word_collocates_single_primary(self):
        e = Entities(
            author_regex="^Poe,",
            multi_author_regex=["^Lovecraft,"],
            word="fog",
        )
        self._assert_single_primary(_plan_word_collocates(e), field="scope_author")

    def test_word_contexts_single_primary(self):
        e = Entities(
            author_regex="^Austen,",
            multi_author_regex=["^Dickens,", "^Doyle,"],
            word="ajar",
        )
        self._assert_single_primary(_plan_word_contexts(e), field="author_regex")

    def test_author_vocab_single_primary(self):
        e = Entities(
            author_regex="^Melville,",
            multi_author_regex=["^Conrad,"],
        )
        self._assert_single_primary(_plan_author_vocab(e), field="author_regex")


if __name__ == "__main__":
    unittest.main(verbosity=2)
