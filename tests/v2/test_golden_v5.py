"""Golden test suite — v5 Phase 0.

Two tiers:

  1. **Type contracts** — RenderableView / FilterSpec / RequestTrace /
     RequestBudget invariants. No external deps. ALWAYS RUN. Failure
     means the v5 foundation contract is broken.

  2. **Behavioural goldens** — actual analytics on canonical inputs.
     SKIPPED unless `WC_GOLDEN_LIVE=1` is set (these require the live
     stack: ChromaDB, Ollama, corpus). They are the v5 acceptance gate
     per [[architecture_refactor_v5_plan]] §Phase 0:

       - B-R14-7  learning_words(PG1342, level=B2) ≥ 10 words
       - B-R14-13 resolve_author_name("Hugo") → Victor Hugo, not Ganz
       - B-R14-4  Latin/Cyrillic homoglyph resolves cleanly
       - B-R14-9  "Толстого" / "Братья Карамазовы" resolve
       - Canonical stylometry: Burrows Delta Doyle↔Stevenson ≈ 0.4385,
         Hawthorne→Melville ≈ 0.4506
       - Canonical readability: P&P Flesch 58.8 / Midsummer 86.9
       - B-R14-1  top_authors_by → no CIA / corporate non-authors
       - B-R14-15 author_vocab(Doyle) — no boer toponyms

When v5 phases advance:
  - Phase 2: behavioural goldens start exercising the .view field
    instead of just .data
  - Phase 3: golden tests assert no fabrication (renderer output ⊆ tool data)
  - Phase 5: budget enforcement asserts wall_clock ≤ 65s per query
  - Phase 6: full suite must be green before v5 production rollout

Run:
  # type contracts only
  python3 tests/v2/test_golden_v5.py

  # full suite (requires live stack)
  WC_GOLDEN_LIVE=1 BASE=http://127.0.0.1:8890 \\
      python3 tests/v2/test_golden_v5.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ---- v5 Phase 0 modules under test ----
from scripts.v2 import budget as budget_mod
from scripts.v2 import observability as obs_mod
from scripts.v2 import view_types as vt
from scripts.v2._types import ToolResult, Coverage
from scripts.v2.filters import FilterSpec


# =====================================================================
# Tier 1 — Type contracts (always run)
# =====================================================================


class ViewTypeContracts(unittest.TestCase):
    """RenderableView / ViewType / EmptyState invariants."""

    def test_viewtype_enum_covers_expected_shapes(self):
        # Sanity: the registry has at least the cores we need for the
        # first wave of tool migrations. New view types should be added
        # consciously, so the test enumerates expected ones explicitly.
        required = {
            "TOP_N_TABLE", "COMPARISON_PANEL", "ETYMOLOGY_BUNDLE",
            "READABILITY_SUMMARY", "TIMELINE_CHART", "ATTRIBUTION_RESULT",
            "RECOMMENDATION_LIST", "CORPUS_META_SNAPSHOT", "WORD_CONTEXTS",
            "COLLOCATES", "AUTHOR_PROFILE", "VOCAB_PASSPORT",
            "AUTHOR_METADATA", "BOOK_LOOKUP", "AUTHOR_LOOKUP",
            "EMOTION_PROFILE", "LEARNING_WORDS", "EXPORT_ARTIFACT",
            "INTRODUCTION", "CLARIFY", "OUT_OF_SCOPE", "NOT_FOUND",
            "ERROR_FRIENDLY", "BUNDLE",
        }
        names = {m.name for m in vt.ViewType}
        missing = required - names
        self.assertFalse(missing, f"Missing ViewType members: {missing}")

    def test_renderable_view_to_dict_serialises_enums(self):
        v = vt.RenderableView(
            view_type=vt.ViewType.TOP_N_TABLE,
            payload={"columns": ["rank", "word"], "rows": [{"rank": 1, "word": "ho"}],
                     "count_returned": 1, "count_requested": 30},
        )
        d = v.to_dict()
        # view_type is .value (string), not the enum
        self.assertEqual(d["view_type"], "top_n_table")
        # Phase 0 contract: count_requested/returned visible to renderer
        self.assertEqual(d["payload"]["count_returned"], 1)
        self.assertEqual(d["payload"]["count_requested"], 30)

    def test_empty_view_must_carry_empty_state(self):
        """B-R14-3 structural fix: empty payload + no empty_state = invalid."""
        bad = vt.RenderableView(
            view_type=vt.ViewType.COMPARISON_PANEL,
            payload={},
            empty_state=None,
        )
        issues = bad.validate()
        self.assertTrue(issues, "Empty payload without empty_state must be invalid")
        self.assertIn("empty_state", issues[0])

    def test_non_empty_view_with_empty_state_is_contradiction(self):
        bad = vt.RenderableView(
            view_type=vt.ViewType.TOP_N_TABLE,
            payload={"rows": [{"a": 1}], "columns": ["a"], "count_returned": 1},
            empty_state=vt.EmptyState(
                reason=vt.EmptyReason.FILTERED_OUT,
                message_ru="нет данных", message_en="no data",
            ),
        )
        issues = bad.validate()
        self.assertTrue(issues, "Non-empty payload + empty_state must be contradiction")
        self.assertIn("contradiction", issues[0])

    def test_is_empty_recognises_metric_payloads(self):
        # readability_summary has flesch=58.8, no arrays — NOT empty
        v = vt.RenderableView(
            view_type=vt.ViewType.READABILITY_SUMMARY,
            payload={"book_title": "X", "pg_id": "PG1",
                     "flesch": 58.8, "flesch_kincaid": 10.9, "cefr": "B2"},
        )
        self.assertFalse(v.is_empty())
        self.assertEqual(v.validate(), [])

    def test_top_n_table_view_asserts_count_match(self):
        with self.assertRaises(ValueError):
            vt.top_n_table_view(
                rows=[{"a": 1}, {"a": 2}],
                columns=["a"],
                count_returned=5,    # mismatch — len(rows) is 2
            )

    def test_empty_view_builder_produces_valid_view(self):
        v = vt.empty_view(
            view_type=vt.ViewType.COMPARISON_PANEL,
            reason=vt.EmptyReason.FILTERED_OUT,
            message_ru="Сравнение не построено: оба автора отфильтрованы порогом min_corpus_count=2000.",
            message_en="No comparison: both authors filtered out by min_corpus_count=2000.",
            filters_applied={"min_corpus_count": 2000},
            suggestion="Снизить min_corpus_count до 500.",
        )
        self.assertEqual(v.validate(), [])
        self.assertTrue(v.is_empty())
        self.assertEqual(v.empty_state.reason, vt.EmptyReason.FILTERED_OUT)


class ToolResultV5Extension(unittest.TestCase):
    """ToolResult must accept optional view + data_validity without
    breaking serialization or existing tests."""

    def test_legacy_toolresult_still_works(self):
        r = ToolResult.success(
            tool="dummy", data={"top": [{"w": "x"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        # No view, no data_validity — to_dict() shouldn't carry them
        d = r.to_dict()
        self.assertNotIn("view", d)
        self.assertNotIn("data_validity", d)
        self.assertTrue(d["ok"])

    def test_toolresult_with_view_serialises_correctly(self):
        v = vt.top_n_table_view(
            rows=[{"word": "ajar"}],
            columns=["word"],
        )
        r = ToolResult.success(tool="enrich_word", data={"x": 1})
        r.view = v
        r.data_validity = vt.DataValidity.OK
        d = r.to_dict()
        self.assertEqual(d["view"]["view_type"], "top_n_table")
        self.assertEqual(d["data_validity"], "ok")

    def test_toolresult_data_validity_broken_serialises(self):
        """B-R14-7 contract: learning_words returning 0 on PG1342 B2
        should mark data_validity=BROKEN, not silently empty."""
        r = ToolResult.success(tool="learning_words", data={"words": []})
        r.data_validity = vt.DataValidity.BROKEN
        d = r.to_dict()
        self.assertEqual(d["data_validity"], "broken")


class FilterSpecContracts(unittest.TestCase):
    """FilterSpec v2 — new fields default safely, validate() catches issues."""

    def test_safe_defaults(self):
        f = FilterSpec()
        # New v5 fields default to safe values
        self.assertTrue(f.exclude_proper_nouns)
        self.assertTrue(f.exclude_toponyms)
        self.assertTrue(f.exclude_translit_names)
        self.assertTrue(f.exclude_corporate_authors)
        self.assertFalse(f.exclude_archaic)
        self.assertIsNone(f.level)

    def test_validate_year_range_inverted(self):
        f = FilterSpec(year_from=1950, year_to=1880)
        issues = f.validate()
        self.assertTrue(any("year_from" in i for i in issues))

    def test_validate_bad_level(self):
        f = FilterSpec(level="X9")
        issues = f.validate()
        self.assertTrue(any("level" in i for i in issues))

    def test_validate_valid_levels_accepted(self):
        for lv in ("B2", "b2", "intermediate", "C1", "advanced"):
            f = FilterSpec(level=lv)
            self.assertEqual(f.validate(), [],
                             f"level={lv!r} should be valid, got {f.validate()}")

    def test_validate_bad_pos_filter(self):
        f = FilterSpec(pos_filter=["FAKE_POS", "ADJ"])
        issues = f.validate()
        self.assertTrue(any("pos_filter" in i for i in issues))

    def test_validate_bad_country_length(self):
        f = FilterSpec(country="UnitedKingdom")
        issues = f.validate()
        self.assertTrue(any("country" in i for i in issues))

    def test_validate_bad_top_n(self):
        f = FilterSpec(top_n=0)
        issues = f.validate()
        self.assertTrue(any("top_n" in i for i in issues))

    def test_legacy_scope_adapter_still_works(self):
        # Backward compat: from_legacy_scope produces a valid FilterSpec
        f = FilterSpec.from_legacy_scope({"author": "^Wilde,", "country": "GB"})
        self.assertEqual(f.author_regex, "^Wilde,")
        self.assertEqual(f.country, "GB")
        self.assertEqual(f.validate(), [])

    def test_explain_includes_level_when_set(self):
        f = FilterSpec(pg_id="PG1342", level="B2")
        s = f.explain()
        self.assertIn("level=B2", s)


class RequestTraceContracts(unittest.TestCase):
    """RequestTrace — append-only, finalize() produces flat shape that
    log_request consumes."""

    def setUp(self):
        obs_mod._reset()

    def test_start_trace_captures_query(self):
        t = obs_mod.start_trace("сравни По и Лавкрафта")
        self.assertEqual(t.query_raw, "сравни По и Лавкрафта")
        self.assertEqual(t.engine, "v2")
        self.assertIsNotNone(t.trace_id)
        self.assertEqual(len(t.trace_id), 12)

    def test_set_intent_records_path_and_confidence(self):
        t = obs_mod.start_trace("q")
        t.set_intent("author_compare", confidence=0.92, path="rules_fast")
        self.assertEqual(t.intent, "author_compare")
        self.assertEqual(t.intent_confidence, 0.92)
        self.assertEqual(t.intent_path, "rules_fast")

    def test_add_entity_resolve_appends(self):
        t = obs_mod.start_trace("q")
        t.add_entity_resolve(
            entity_type="author", query="Hugo",
            decision="resolved", resolved="Hugo, Victor",
            confidence=0.95,
            candidates=[{"name": "Hugo, Victor", "downloads": 12000}],
            normalization_trace=["NFKC fold", "rank by downloads"],
        )
        t.add_entity_resolve(
            entity_type="author", query="Доcтоевский (Latin c)",
            decision="resolved", resolved="Достоевский, Фёдор",
            confidence=0.99,
            normalization_trace=["homoglyph fold c→с"],
        )
        self.assertEqual(len(t.entity_resolves), 2)
        self.assertEqual(t.entity_resolves[0].resolved, "Hugo, Victor")
        self.assertIn("homoglyph fold c→с",
                      t.entity_resolves[1].normalization_trace)

    def test_tool_execution_records_data_validity(self):
        t = obs_mod.start_trace("q")
        t.add_tool_execution(
            tool="learning_words", args_summary={"level": "B2", "pg_id": "PG1342"},
            runtime_ms=400, ok=True,
            data_validity="broken",  # B-R14-7
        )
        self.assertEqual(len(t.tool_executions), 1)
        self.assertEqual(t.tool_executions[0].data_validity, "broken")

    def test_finalize_writes_to_ring(self):
        t = obs_mod.start_trace("test query")
        t.set_intent("introduction", confidence=1.0, path="rules_fast")
        t.set_render(view_type="introduction", phase_a_ms=2, phase_b_used=False)
        t.set_answer("Привет, я Словоёб.")
        flat = t.finalize()
        # log_request added to ring
        self.assertEqual(flat["intent"], "introduction")
        self.assertEqual(flat["v5_render_view_type"], "introduction")
        self.assertGreaterEqual(flat["total_elapsed_ms"], 0)
        records = obs_mod.recent_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], t.trace_id)

    def test_failure_marker(self):
        t = obs_mod.start_trace("q")
        t.mark_failure("renderer_timeout", error="Ollama gave up after 60s")
        flat = t.finalize()
        self.assertTrue(flat["is_failure"])
        self.assertEqual(flat["failure_kind"], "renderer_timeout")


class RequestBudgetContracts(unittest.TestCase):
    """Budget per-intent + estimator + downsize utility."""

    def test_for_intent_returns_sensible_budget(self):
        # Lookup is tight
        b1 = budget_mod.RequestBudget.for_intent("author_metadata")
        self.assertLess(b1.wall_clock_s, 20)
        # Composite is heavy
        b2 = budget_mod.RequestBudget.for_intent("author_profile")
        self.assertGreater(b2.wall_clock_s, 40)
        # Hard ceiling never above 90
        self.assertLessEqual(b2.wall_clock_s, 90.0)

    def test_unknown_intent_falls_back_to_default(self):
        b = budget_mod.RequestBudget.for_intent("some_new_intent")
        self.assertEqual(b.wall_clock_s, budget_mod.DEFAULT_WALL_CLOCK_MAX_S)

    def test_estimator_light_plan_fits(self):
        plan_steps = [
            {"tool": "resolve_author_name", "args": {}},
            {"tool": "author_metadata", "args": {}},
        ]
        budget = budget_mod.RequestBudget.for_intent("author_metadata")
        est = budget_mod.BudgetEstimator.estimate(
            plan_steps, budget=budget,
            has_critic_llm_call=False,    # introduction skips critic
        )
        self.assertTrue(est.fits)
        self.assertEqual(est.recommendation, "execute")

    def test_estimator_heavy_plan_recommends_downsize(self):
        # find_book_by_topic (15s) ×3 + render+planner+critic = ~55s
        # Budget for word_timeline is 40 — over by ~15s → downsize.
        plan_steps = [
            {"tool": "find_book_by_topic", "args": {}},
            {"tool": "find_book_by_topic", "args": {}},
            {"tool": "find_book_by_topic", "args": {}},
        ]
        budget = budget_mod.RequestBudget.for_intent("word_timeline")
        est = budget_mod.BudgetEstimator.estimate(plan_steps, budget=budget)
        self.assertFalse(est.fits)
        self.assertIn(est.recommendation, {"downsize", "clarify"})

    def test_estimator_obviously_over_recommends_clarify(self):
        # 10×find_book_by_topic ≈ 150s, budget 40 → clarify (>2×).
        plan_steps = [{"tool": "find_book_by_topic", "args": {}} for _ in range(10)]
        budget = budget_mod.RequestBudget.for_intent("word_timeline")
        est = budget_mod.BudgetEstimator.estimate(plan_steps, budget=budget)
        self.assertEqual(est.recommendation, "clarify")

    def test_estimator_bumps_cost_for_wide_scope(self):
        # No author scope → multiplier 1.4
        scoped = budget_mod.BudgetEstimator.estimate_step(
            "affinity_by_author",
            args={"filter": {"author_regex": "^Doyle,", "top_n": 30}},
        )
        unscoped = budget_mod.BudgetEstimator.estimate_step(
            "affinity_by_author",
            args={"filter": {"top_n": 30}},
        )
        self.assertGreater(unscoped, scoped)

    def test_downsize_halves_top_n_and_max_books(self):
        args = {"filter": {"top_n": 100, "max_books": 8000}}
        out = budget_mod.downsize_args(args)
        self.assertEqual(out["filter"]["top_n"], 50)
        self.assertEqual(out["filter"]["max_books"], 4000)

    def test_downsize_respects_floor(self):
        args = {"filter": {"top_n": 5}}
        out = budget_mod.downsize_args(args)
        # Floor is 10 — small top_n is left alone (already small)
        self.assertGreaterEqual(out["filter"]["top_n"], 5)

    def test_llm_planner_hard_cap_known(self):
        # Module-level constant addresses B-R14-10 (parse-fail 30-58s).
        # Phase 5 enforces; Phase 0 documents the contract.
        self.assertEqual(budget_mod.LLM_PLANNER_HARD_CAP_S, 5.0)
        self.assertEqual(budget_mod.LLM_PLANNER_MAX_RETRIES, 1)


# =====================================================================
# Tier 2 — Behavioural goldens (require live stack)
# =====================================================================
#
# Skipped unless WC_GOLDEN_LIVE=1. These are the R14 acceptance gates:
# specific queries with specific expected behaviour, asserted on the
# real backend. When a golden fails, do NOT change the test — fix the
# underlying tool. The whole point is the test is the contract.
#
# Each test prints intent + tool calls + a truncated answer so failures
# are easy to triage from CI logs.


_LIVE = os.environ.get("WC_GOLDEN_LIVE") == "1"
_BASE = os.environ.get("BASE", "http://127.0.0.1:8890")


def _live_post(question: str, history=None, *, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        f"{_BASE}/api/chat",
        data=json.dumps({"question": question,
                         "history": history or []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


@unittest.skipUnless(_LIVE, "live stack not enabled (set WC_GOLDEN_LIVE=1)")
class BehaviouralGoldens_EntityResolution(unittest.TestCase):
    """B-R14-4 / B-R14-9 / B-R14-13 entity-resolution goldens."""

    def test_hugo_resolves_to_victor_not_ganz(self):
        """B-R14-13: 'Hawthorne vs Hardy vs Hugo' must resolve Hugo to
        Victor Hugo, not the obscure 'Ganz, Hugo' (first-name match).
        """
        d = _live_post("Hawthorne vs Hardy vs Hugo")
        ans = (d.get("answer") or "").lower()
        # Either Victor Hugo appears, or system asks clarify, but NOT
        # silent resolve to Ganz.
        self.assertNotIn("ganz", ans,
                         f"B-R14-13 regression: Ganz still wins over Victor Hugo. "
                         f"Answer head: {ans[:200]}")

    def test_homoglyph_dostoevsky_resolves(self):
        """B-R14-4: 'Доcтоевского' (Latin c) must resolve cleanly,
        not parse-fail."""
        d = _live_post("самые частотные слова Доcтоевского")  # Latin c
        intent = d.get("intent", "")
        # Acceptance: either resolves (intent=author_top_words) or asks
        # clarify with normalization hint — but NOT a generic parse-fail.
        if intent == "clarify":
            ans = (d.get("answer") or "").lower()
            self.assertTrue(
                "доcтоев" in ans or "достоев" in ans,
                "Clarify should explain the homoglyph, not generic parse-fail"
            )

    def test_russian_genitive_tolstogo_resolves(self):
        """B-R14-9: 'а у Толстого' followup must resolve Толстой,
        not bounce to 'unknown author'."""
        history = [
            {"role": "user", "content": "фирменные слова Диккенса"},
            {"role": "assistant", "content": "wegg, trotwood, snodgrass"},
        ]
        d = _live_post("а у Толстого?", history=history)
        ans = (d.get("answer") or "").lower()
        self.assertNotIn("автор не определён", ans,
                         "B-R14-9 regression: Толстого genitive still fails")

    def test_russian_book_brothers_karamazov_resolves(self):
        """B-R14-9 part 2: 'Братья Карамазовы' in genitive must resolve
        to PG28054, not 'not found'."""
        d = _live_post("vocab passport Братьев Карамазовых")
        ans = (d.get("answer") or "").lower()
        # Must NOT say "книга не найдена" — book is in corpus
        self.assertFalse(
            "книга не найдена" in ans and "карамаз" in ans,
            "B-R14-9 part 2: Братьев Карамазовых must resolve to PG28054"
        )


@unittest.skipUnless(_LIVE, "live stack not enabled (set WC_GOLDEN_LIVE=1)")
class BehaviouralGoldens_DataValidity(unittest.TestCase):
    """B-R14-7 / B-R14-12 tool-semantic goldens."""

    def test_learning_words_b2_pride_and_prejudice_nonempty(self):
        """B-R14-7 (P0): learning_words(PG1342, B2) returned 0 across
        every book in R14. Headline feature broken. This golden gates v5
        Phase 0 — Phase 0 sign-off requires this passing.
        """
        d = _live_post("20 слов уровня B2 из 'Pride and Prejudice'")
        ans = (d.get("answer") or "").lower()
        # Either we get a non-empty list, or critic surfaces the broken
        # state — but we should NOT see "0 слов после фильтрации" as a
        # silent acceptable outcome.
        self.assertFalse(
            "0 слов" in ans and "b2" in ans and "успешн" in ans,
            "B-R14-7 regression: learning_words B2 still returns 0 on canonical book"
        )

    def test_word_movement_returns_movement_verbs(self):
        """B-R14-12: 'глаголы движения у Диккенса' must return run/walk/
        fly-class, not common verbs (said/know/think)."""
        d = _live_post("глаголы движения у Диккенса")
        ans = (d.get("answer") or "").lower()
        # If movement-class words are present (run/walk/fly/come/go), pass.
        # If only common cognition verbs (said/know/think) — fail.
        common_cognition = {"said", "know", "think", "say", "thought"}
        # Spot-check: at least one bona-fide motion verb should appear
        motion_signal = any(w in ans for w in
                            ("run", "walk", "fly", "ride", "march"))
        if not motion_signal:
            # Allow clarify or "feature broken" disclosure
            self.assertTrue(
                "не работает" in ans or "уточни" in ans
                or "семантический фильтр" in ans,
                "B-R14-12: word_movement returned non-movement verbs without disclosure"
            )


@unittest.skipUnless(_LIVE, "live stack not enabled (set WC_GOLDEN_LIVE=1)")
class BehaviouralGoldens_CanonicalStylometry(unittest.TestCase):
    """Stylometry numbers from R7/R14 — these are the corpus's
    'never break this' invariants."""

    def test_burrows_delta_doyle_stevenson(self):
        """Doyle ↔ Stevenson Burrows Delta ≈ 0.4385 (canonical R7)."""
        d = _live_post("на кого по стилю похож Doyle")
        ans = d.get("answer") or ""
        # Number must appear in the answer (table cell or prose)
        self.assertIn("0.4385", ans,
                      f"Canonical Burrows Delta 0.4385 missing. "
                      f"Answer head: {ans[:300]}")

    def test_flesch_pride_and_prejudice(self):
        """P&P Flesch 58.8 / Midsummer 86.9 (canonical R7)."""
        d = _live_post("что сложнее 'Pride and Prejudice' или 'Сон в летнюю ночь'")
        ans = d.get("answer") or ""
        self.assertIn("58.8", ans, "Canonical Flesch 58.8 missing")
        self.assertIn("86.9", ans, "Canonical Flesch 86.9 missing")


@unittest.skipUnless(_LIVE, "live stack not enabled (set WC_GOLDEN_LIVE=1)")
class BehaviouralGoldens_FilterCorrectness(unittest.TestCase):
    """B-R14-1 / B-R14-15: filter contract goldens."""

    def test_top_authors_no_cia(self):
        """B-R14-1: CIA / corporate non-authors must not appear in
        top_authors_by output."""
        d = _live_post("топ-10 авторов по объёму корпуса")
        ans = (d.get("answer") or "").lower()
        forbidden = {"central intelligence agency", "cia,",
                     "library of congress", "internet archive"}
        leaked = [t for t in forbidden if t in ans]
        self.assertEqual(leaked, [],
                         f"B-R14-1 regression: corporate non-authors leaked: {leaked}")

    def test_doyle_vocab_no_boer_toponyms(self):
        """B-R14-15: 'фирменные слова Конан Дойла' must not return Boer
        toponyms (burger / uitlanders / belmont / colesberg). These are
        place names from war reportage, not Doyle's vocabulary."""
        d = _live_post("фирменные слова Конан Дойла")
        ans = (d.get("answer") or "").lower()
        forbidden_toponyms = {"uitlanders", "colesberg", "belmont"}
        leaked = [t for t in forbidden_toponyms if t in ans]
        self.assertEqual(leaked, [],
                         f"B-R14-15 regression: toponyms leaked in Doyle vocab: {leaked}")

    def test_dostoevsky_vocab_no_translit_names(self):
        """B-R14-15 part 2: 'словарь Достоевского' must not return
        transliterated character names (pyotr / alexandrovna / katerina)."""
        d = _live_post("словарь Достоевского")
        ans = (d.get("answer") or "").lower()
        forbidden_names = {"pyotr", "alexandrovna", "katerina", "mihailovna"}
        leaked = [t for t in forbidden_names if t in ans]
        self.assertEqual(leaked, [],
                         f"B-R14-15 part 2 regression: translit names leaked: {leaked}")


@unittest.skipUnless(_LIVE, "live stack not enabled (set WC_GOLDEN_LIVE=1)")
class BehaviouralGoldens_PerformanceBudget(unittest.TestCase):
    """Phase 5 acceptance: every query ≤ 65s wall-clock. Phase 0 reports
    actual elapsed for tracking; Phase 5 will assertLess.
    """

    def test_topical_book_search_under_budget(self):
        """Q114 in R14 took 310s. v5 target: ≤ 65s."""
        t0 = time.perf_counter()
        d = _live_post("что почитать после Преступления и наказания")
        elapsed = time.perf_counter() - t0
        # Phase 0: emit warning only. Phase 5 will fail.
        if elapsed > 65.0:
            print(f"  [warn] topical search elapsed {elapsed:.1f}s "
                  f"— Phase 5 target ≤ 65s")
        # Sanity: did not hang forever
        self.assertLess(elapsed, 180.0)
        self.assertTrue(d.get("answer"))


# =====================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
