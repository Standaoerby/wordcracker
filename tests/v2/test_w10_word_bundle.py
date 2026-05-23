"""W-10 tests — word lookup bundles translation + IPA + POS +
definition + corpus snippets + etymology.

Stan 2026-05-23 (Phase 4) test bench:
    «что значит ajar» → есть перевод/POS/определение, нет IPA, нет
    этимологии, нет corpus-сниппетов с titles.
    «этимология engine» → только цепочка family.

W-10 acceptance:
    1. Ответ о слове бандлит перевод + IPA + POS + определение +
       2–3 corpus-сниппета с titles + этимология.
    2. Недостающий фасет деградирует мягко и помечается, остальные
       остаются.
    3. «что значит ajar» и «этимология слова engine» содержат IPA
       и corpus-сниппеты с titles.

R5 compliance: позитивный кейс (intent + word extracted), негативный
(bare «что значит» без слова → clarify, не bundle с None word).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import build
from scripts.v2.planner.builders.word import _plan_word_contexts, _plan_word_etymology


# ---------------------------------------------------------------------------
# Intent — «что значит X» / «meaning of X» / «define X» route to word_contexts
# ---------------------------------------------------------------------------


class MeaningQueriesRouteToWordContexts(unittest.TestCase):

    def test_russian_chto_znachit(self):
        self.assertEqual(classify("что значит ajar").label, "word_contexts")

    def test_russian_chto_takoe(self):
        self.assertEqual(classify("что такое ajar").label, "word_contexts")

    def test_russian_smysl_slova(self):
        self.assertEqual(classify("смысл слова ajar").label, "word_contexts")

    def test_russian_znachenie_slova(self):
        self.assertEqual(classify("значение слова engine").label, "word_contexts")

    def test_russian_obyasni_slovo(self):
        self.assertEqual(classify("объясни слово ajar").label, "word_contexts")

    def test_english_meaning_of(self):
        self.assertEqual(classify("meaning of ajar").label, "word_contexts")

    def test_english_meaning_of_the_word(self):
        self.assertEqual(classify("meaning of the word ajar").label, "word_contexts")

    def test_english_define(self):
        self.assertEqual(classify("define ajar").label, "word_contexts")

    def test_english_what_is_the_word(self):
        self.assertEqual(classify("what is the word ajar").label, "word_contexts")

    def test_meaning_query_does_NOT_misroute_book_compare(self):
        # Negative — book-compare phrasing must not accidentally bucket
        # as word_contexts even though «what is» appears.
        intent = classify("what is harder, Dracula or Frankenstein")
        self.assertNotEqual(intent.label, "word_contexts",
                            msg="book-compare phrasing leaked into word_contexts")


# ---------------------------------------------------------------------------
# Entity extraction — word lifted out of «что значит X» phrases
# ---------------------------------------------------------------------------


class MeaningQueryWordExtraction(unittest.TestCase):

    def test_chto_znachit_lifts_word(self):
        self.assertEqual(extract("что значит ajar").word, "ajar")

    def test_chto_takoe_lifts_word(self):
        self.assertEqual(extract("что такое ajar").word, "ajar")

    def test_smysl_slova_lifts_word(self):
        self.assertEqual(extract("смысл слова engine").word, "engine")

    def test_znachenie_slova_lifts_word(self):
        self.assertEqual(extract("значение слова engine").word, "engine")

    def test_obyasni_slovo_lifts_word(self):
        self.assertEqual(extract("объясни слово ajar").word, "ajar")

    def test_meaning_of_lifts_word(self):
        self.assertEqual(extract("meaning of ajar").word, "ajar")

    def test_define_lifts_word(self):
        self.assertEqual(extract("define ajar").word, "ajar")

    def test_quoted_word_still_lifts(self):
        self.assertEqual(extract('что значит "ajar"').word, "ajar")


# ---------------------------------------------------------------------------
# Plan bundles enrich_word for the «что значит X» path
# ---------------------------------------------------------------------------


class WordContextsBundlesEnrichAndCorpus(unittest.TestCase):
    """No-author word_contexts must call BOTH hybrid_search (corpus
    snippets with titles) AND enrich_word (translation + IPA + POS +
    definition + family_chain). Otherwise the bundle is missing facets."""

    def test_plan_has_hybrid_search_step(self):
        e = extract("что значит ajar")
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("hybrid_search", tools)

    def test_plan_has_enrich_word_step(self):
        e = extract("что значит ajar")
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("enrich_word", tools,
                      msg="W-10 bundle requires enrich_word for IPA/POS/def")

    def test_enrich_word_passes_target_lang_ru(self):
        # Translation_ru must come through — default target_lang is ru
        e = extract("что значит ajar")
        plan = _plan_word_contexts(e)
        enrich_step = next(s for s in plan.steps if s.tool == "enrich_word")
        self.assertEqual(enrich_step.args.get("target_lang"), "ru")

    def test_enrich_word_is_optional(self):
        # Soft-degrade — if Wiktionary is offline the bundle still answers
        e = extract("что значит ajar")
        plan = _plan_word_contexts(e)
        enrich_step = next(s for s in plan.steps if s.tool == "enrich_word")
        self.assertTrue(enrich_step.optional)

    def test_plan_has_composite_render_note(self):
        # The renderer needs explicit instruction to surface ALL facets.
        e = extract("что значит ajar")
        plan = _plan_word_contexts(e)
        self.assertTrue(plan.render_notes,
                        msg="W-10 plan must stamp composite render note")
        joined = " ".join(plan.render_notes).lower()
        # Must mention the facet list so renderer knows to show them
        for facet in ("translation_ru", "ipa", "pos", "family_chain",
                       "сниппет"):
            self.assertIn(facet, joined,
                          msg=f"render_note must mention facet {facet!r}: {joined!r}")


# ---------------------------------------------------------------------------
# Plan bundles enrich_word for the «этимология engine» path
# ---------------------------------------------------------------------------


class WordEtymologyBundlesEnrich(unittest.TestCase):
    """Etymology query with a single word (no author) should ALSO get
    translation + IPA + POS + definition via enrich_word. W-10 critical
    case: «этимология engine» was returning only family_chain — no IPA,
    no translation."""

    def test_plan_has_word_etymology_step(self):
        e = extract("этимология слова engine")
        plan = _plan_word_etymology(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("word_etymology", tools)

    def test_plan_has_enrich_word_step(self):
        # W-10 root fix: bundle enrich_word so IPA/POS/translation lands
        # in the answer.
        e = extract("этимология слова engine")
        plan = _plan_word_etymology(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("enrich_word", tools,
                      msg="W-10 etymology bundle requires enrich_word")

    def test_plan_has_hybrid_search_step(self):
        # Corpus snippets with titles round out the bundle
        e = extract("этимология слова engine")
        plan = _plan_word_etymology(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("hybrid_search", tools)

    def test_enrich_and_hybrid_are_optional(self):
        # word_etymology is the headline; the lexical facets are bonus
        e = extract("этимология слова engine")
        plan = _plan_word_etymology(e)
        for tool in ("enrich_word", "hybrid_search"):
            step = next(s for s in plan.steps if s.tool == tool)
            self.assertTrue(step.optional,
                            msg=f"{tool} must be optional in etymology bundle")


# ---------------------------------------------------------------------------
# End-to-end via build() — full bundle present for Stan test bench cases
# ---------------------------------------------------------------------------


class EndToEndStanTestBenchCases(unittest.TestCase):

    def test_chto_znachit_ajar_full_bundle(self):
        e = extract("что значит ajar")
        plan = build(classify("что значит ajar").label, e)
        self.assertEqual(plan.intent, "word_contexts")
        tools = {s.tool for s in plan.steps}
        # Stan's acceptance — these two tools must be in the plan
        self.assertIn("enrich_word", tools)
        self.assertIn("hybrid_search", tools)

    def test_etymology_engine_full_bundle(self):
        e = extract("этимология слова engine")
        plan = build(classify("этимология слова engine").label, e)
        self.assertEqual(plan.intent, "word_etymology")
        tools = {s.tool for s in plan.steps}
        # Stan's acceptance — etymology + IPA + corpus snippets in one plan
        self.assertIn("word_etymology", tools)
        self.assertIn("enrich_word", tools,
                      msg="etymology query must bundle enrich_word for IPA")
        self.assertIn("hybrid_search", tools,
                      msg="etymology query must bundle hybrid_search for corpus titles")


if __name__ == "__main__":
    unittest.main(verbosity=2)
