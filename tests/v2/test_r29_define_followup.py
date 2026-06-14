"""R-29 WP5 / B107 (repro for B103) — plural-define follow-up must
resolve to the FULL prior word set, not the head.

Bug: after a turn that listed N words, a plural follow-up asking for
their meanings («дай их значения» / «что значат эти слова» / «их
значения») returned ONE word instead of all N. Root cause:
`history.merge_with_history` backfilled `entity.word = words[0]` (the
HEAD of the prior list) and never carried the full set, so the plan
defined a single word.

Fix: a plural-define follow-up reuses the EXISTING set machinery — the
same `_prior_words` hand-off + `enrich_word`-per-word plan that the
translate follow-up uses (`translate_word_list`). enrich_word returns
translation_ru + definition_en + IPA + POS, i.e. the «значение» of each
word. No new intent / plan / wrapper — just a recognizer that routes the
plural-define phrasing into the set path before the head-only backfill.

R2: every GREEN assertion below FAILS on pre-fix code (inferred=None,
_prior_words=None, plan=clarify / single head word) and passes after the
`_is_define_followup` wiring lands.
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


# A realistic prior turn: an author-vocab question whose assistant answer
# rendered a 5-word comma list (the form `_last_word_list_from_assistant`
# already parses for the translate follow-up).
FIVE_WORD_HISTORY = [
    {"role": "user", "content": "топ 5 фирменных слов Дойла"},
    {"role": "assistant",
     "content": "blighter, dashed, ripping, hullo, jolly"},
]
FIVE_WORDS = ["blighter", "dashed", "ripping", "hullo", "jolly"]


class DefineFollowupRecognizer(unittest.TestCase):
    """The recognizer fires on plural-define phrasings and stays OFF for
    single-word meaning queries (no over-trigger)."""

    def test_plural_define_phrasings_match(self):
        for text in [
            "дай их значения",
            "их значения",
            "что значат эти слова",
            "значения этих слов",
            "каковы их значения",
            "what do these words mean",
            "their meanings please",
            "define them",
        ]:
            self.assertTrue(
                history_mod._is_define_followup(text), text)

    def test_single_word_meaning_does_not_match(self):
        # «что значит X» / «значение слова X» are single-word word_contexts
        # queries — they must NOT be captured by the plural-define path,
        # or we'd reroute them through the list machinery.
        for text in [
            "что значит ajar",
            "значение слова factory",
            "что это значит",
            "приведи примеры использования",
            "переведи Толстого с русского",
            "что такое affinity",
        ]:
            self.assertFalse(
                history_mod._is_define_followup(text), text)

    def test_define_followup_registers_as_followup(self):
        # Pre-fix «дай их значения» / «их значения» were not even seen as
        # follow-ups (no эти/их trigger), so merge_with_history bailed.
        for text in ["дай их значения", "их значения",
                     "что значат эти слова"]:
            self.assertTrue(
                history_mod._looks_like_followup(text), text)


class DefineFollowupFullSet(unittest.TestCase):
    """Case 1 — prior turn = list of 5 words → «их значения» resolves to
    all 5 (enrich/define over the full set), not the head."""

    def test_infers_translate_word_list_intent(self):
        for q in ["дай их значения", "их значения", "что значат эти слова"]:
            inferred = history_mod.infer_followup_intent(q, FIVE_WORD_HISTORY)
            self.assertEqual(inferred, "translate_word_list", q)

    def test_merge_carries_full_prior_set(self):
        for q in ["дай их значения", "их значения", "что значат эти слова"]:
            e = ent_mod.extract(q)
            merged = history_mod.merge_with_history(e, FIVE_WORD_HISTORY, q)
            prior = (merged.raw_misc or {}).get("_prior_words")
            self.assertEqual(prior, FIVE_WORDS, q)

    def test_plan_enriches_all_five(self):
        for q in ["дай их значения", "их значения", "что значат эти слова"]:
            e = ent_mod.extract(q)
            inferred = history_mod.infer_followup_intent(q, FIVE_WORD_HISTORY)
            e = history_mod.merge_with_history(e, FIVE_WORD_HISTORY, q)
            plan = plan_mod.build(inferred or int_mod.classify(q).label, e)
            self.assertEqual(plan.intent, "translate_word_list", q)
            self.assertFalse(plan.needs_clarify, q)
            self.assertEqual(len(plan.steps), 5, q)
            self.assertTrue(all(s.tool == "enrich_word" for s in plan.steps), q)
            words_in_plan = [s.args.get("word") for s in plan.steps]
            self.assertEqual(words_in_plan, FIVE_WORDS, q)


class DefineFollowupSingleWord(unittest.TestCase):
    """Case 2 — one word in the prior turn → still ok (defines that one,
    via the same path; no clarify, no crash)."""

    def setUp(self):
        self.history = [
            {"role": "user", "content": "топ слов Дойла"},
            {"role": "assistant",
             "content": "Самое характерное слово — **blighter**."},
        ]

    def test_single_prior_word_still_defines(self):
        q = "дай их значения"
        e = ent_mod.extract(q)
        inferred = history_mod.infer_followup_intent(q, self.history)
        e = history_mod.merge_with_history(e, self.history, q)
        plan = plan_mod.build(inferred or int_mod.classify(q).label, e)
        self.assertEqual(plan.intent, "translate_word_list")
        self.assertFalse(plan.needs_clarify)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].args.get("word"), "blighter")

    def test_classic_single_word_followup_unaffected(self):
        # Regression guard: a genuine single-word follow-up («приведи
        # примеры использования» → word_contexts) must NOT be rerouted
        # into the list/define path.
        inferred = history_mod.infer_followup_intent(
            "приведи примеры использования", FIVE_WORD_HISTORY)
        self.assertNotEqual(inferred, "translate_word_list")


class DefineFollowupNoList(unittest.TestCase):
    """Case 3 — no parseable word list in history → honest clarify, no
    fabrication (do NOT invent words)."""

    def test_no_prior_words_surfaces_clarify(self):
        history = [
            {"role": "user", "content": "топ слов Дойла"},
            {"role": "assistant", "content": "Вот что я нашёл по Дойлю."},
        ]
        q = "дай их значения"
        e = ent_mod.extract(q)
        inferred = history_mod.infer_followup_intent(q, history)
        e = history_mod.merge_with_history(e, history, q)
        plan = plan_mod.build(inferred or int_mod.classify(q).label, e)
        self.assertTrue(plan.needs_clarify)
        self.assertEqual(plan.steps, [])
        # Honest: clarify text must not contain invented vocabulary.
        self.assertNotIn("blighter", (plan.clarify_question or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
