"""RequestBudget — global per-request envelope.

Part of the v5 architectural refactor ([[architecture_refactor_v5_plan]] §P7).
Closes R14 latency regressions:
  - Q114 310s (book_similar deep search), Q89 196s (multi-word timeline),
    Q96 162s (cross-book etymology), Q105 152s (learning_words wide scope)
  - B-R14-10: parse-fail spent 30-58s in LLM planner before clarify

Phase 0: this module exists with the contract, but is NOT yet wired into
rag_v2.ask/ask_stream — that's Phase 5. Plan builders can opt-in by
calling `BudgetEstimator.estimate(plan)` for a sanity check.

Phase 5: pre-execute estimate compared to budget; if exceeded, plan is
either downsized (lower top_n, shrink scope) or replaced with a clarify
("this is a 5-minute query, narrow it down?").

The budget is wall-clock-first. Token / call-count caps are secondary
guards. We don't bill on tokens; we care about user-perceived latency.

Design notes:
- Per-intent defaults: heavy intents (composite, multi-author) get
  bigger budgets than lookup intents.
- Cost estimates per tool are intentionally rough. They get refined
  from production traces (Phase 6 will export the trace-derived cost
  table to replace these defaults).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


# Rough per-tool cost in seconds (median, not p95). Derived from
# pipeline_trace + R14 timings. Used by BudgetEstimator.
STEP_COSTS_S: dict[str, float] = {
    # Resolution / metadata (light)
    "resolve_author_name": 0.5,
    "resolve_book_title": 0.5,
    "find_book": 1.0,
    "author_metadata": 1.0,
    "corpus_overview": 0.5,
    "corpus_stats_by_author": 2.0,

    # Affinity / stylometry (medium)
    "affinity_by_author": 3.0,
    "affinity_by_book": 3.0,
    "top_ngrams_by_author": 2.5,
    "lexical_diversity": 2.0,
    "compare_authors": 5.0,
    "author_attribution": 4.0,
    "author_influences": 4.0,

    # Word-level (light to medium)
    "word_contexts": 2.0,
    "word_contexts_global": 3.0,
    "word_collocates": 4.0,
    "word_pos_distribution": 1.5,
    "enrich_word": 2.5,
    "word_etymology": 2.0,
    "find_words_by_etymology": 8.0,

    # Time-series (heavy)
    "word_freq_timeline": 12.0,

    # Readability / emotion (medium)
    "book_readability": 2.0,
    "book_archaic_words": 3.0,
    "book_emotion_profile": 4.0,
    "emotion_collocates": 5.0,

    # Search (heavy)
    "semantic_search": 3.0,
    "lexical_search": 1.5,
    "hybrid_search": 4.0,
    "find_book_by_topic": 15.0,        # R14 worst-case 50s/call

    # Learning (medium)
    "learning_words": 6.0,
    "export_word_list": 0.2,

    # Top-N / browse (light)
    "top_authors_by": 1.0,
    "top_authors_by_country": 1.5,
    "top_books_by_downloads": 1.0,
    "top_books_by_recency": 1.5,
    "words_disappearing_after": 4.0,

    # Composites (heavy)
    "author_profile": 12.0,            # 6 sub-tools in parallel
    "vocab_passport": 10.0,            # composite

    # LLM stages (separately accounted)
    "_planner_llm": 3.0,
    "_render_phase_b": 4.0,
    "_critic_llm": 3.0,
}

# Per-intent budget defaults. Heavy intents get more headroom; lookup
# intents are tight to keep p50 fast.
INTENT_BUDGETS_S: dict[str, float] = {
    # Lookup / metadata — tight
    "author_metadata": 15.0,
    "author_lookup": 15.0,
    "book_lookup": 15.0,
    "corpus_meta": 10.0,
    "introduction": 5.0,
    "out_of_scope": 5.0,

    # Single-tool analysis — medium
    "author_vocab": 25.0,
    "author_top_words": 25.0,
    "book_readability": 20.0,
    "book_archaic": 25.0,
    "book_emotion": 30.0,
    "word_etymology": 20.0,
    "word_contexts": 20.0,
    "word_collocates": 25.0,
    "word_pos": 15.0,

    # Stylometry / comparison — heavy
    "author_compare": 40.0,
    "author_closest": 35.0,
    "author_influences": 35.0,
    "author_attribution": 30.0,

    # Recommendation / search — heavy
    "book_recommendation": 30.0,
    "book_similar": 30.0,
    "topic_book_search": 35.0,

    # Time series — heavy
    "word_timeline": 40.0,

    # Composites — heaviest
    "author_profile": 50.0,
    "vocab_passport": 45.0,
    "country_vocab": 35.0,
    "country_compare": 45.0,

    # Learning — medium
    "learning": 30.0,
    "export_word_list": 10.0,

    # Multi-author / composite from R14 (set tight to force downsize)
    "_composite_multi_author": 60.0,
    "_composite_multi_book": 60.0,
}

# Global hard ceiling. Past this, request must downsize or clarify.
DEFAULT_WALL_CLOCK_MAX_S = 60.0

# LLM planner hard cap (B-R14-10: parse-fail wasted 30-58s).
LLM_PLANNER_HARD_CAP_S = 5.0
LLM_PLANNER_MAX_RETRIES = 1


@dataclass
class RequestBudget:
    """Per-request envelope. Pre-execute estimator compares plan to this;
    Phase 5 enforces downsize-or-clarify if exceeded.

    `wall_clock_s` is the user-perceived hard deadline. Past this we
    abort or surface a partial result. `llm_calls_max` and
    `tool_calls_max` are secondary guards — exceeding them is suspicious
    and gets logged but doesn't auto-abort (a 12-step DAG may be legit).

    `response_bytes_max` is enforced at the frontend layer (P8) — server
    truncates with a "full response in server log, trace_id=…" marker.

    Phase 5: `ts_start` фиксируется в момент создания бюджета (или явно
    через `set_clock`), и `remaining_s()` отдаёт «сколько секунд осталось
    до wall_clock_s». Этот метод — единый источник истины для
    dispatch-chokepoint при вычислении effective tool timeout.
    """
    wall_clock_s: float = DEFAULT_WALL_CLOCK_MAX_S
    llm_calls_max: int = 3              # planner + render + critic
    tool_calls_max: int = 12
    response_bytes_max: int = 200_000

    # The intent the budget was sized for (for log/debug)
    sized_for_intent: str | None = None
    ts_start: float = field(default_factory=time.perf_counter)

    def remaining_s(self) -> float:
        """Seconds left in the envelope. Negative = already over budget;
        callers should treat <=0 as «no time, fail fast»."""
        return self.wall_clock_s - (time.perf_counter() - self.ts_start)

    def set_clock(self, ts_start: float) -> None:
        """Re-anchor the budget clock — used when budget is derived from a
        pipeline envelope that started earlier (planner/entity resolution
        already consumed some wall-clock before the router gets here)."""
        self.ts_start = ts_start

    @classmethod
    def for_intent(cls, intent: str | None) -> "RequestBudget":
        wc = INTENT_BUDGETS_S.get(intent or "", DEFAULT_WALL_CLOCK_MAX_S)
        # Hard ceiling — never above 90s even for heavy composites.
        wc = min(wc, 90.0)
        return cls(wall_clock_s=wc, sized_for_intent=intent)


@dataclass
class BudgetUsage:
    """Running tally for a request. Pipeline layers update it as they
    execute; RequestTrace persists the final snapshot."""
    wall_clock_used_s: float = 0.0
    llm_calls_used: int = 0
    tool_calls_used: int = 0
    response_bytes: int = 0
    exceeded_at: str | None = None          # which stage tripped the budget
    ts_start: float = field(default_factory=time.perf_counter)

    def tick(self) -> None:
        self.wall_clock_used_s = time.perf_counter() - self.ts_start

    def exceeded(self, budget: RequestBudget) -> bool:
        self.tick()
        if self.wall_clock_used_s > budget.wall_clock_s:
            if self.exceeded_at is None:
                self.exceeded_at = "wall_clock"
            return True
        if self.llm_calls_used > budget.llm_calls_max:
            if self.exceeded_at is None:
                self.exceeded_at = "llm_calls"
            return True
        if self.tool_calls_used > budget.tool_calls_max:
            if self.exceeded_at is None:
                self.exceeded_at = "tool_calls"
            return True
        return False


@dataclass
class BudgetEstimate:
    """Pre-execute estimate of plan cost."""
    estimated_wall_clock_s: float
    estimated_llm_calls: int
    estimated_tool_calls: int
    fits: bool
    headroom_s: float                 # budget - estimate (negative if over)
    recommendation: Literal["execute", "downsize", "clarify"]
    reason: str | None = None
    cost_breakdown: dict[str, float] = field(default_factory=dict)


class BudgetEstimator:
    """Estimate cost of a plan before executing it.

    Phase 0: free-standing utility. Tests exercise it; plan-builders may
    opt-in. Phase 5: rag_v2.ask uses it to choose execute / downsize /
    clarify per plan.
    """

    @staticmethod
    def estimate_step(tool: str, args: dict | None = None) -> float:
        """Estimate one tool call cost. `args` may bump cost (e.g. wide
        `max_books` or large `top_n`)."""
        base = STEP_COSTS_S.get(tool, 3.0)
        if not args:
            return base
        # Wide scope = cost multiplier
        mult = 1.0
        max_books = args.get("max_books") or args.get("filter", {}).get("max_books")
        if isinstance(max_books, int) and max_books > 5000:
            mult *= 1.5
        top_n = args.get("top_n") or args.get("filter", {}).get("top_n")
        if isinstance(top_n, int) and top_n > 100:
            mult *= 1.3
        # No author/book scope = whole-corpus query, more expensive
        f = args.get("filter") or {}
        if isinstance(f, dict):
            has_scope = bool(
                f.get("author_regex") or f.get("pg_id") or f.get("user_id")
            )
            if not has_scope:
                mult *= 1.4
        return base * mult

    @staticmethod
    def estimate(plan_steps: list[dict], *,
                 budget: RequestBudget,
                 has_planner_llm_call: bool = True,
                 has_render_llm_call: bool = True,
                 has_critic_llm_call: bool = True) -> BudgetEstimate:
        """Estimate total cost of a plan.

        plan_steps: list of {tool: str, args: dict} — works for both v3
        QueryPlan-style steps and v4 PlanSpec steps.
        """
        cost_breakdown: dict[str, float] = {}
        total = 0.0

        # LLM stages
        if has_planner_llm_call:
            c = STEP_COSTS_S["_planner_llm"]
            cost_breakdown["_planner_llm"] = c
            total += c
        if has_render_llm_call:
            c = STEP_COSTS_S["_render_phase_b"]
            cost_breakdown["_render_phase_b"] = c
            total += c
        if has_critic_llm_call:
            c = STEP_COSTS_S["_critic_llm"]
            cost_breakdown["_critic_llm"] = c
            total += c

        # Tool steps. Sibling-parallel steps are not (yet) recognised —
        # we sum costs as if sequential. Phase 6 will refine this with
        # parallel-aware estimator once router DAG-execute timings are
        # logged in traces.
        for step in plan_steps:
            tool = step.get("tool") if isinstance(step, dict) else getattr(step, "tool", "")
            args = step.get("args") if isinstance(step, dict) else getattr(step, "args", {})
            c = BudgetEstimator.estimate_step(tool, args)
            cost_breakdown[tool] = cost_breakdown.get(tool, 0.0) + c
            total += c

        llm_calls = sum(1 for x in (has_planner_llm_call, has_render_llm_call,
                                      has_critic_llm_call) if x)
        tool_calls = len(plan_steps)

        fits = (
            total <= budget.wall_clock_s
            and llm_calls <= budget.llm_calls_max
            and tool_calls <= budget.tool_calls_max
        )
        headroom = budget.wall_clock_s - total
        recommendation: Literal["execute", "downsize", "clarify"]
        reason: str | None
        if fits:
            recommendation = "execute"
            reason = None
        elif total > budget.wall_clock_s * 2:
            recommendation = "clarify"
            reason = (
                f"Estimated {total:.1f}s vs budget {budget.wall_clock_s:.1f}s"
                f" — too far over to downsize; ask user to narrow."
            )
        else:
            recommendation = "downsize"
            reason = (
                f"Estimated {total:.1f}s vs budget {budget.wall_clock_s:.1f}s"
                f" — try lowering top_n / max_books / scope."
            )

        return BudgetEstimate(
            estimated_wall_clock_s=total,
            estimated_llm_calls=llm_calls,
            estimated_tool_calls=tool_calls,
            fits=fits,
            headroom_s=headroom,
            recommendation=recommendation,
            reason=reason,
            cost_breakdown=cost_breakdown,
        )


def downsize_args(args: dict | None) -> dict | None:
    """Apply a single downsize pass to tool args: halve top_n / max_books.

    Phase 5 plan-builder calls this when estimator returns
    `recommendation="downsize"`, then re-estimates. If still over budget,
    falls back to clarify.

    Phase 0: free-standing utility, tests exercise it.
    """
    if not args:
        return args
    out = dict(args)
    for key in ("top_n", "max_books"):
        v = out.get(key)
        if isinstance(v, int) and v > 10:
            out[key] = max(10, v // 2)
    f = out.get("filter")
    if isinstance(f, dict):
        out["filter"] = downsize_args(f)
    return out


# Module-level marker
V5_BUDGET_VERSION = "0.1"
