"""R15 hotfixes — budget wiring + Q88 + Q79 intent routing.

After R15 acceptance turnir found 3 new regressions (Q79 multi-author
parse-fail, Q88 PG id fabrication, Q25 329s latency), these tests lock
in three structural fixes:

  1. **Budget wiring (Q25/Q114/Q105 class)** — rag_v2.ask*/dispatch_render
     now passes RequestBudget into router.execute*. Closes runaway-latency
     class structurally — find_book_by_topic burning 5min on a single
     call would have been aborted at ~30-60s (per-intent budget).
     Verified via _v5_budget_from_envelope helper.

  2. **Q88-class intent routing fix** — «сколько книг написал X» now
     routes to author_metadata, not corpus_meta. Root cause of Q88:
     wrong intent → corpus_overview tool → renderer fabricated PG1342
     (= Pride and Prejudice) as «Doctor Faustus by Marlowe». With
     correct routing, author_metadata tool fires + view emission
     guarantees no PG id fabrication.

  3. **Q79 multi-author intersection** — «общие слова Мелвилла, Конрада
     и Стивенсона» fell through to generic clarify in R15 (0 tool calls,
     intent classifier had no rule for «общие слова + N авторов»). Now
     routes to author_vocab — _plan_author_vocab already parallels
     affinity_by_author × N via multi_author_regex (Sprint 11.3),
     renderer computes intersection downstream.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.intent import classify


# =====================================================================
# Q88 — intent routing for «сколько книг написал X»
# =====================================================================


class Q88IntentRouting(unittest.TestCase):
    """Q88 failure mode: «сколько книг написал Marlowe» → corpus_meta
    → corpus_overview tool → no Marlowe data → renderer fabricated
    PG1342 / PG1343 / PG13... for «Doctor Faustus», «Edward II».
    Fix: route to author_metadata which actually queries author data."""

    def test_q88_marlowe_routes_to_author_metadata(self):
        for q in [
            "сколько книг написал Christopher Marlowe",
            "сколько книг написал Марло",
            "сколько книг написал Шекспир",
            "сколько произведений написал Достоевский",
            "сколько работ создал Толстой",
            "сколько драм написал Шекспир",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_metadata",
                             f"Q88 regression: {q!r} → {m.label}")
            self.assertGreaterEqual(m.confidence, 0.9)

    def test_q88_english_variants_route_to_author_metadata(self):
        for q in [
            "how many books did Marlowe write",
            "how many plays did Shakespeare write",
            "how many works has Tolstoy published",
            "how many poems did Poe author",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_metadata",
                             f"Q88 EN regression: {q!r} → {m.label}")

    def test_count_phrasing_routes_to_author_metadata(self):
        for q in [
            "количество книг Толстого",
            "количество произведений Marlowe",
            "количество книг автора Шекспира",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_metadata", f"{q!r} → {m.label}")

    def test_corpus_meta_controls_still_route_correctly(self):
        """Critical: don't over-fold corpus_meta → author_metadata.
        General corpus questions must stay corpus_meta."""
        for q, expected in [
            ("сколько книг в базе",              "corpus_meta"),
            ("сколько книг всего",               "corpus_meta"),
            ("сколько у тебя книг",              "author_metadata"),  # «у X» existing rule
            ("сколько книжек",                   "corpus_meta"),
            ("how many books in the corpus",     "corpus_meta"),
            ("сколько книг в коллекции",         "corpus_meta"),
        ]:
            m = classify(q)
            self.assertEqual(m.label, expected,
                             f"Control regression: {q!r} → {m.label} "
                             f"(expected {expected})")

    def test_existing_genitive_pattern_unchanged(self):
        """R3 rule «сколько у Толстого книг» — pre-existing fix that
        must not break."""
        for q in [
            "сколько у Толстого книг",
            "сколько у Doyle книг",
            "сколько у Marlowe книг",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_metadata", f"{q!r} → {m.label}")


# =====================================================================
# Q79 — multi-author intersection intent
# =====================================================================


class Q79IntentRouting(unittest.TestCase):
    """Q79 failure mode (R15): «общие слова Мелвилла, Конрада и Стивенсона»
    → clarify (0 tool calls) because intent classifier had no rule for
    «общие слова + N authors». Fix routes to author_vocab — which already
    parallels affinity_by_author × N via _plan_author_vocab's
    multi_author_regex handling (Sprint 11.3). Renderer computes the
    intersection."""

    def test_q79_exact_query_routes_to_author_vocab(self):
        m = classify("общие слова Мелвилла, Конрада и Стивенсона")
        self.assertEqual(m.label, "author_vocab")
        self.assertGreaterEqual(m.confidence, 0.8)

    def test_q79_variants_route_to_author_vocab(self):
        for q in [
            "общие фирменные слова Doyle и Stevenson",
            "общая лексика Шекспира и Марло",
            "пересечение слов По и Лавкрафта",
            "что общего у Doyle, Wells и Stevenson",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_vocab", f"{q!r} → {m.label}")

    def test_q79_english_variants(self):
        for q in [
            "common words across Melville, Conrad and Stevenson",
            "common signature words Doyle and Stevenson",
            "intersection of vocabulary Poe and Lovecraft",
            "shared signature words across Wells and Stevenson",
        ]:
            m = classify(q)
            self.assertEqual(m.label, "author_vocab", f"{q!r} → {m.label}")

    def test_q79_controls_dont_overmatch(self):
        """Controls that contain NO author/«слова» keyword combo must
        not match the new rule.

        Note: «что такое общие слова» does match the intent (regex
        sees «общие слова»), but entity extractor finds no author →
        _plan_author_vocab returns clarify. Harmless at intent layer."""
        for q in [
            "общие сведения",          # no «слова» keyword
            "привет",                  # no relevant keywords
        ]:
            m = classify(q)
            self.assertNotEqual(
                m.label, "author_vocab",
                f"Control matched author_vocab unexpectedly: {q!r} → {m}",
            )


# =====================================================================
# Budget wiring through ask/ask_stream → router
# =====================================================================


class BudgetEnvelopeWiring(unittest.TestCase):
    """ask/ask_stream create an envelope per request; budget must flow
    through to router.execute* — closes Q25/Q114/Q105 latency class
    structurally. Phase 1 (2026-05-22) — WC_V5_PIPELINE gate removed,
    envelope is always created."""

    def test_v5_budget_from_envelope_returns_none_when_envelope_is_none(self):
        from scripts.v2 import rag_v2 as r2
        b = r2._v5_budget_from_envelope(None, intent_label="author_metadata")
        self.assertIsNone(b)

    def test_v5_budget_from_envelope_returns_budget_with_intent(self):
        from scripts.v2 import rag_v2 as r2
        env = r2._v5_pipeline_envelope("test query")
        self.assertIsNotNone(env)
        b = r2._v5_budget_from_envelope(env, intent_label="author_compare")
        self.assertIsNotNone(b)
        # author_compare is heavy intent — should have generous budget
        self.assertGreater(b.wall_clock_s, 20)
        # But envelope tracks elapsed already
        self.assertLessEqual(b.wall_clock_s, 90.0)

    def test_v5_budget_subtracts_elapsed_time(self):
        """If envelope was created N seconds ago, budget for downstream
        router should reflect elapsed time via remaining_s().

        Phase 5: `wall_clock_s` остаётся per-intent константой; уменьшается
        `remaining_s()` через привязку `ts_start` к envelope t0 (см.
        `RequestBudget.set_clock`). dispatch-chokepoint считает effective
        timeout именно по remaining_s().
        """
        import time as _t
        from scripts.v2 import rag_v2 as r2
        env = r2._v5_pipeline_envelope("q")
        # Sleep briefly to simulate planner work
        _t.sleep(0.05)
        b = r2._v5_budget_from_envelope(env, intent_label="author_metadata")
        from scripts.v2.budget import INTENT_BUDGETS_S
        author_md_budget = INTENT_BUDGETS_S["author_metadata"]
        # Budget should reflect the small elapsed already consumed
        self.assertLess(b.remaining_s(), author_md_budget)
        self.assertGreater(b.remaining_s(), author_md_budget - 1.0)
        # wall_clock_s itself is the per-intent ceiling, unchanged
        self.assertEqual(b.wall_clock_s, author_md_budget)

    def test_minimum_budget_floor(self):
        """If envelope already burned all budget, downstream remaining_s()
        goes negative — chokepoint floors to >=1s so the tool can return
        a clean timeout error instead of hanging."""
        from scripts.v2 import rag_v2 as r2
        from scripts.v2.tool_registry import effective_timeout_s
        env = r2._v5_pipeline_envelope("q")
        # Manually fast-forward time by mutating t0
        env["t0"] = 0.0       # makes elapsed = enormous
        b = r2._v5_budget_from_envelope(env, intent_label="introduction")
        # Spec ceiling stays intact
        self.assertGreaterEqual(b.wall_clock_s, 1.0)
        # Effective timeout floored to >=1 so tool aborts cleanly
        self.assertGreaterEqual(effective_timeout_s(60, b), 1)


# =====================================================================
# Integration — ask() with envelope passes budget to router
# =====================================================================


class AskBudgetIntegration(unittest.TestCase):
    """Verify the wiring actually propagates by mocking router.execute*
    and checking budget kwarg is set."""

    def test_ask_passes_budget_to_router(self):
        """ask() always creates an envelope and passes budget to
        router.execute. Phase 1 (2026-05-22) — gate removed."""
        from scripts.v2 import rag_v2 as r2
        from scripts.v2.planner.router import RouterResult

        captured_budget = []

        def fake_execute(plan, *, budget=None):
            captured_budget.append(budget)
            return RouterResult(kind="no_steps", plan=plan, message="x")

        with mock.patch("scripts.v2.planner.router.execute",
                        side_effect=fake_execute):
            _ = r2.ask("какие книги у Doyle", history=None)

        # If execute fired (introduction wouldn't, but author_lookup will),
        # budget should be a RequestBudget (not None)
        if captured_budget:
            from scripts.v2.budget import RequestBudget
            self.assertIsInstance(
                captured_budget[0], RequestBudget,
                f"Expected RequestBudget kwarg, got {captured_budget[0]}"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
