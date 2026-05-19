"""Sprint 20 — result-modifier follow-up «убери из них имена собственные».

Stan 2026-05-19 screenshot:
  Turn 1 (assumed): «фирменные слова Дойла» → affinity_by_author returns
    list, but some character surnames may still slip through the
    v3.1.1 blocklist (obscure characters not in curated set + not in
    PG metadata authors).
  Turn 2: «убери из них имена собственные» → fell to clarify
    («Не уверен, что ты имеешь в виду…»).

Rules-based pipeline can't re-run a prior tool with stricter filters
on its own — no result-modifier infrastructure. This patch adds
recognition for propn-removal modifiers + a `_propn_strict` flag that
plan templates honor by cranking `min_corpus_count` up to 5000.

Long-term: v4 LLM planner sees the full history and can compose this
without per-modifier patches.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import history as history_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class PropnRemovalRecognition(unittest.TestCase):

    def test_pattern_matches_ru(self):
        from scripts.v2.planner.history import _PROPN_REMOVAL_PATTERNS
        for text in [
            "убери из них имена собственные",
            "убери имена собственные",
            "без имён собственных",
            "выкини фамилии",
            "отфильтруй фамилии",
            "исключи имена авторов",
        ]:
            self.assertTrue(_PROPN_REMOVAL_PATTERNS.search(text), text)

    def test_pattern_matches_en(self):
        from scripts.v2.planner.history import _PROPN_REMOVAL_PATTERNS
        for text in [
            "remove proper nouns",
            "exclude surnames",
            "drop character names",
            "filter out proper nouns",
            "drop them from the list",  # too generic — should NOT match
        ]:
            if text == "drop them from the list":
                self.assertFalse(_PROPN_REMOVAL_PATTERNS.search(text), text)
            else:
                self.assertTrue(_PROPN_REMOVAL_PATTERNS.search(text), text)

    def test_unrelated_followup_does_not_match(self):
        from scripts.v2.planner.history import _PROPN_REMOVAL_PATTERNS
        for text in [
            "приведи примеры",
            "отсортируй по убыванию",
            "теперь у Wodehouse",
            "покажи все",
        ]:
            self.assertFalse(_PROPN_REMOVAL_PATTERNS.search(text), text)


class FollowupIntentInference(unittest.TestCase):

    def test_inherits_prior_intent(self):
        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "..."},
        ]
        inferred = history_mod.infer_followup_intent(
            "убери из них имена собственные", history,
        )
        self.assertEqual(inferred, "author_vocab")

    def test_no_history_no_inference(self):
        inferred = history_mod.infer_followup_intent(
            "убери из них имена собственные", history=None,
        )
        self.assertIsNone(inferred)

    def test_prior_clarify_skipped(self):
        """If prior turn was itself a clarify (no real intent), we
        don't anchor onto it — the modifier becomes ambiguous."""
        history = [
            {"role": "user", "content": "asdfqwer ghjkl 123"},
            {"role": "assistant", "content": "(clarify)"},
        ]
        inferred = history_mod.infer_followup_intent(
            "убери из них имена собственные", history,
        )
        self.assertIsNone(inferred)


class MergeSetsPropnStrictFlag(unittest.TestCase):

    def test_flag_set_on_propn_removal(self):
        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "..."},
        ]
        e = ent_mod.extract("убери из них имена собственные")
        merged = history_mod.merge_with_history(
            e, history, "убери из них имена собственные",
        )
        self.assertTrue((merged.raw_misc or {}).get("_propn_strict"))
        # Author backfilled from prior turn
        self.assertEqual(merged.author_regex, "^Doyle,")

    def test_flag_not_set_on_regular_followup(self):
        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "..."},
        ]
        e = ent_mod.extract("приведи примеры")
        merged = history_mod.merge_with_history(
            e, history, "приведи примеры",
        )
        self.assertFalse((merged.raw_misc or {}).get("_propn_strict"))


class PlanRespectsPropnStrict(unittest.TestCase):

    def test_min_corpus_count_bumped(self):
        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "..."},
        ]
        e = ent_mod.extract("убери из них имена собственные")
        merged = history_mod.merge_with_history(
            e, history, "убери из них имена собственные",
        )
        plan = plan_mod.build("author_vocab", merged)
        self.assertFalse(plan.needs_clarify)
        self.assertEqual(plan.steps[0].tool, "affinity_by_author")
        # Aggressive OOV filter
        self.assertGreaterEqual(plan.steps[0].args["min_corpus_count"], 5000)
        self.assertIn("propn_strict", plan.explain)

    def test_default_min_corpus_count_unchanged(self):
        """Regression: without the modifier, min_corpus_count keeps
        its auto-computed value (not 5000)."""
        e = ent_mod.extract("фирменные слова Дойла")
        plan = plan_mod.build("author_vocab", e)
        self.assertEqual(plan.steps[0].tool, "affinity_by_author")
        self.assertLess(plan.steps[0].args["min_corpus_count"], 5000)


class StansEndToEndScenario(unittest.TestCase):
    """The exact two-turn flow from Stan's screenshot."""

    def test_screenshot_followup_resolves(self):
        history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "challenger 122 / barrymore 112 / …"},
        ]
        turn2 = "убери из них имена собственные"
        m = int_mod.classify(turn2)
        inferred = history_mod.infer_followup_intent(turn2, history)
        intent_label = inferred or m.label
        e = ent_mod.extract(turn2)
        e = history_mod.merge_with_history(e, history, turn2)
        plan = plan_mod.build(intent_label, e)
        # Re-runs author_vocab against Doyle with stricter filter
        self.assertEqual(plan.intent, "author_vocab")
        self.assertFalse(plan.needs_clarify)
        self.assertGreaterEqual(plan.steps[0].args["min_corpus_count"], 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
