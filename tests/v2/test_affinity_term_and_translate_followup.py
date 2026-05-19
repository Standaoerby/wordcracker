"""Sprint 20 — Stan 2026-05-19 16:43-16:47 prod logs (admin/failed):

  16:43:26  «Покажи топ 100 аффинных слов Агаты Кристи по всем произведениям»
  16:47:17  «Сделай перевод на русский всех слов. В переводе дай то значение,
             которое используется у Агаты кристи»
  16:47:54  «Возьми слова, которые ты мне выдал и переведи на русский»

Three independent rules-path gaps:

(1) «аффинных слов» — power-user technical phrasing using «affinity»
    name of the metric. Not in author_vocab rule patterns. Patched.

(2) Translation follow-up after a word-list turn. We have enrich_word
    with target_lang='ru', and learning_words combines affinity-band +
    per-word enrich. The right re-run for «переведи на русский» after
    affinity_by_author is `learning_words(scope=author)` — same words,
    plus RU translations + level + definitions. Patched via a
    follow-up recognizer that switches author_vocab/book_vocab to
    `learning` intent.

(3) «возьми слова которые ты мне выдал» — prior-output reference
    combined with translate verb fires the same followup, even
    without explicit «на русский» target.

Long-term: v4 LLM planner sees the conversation and can compose this
without per-modifier code. Rules-path bridge until flag is on.
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


class AffinityTerminology(unittest.TestCase):

    def test_top_100_affinnyh(self):
        q = "Покажи топ 100 аффинных слов Агаты Кристи по всем произведениям"
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        self.assertEqual(m.label, "author_vocab")
        self.assertEqual(e.author_regex, "^Christie,")
        self.assertEqual(e.top_n, 100)
        self.assertFalse(p.needs_clarify)

    def test_affinity_words_english(self):
        m = int_mod.classify("show high-affinity words for Wodehouse")
        self.assertEqual(m.label, "author_vocab")

    def test_affinnaya_lexika(self):
        m = int_mod.classify("аффинная лексика Wodehouse")
        self.assertEqual(m.label, "author_vocab")


class TranslateFollowupPattern(unittest.TestCase):

    def test_pattern_explicit_target(self):
        from scripts.v2.planner.history import _is_translate_followup
        for text in [
            "переведи на русский",
            "сделай перевод на русский",
            "переведи слова на русский",
            "translate to Russian",
            "translate them to ru",
        ]:
            self.assertTrue(_is_translate_followup(text), text)

    def test_pattern_prior_output_ref(self):
        from scripts.v2.planner.history import _is_translate_followup
        # «возьми слова которые ты мне выдал и переведи» — no «на русский»
        # but the prior-output ref makes it clear
        text = "возьми слова которые ты мне выдал и переведи"
        self.assertTrue(_is_translate_followup(text))

    def test_pattern_unrelated_does_not_match(self):
        from scripts.v2.planner.history import _is_translate_followup
        for text in [
            "переведи Толстого с русского",  # «с русского» != target
            "приведи примеры",
            "уточни запрос",
            "что такое affinity",
        ]:
            self.assertFalse(_is_translate_followup(text), text)


class TranslateFollowupRouting(unittest.TestCase):
    """Sprint 20 retrospective — when user asks to translate after a
    word-list turn, we DO NOT route to learning_words (different list,
    misleading). Instead surface honest clarify via the new
    `translate_word_list` intent."""

    def setUp(self):
        self.history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "blighter, dashed, ripping, …"},
        ]

    def test_inherits_translate_word_list_intent(self):
        inferred = history_mod.infer_followup_intent(
            "переведи их на русский", self.history,
        )
        self.assertEqual(inferred, "translate_word_list")

    def test_takes_prior_author(self):
        e = ent_mod.extract("переведи их на русский")
        merged = history_mod.merge_with_history(
            e, self.history, "переведи их на русский",
        )
        self.assertEqual(merged.author_regex, "^Doyle,")
        self.assertEqual((merged.raw_misc or {}).get("_translate_to"), "ru")

    def test_take_prior_output_phrasing(self):
        inferred = history_mod.infer_followup_intent(
            "Возьми слова, которые ты мне выдал и переведи на русский",
            self.history,
        )
        self.assertEqual(inferred, "translate_word_list")

    def test_explicit_author_in_followup_wins(self):
        """User translates with explicit different author («у Кристи»);
        backfill respects the explicit one."""
        q = ("Сделай перевод на русский всех слов. В переводе дай то "
             "значение, которое используется у Агаты кристи")
        e = ent_mod.extract(q)
        merged = history_mod.merge_with_history(e, self.history, q)
        # The query mentions Christie explicitly, so backfill keeps it
        self.assertEqual(merged.author_regex, "^Christie,")
        self.assertEqual((merged.raw_misc or {}).get("_translate_to"), "ru")

    def test_full_plan_chains_enrich_when_words_extractable(self):
        """Sprint 20+: prior assistant content has a comma-list — words
        ARE extractable, plan chains enrich_word per word, no clarify."""
        q = "Возьми слова, которые ты мне выдал и переведи на русский"
        m = int_mod.classify(q)
        inferred = history_mod.infer_followup_intent(q, self.history)
        intent_label = inferred or m.label
        e = ent_mod.extract(q)
        e = history_mod.merge_with_history(e, self.history, q)
        plan = plan_mod.build(intent_label, e)
        self.assertEqual(plan.intent, "translate_word_list")
        # Words extracted from prior assistant comma list
        self.assertFalse(plan.needs_clarify)
        # Each step is enrich_word with target_lang=ru
        self.assertGreater(len(plan.steps), 0)
        self.assertEqual(plan.steps[0].tool, "enrich_word")
        self.assertEqual(plan.steps[0].args["target_lang"], "ru")
        self.assertTrue(plan.steps[0].optional)  # Wiktionary miss not fatal

    def test_clarify_when_prior_extraction_fails(self):
        """If prior assistant message has nothing parseable, surface
        honest clarify with actionable advice."""
        empty_history = [
            {"role": "user", "content": "фирменные слова Дойла"},
            {"role": "assistant", "content": "Вот что я нашёл по Дойлю."},
        ]
        q = "переведи эти слова на русский"
        m = int_mod.classify(q)
        inferred = history_mod.infer_followup_intent(q, empty_history)
        e = ent_mod.extract(q)
        e = history_mod.merge_with_history(e, empty_history, q)
        plan = plan_mod.build(inferred or m.label, e)
        self.assertTrue(plan.needs_clarify)
        self.assertIn("WC_LLM_PLANNER", plan.clarify_question or "")


class TranslateFollowupNonWordlistPrior(unittest.TestCase):
    """If prior turn wasn't a word-list intent (e.g. readability),
    don't force a learning re-run — just keep prior intent + stamp
    the translate flag so renderer knows to add translations."""

    def test_prior_readability_keeps_intent(self):
        history = [
            {"role": "user", "content": "уровень сложности Pride and Prejudice"},
            {"role": "assistant", "content": "Flesch 58.8, CEFR B2"},
        ]
        inferred = history_mod.infer_followup_intent(
            "переведи это на русский", history,
        )
        # Not a word-list prior → stay on book_readability
        self.assertEqual(inferred, "book_readability")


class StanThreeScenariosE2E(unittest.TestCase):

    def test_query_1_аффинных_resolves(self):
        q = "Покажи топ 100 аффинных слов Агаты Кристи по всем произведениям"
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        self.assertEqual(p.intent, "author_vocab")
        self.assertFalse(p.needs_clarify)
        self.assertEqual(p.steps[0].args["top"], 100)
        self.assertEqual(p.steps[0].args["author_regex"], "^Christie,")

    def test_query_2_translation_with_explicit_author(self):
        """Sprint 20+: with extractable prior words AND explicit author,
        plan chains enrich_word over the prior list."""
        history = [
            {"role": "user", "content": "фирменные слова Wodehouse"},
            {"role": "assistant", "content": "blighter, dashed, ripping, hullo, jolly"},
        ]
        q = ("Сделай перевод на русский всех слов. В переводе дай то "
             "значение, которое используется у Агаты кристи")
        m = int_mod.classify(q)
        inferred = history_mod.infer_followup_intent(q, history)
        intent_label = inferred or m.label
        e = ent_mod.extract(q)
        e = history_mod.merge_with_history(e, history, q)
        plan = plan_mod.build(intent_label, e)
        self.assertEqual(plan.intent, "translate_word_list")
        self.assertFalse(plan.needs_clarify)
        # The entity still has the explicit Christie reference for any
        # future renderer to use
        self.assertEqual(e.author_regex, "^Christie,")
        # Each step is enrich_word with target_lang=ru
        self.assertGreater(len(plan.steps), 0)
        self.assertEqual(plan.steps[0].tool, "enrich_word")

    def test_query_3_take_words_and_translate(self):
        history = [
            {"role": "user", "content": "фирменные слова Wodehouse"},
            {"role": "assistant", "content": "blighter, dashed, ripping, hullo, jolly"},
        ]
        q = "Возьми слова, которые ты мне выдал и переведи на русский"
        m = int_mod.classify(q)
        inferred = history_mod.infer_followup_intent(q, history)
        intent_label = inferred or m.label
        e = ent_mod.extract(q)
        e = history_mod.merge_with_history(e, history, q)
        plan = plan_mod.build(intent_label, e)
        self.assertEqual(plan.intent, "translate_word_list")
        self.assertFalse(plan.needs_clarify)
        # Wodehouse backfilled from prior, available in entity
        self.assertEqual(e.author_regex, "^Wodehouse,")
        # enrich_word chain on prior words
        self.assertGreater(len(plan.steps), 0)
        self.assertEqual(plan.steps[0].tool, "enrich_word")


if __name__ == "__main__":
    unittest.main(verbosity=2)
