"""W-11 follow-up (Phase 5 P2, 2026-05-24) — POS-anchored period intents
+ vague-period smart-clarify.

Stan test bench (the «разумные запросы уходят в 54-60s parse-fail» class):
    «топ существительных эпохи»
    «топ существительных XIX века»
    «существительные периода»

Acceptance (from tz_claude_code_fixes_2026-05-22 W-11):
    разумный запрос исполняется или даёт smart-clarify быстро (<~10s),
    без 50s+ парс-фейлов.

After this change:
  · POS-anchored period queries classify to `period_vocab` (was clarify
    → LLM fallback 50-60s).
  · A vague-period query («эпохи»/«периода» без specific anchor) gets a
    fast smart-clarify with concrete recipe (was: silently default to
    victorian 1837-1901 and run 50s heavy ngrams).
  · Specific-period queries («XIX века», explicit year range, era stem)
    still execute via top_ngrams_by_author — no regression on the
    existing happy path (verified by test_w11_period_vocab_intents.py
    sibling suite).

R5 compliance: every regex/builder change ships with the motivating
queries as named test cases; negative cases protect against intent
poaching from author_vocab / book_vocab.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.intent import classify
from scripts.v2.planner.entities import extract
from scripts.v2.planner.plan import build
from scripts.v2.planner.builders.composite import _plan_period_vocab


# ---------------------------------------------------------------------------
# Intent classifier — POS-anchored period queries
# ---------------------------------------------------------------------------


class W11POSPeriodIntent(unittest.TestCase):

    def test_top_nouns_era_classifies_as_period_vocab(self):
        """Stan's W-11 acceptance case — was clarify → 50s LLM fallback."""
        self.assertEqual(classify("топ существительных эпохи").label,
                         "period_vocab")

    def test_top_nouns_19c_classifies_as_period_vocab(self):
        self.assertEqual(classify("топ существительных XIX века").label,
                         "period_vocab")

    def test_top_adjectives_19c_classifies_as_period_vocab(self):
        self.assertEqual(classify("топ прилагательных XIX века").label,
                         "period_vocab")

    def test_top_verbs_19c_classifies_as_period_vocab(self):
        self.assertEqual(classify("топ глаголов XIX века").label,
                         "period_vocab")

    def test_bare_period_word_classifies(self):
        self.assertEqual(classify("существительные эпохи").label,
                         "period_vocab")
        self.assertEqual(classify("существительные периода").label,
                         "period_vocab")

    def test_period_first_ordering_classifies(self):
        """«XIX века существительные» — period mentioned BEFORE the
        POS anchor. Both orderings must route here."""
        self.assertEqual(classify("XIX века существительные").label,
                         "period_vocab")

    def test_top_n_prefix_does_not_break(self):
        """Numeric prefix («топ-100 существительных эпохи») doesn't
        steal the match into top_authors_books / etc."""
        self.assertEqual(classify("топ-100 существительных эпохи").label,
                         "period_vocab")

    def test_century_word_classifies(self):
        self.assertEqual(classify("слова XIX века").label,
                         "period_vocab")
        self.assertEqual(classify("слова XX века").label,
                         "period_vocab")

    def test_english_century_classifies(self):
        self.assertEqual(classify("vocab of the 19th century").label,
                         "period_vocab")


# ---------------------------------------------------------------------------
# Intent classifier — negative cases (must NOT classify as period_vocab)
# ---------------------------------------------------------------------------


class W11POSPeriodNegatives(unittest.TestCase):

    def test_author_vocab_does_not_steal(self):
        """«фирменные слова Дойла» stays in author_vocab — period rules
        require a period word, not just a POS or «слова»."""
        self.assertEqual(classify("фирменные слова Дойла").label,
                         "author_vocab")

    def test_signature_phrasings_stay_author_vocab(self):
        """«характерные прилагательные у Wodehouse» — author_vocab,
        not period_vocab. No period word in the query."""
        self.assertNotEqual(
            classify("характерные прилагательные у Wodehouse").label,
            "period_vocab",
        )

    def test_book_vocab_does_not_steal(self):
        """Quoted-title queries with POS anchor → book_vocab."""
        self.assertNotEqual(
            classify('характерные прилагательные в "Pride and Prejudice"').label,
            "period_vocab",
        )


# ---------------------------------------------------------------------------
# Plan builder — vague period → smart-clarify (fast); specific → execute
# ---------------------------------------------------------------------------


class W11PeriodVocabSmartClarify(unittest.TestCase):

    def test_vague_period_smart_clarifies(self):
        """«топ существительных эпохи» — no year range, no era stem,
        no author. Was: silently defaulted to victorian 1837-1901 and
        ran 50s heavy ngrams. Now: smart-clarify in <1s asking for
        a specific period."""
        e = extract("топ существительных эпохи")
        plan = _plan_period_vocab(e)
        self.assertTrue(plan.needs_clarify, plan.explain)
        # The recipe text mentions the cheaper alternatives so user can
        # reformulate in one step.
        q = plan.clarify_question or ""
        self.assertIn("XIX века", q)
        self.assertIn("викторианской", q)
        # Authoritative so v4 LLM-planner doesn't override the clarify
        # with a generic top_books plan.
        self.assertTrue(plan.authoritative_clarify)

    def test_vague_period_with_pos_filter_smart_clarifies(self):
        """POS filter alone doesn't substitute for a period anchor —
        we still don't know which century the user means."""
        e = extract("существительные периода")
        self.assertIn("NOUN", (e.pos_filter or []))
        plan = _plan_period_vocab(e)
        self.assertTrue(plan.needs_clarify)

    def test_specific_century_executes(self):
        """«XIX века» extracts year_from=1800, year_to=1899 → execute path."""
        e = extract("топ существительных XIX века")
        self.assertEqual(e.year_from, 1800)
        self.assertEqual(e.year_to, 1899)
        plan = _plan_period_vocab(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(plan.steps[0].tool, "top_ngrams_by_author")
        self.assertEqual(plan.steps[0].args["year_from"], 1800)
        self.assertEqual(plan.steps[0].args["year_to"], 1899)
        self.assertIn("NOUN", plan.steps[0].args.get("pos_filter") or [])

    def test_year_range_executes_without_era_stem(self):
        """«слова 1837-1901» — explicit range, even without «викториан»
        keyword, anchors the period unambiguously."""
        e = extract("слова 1837-1901")
        plan = _plan_period_vocab(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(plan.steps[0].args["year_from"], 1837)
        self.assertEqual(plan.steps[0].args["year_to"], 1901)

    def test_era_stem_executes_via_default(self):
        """«слова викторианцев 1837-1901» / «слова викторианской эпохи»
        — era stem present, run the heavy path. Mirrors
        test_w11_period_vocab_intents.py invariant — don't regress."""
        for q in ("слова викторианцев 1837-1901",
                   "слова викторианской эпохи"):
            with self.subTest(query=q):
                e = extract(q)
                plan = _plan_period_vocab(e)
                self.assertFalse(plan.needs_clarify, plan.explain)
                self.assertEqual(plan.steps[0].tool, "top_ngrams_by_author")

    def test_author_specified_executes_even_with_vague_period(self):
        """When author is named («у Doyle») we always have a narrow
        scope — no need to clarify."""
        e = extract("топ существительных эпохи у Doyle")
        plan = _plan_period_vocab(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(plan.steps[0].args["author_regex"], "^Doyle,")


# ---------------------------------------------------------------------------
# End-to-end: classify → extract → build (full plan path)
# ---------------------------------------------------------------------------


class W11PeriodVocabEndToEnd(unittest.TestCase):

    def test_e2e_top_nouns_19c_builds_executable_plan(self):
        """Sanity: the full pipeline turns Stan's query into an
        executable single-step plan with the right tool + filters."""
        q = "топ существительных XIX века"
        e = extract(q)
        intent = classify(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "period_vocab")
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(len(plan.steps), 1)
        s = plan.steps[0]
        self.assertEqual(s.tool, "top_ngrams_by_author")
        self.assertEqual(s.args["pos_filter"], ["NOUN"])
        self.assertEqual(s.args["year_from"], 1800)
        self.assertEqual(s.args["year_to"], 1899)

    def test_e2e_vague_period_builds_clarify_plan(self):
        """The full pipeline must NOT silently default a vague query
        to victorian 1837-1901. Smart-clarify is the expected output."""
        q = "топ существительных эпохи"
        e = extract(q)
        intent = classify(q)
        plan = build(intent.label, e)
        self.assertEqual(intent.label, "period_vocab")
        self.assertTrue(plan.needs_clarify, plan.explain)
        self.assertTrue(plan.authoritative_clarify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
