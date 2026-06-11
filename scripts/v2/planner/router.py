"""Tool Router — execute a plan step-by-step, deterministic.

Contract: docs/v2/PLANNER.md §5.

The router doesn't talk to the LLM. It just runs the steps in order, threads
prior `ToolResult.data` into later args, and stops on hard failures (unless
the step is `optional`). This is what kills the "narrates plan, never calls
tool" failure mode from v1.1.7 — there's no LLM in the loop here.

Two public entry points (T1 / D-P1-8, 2026-05-23):
    `execute(plan_or_spec)`        — polymorphic; QueryPlan → v3 linear
                                     executor, PlanSpec → v4 DAG executor.
    `execute_stream(plan_or_spec)` — same dispatch, SSE-friendly generator.

Both produce a `RouterResult`. v4 plans support a DAG (not just a linear
chain) via `$sN.field` references in args + explicit `needs` lists. The
DAG is topologically ordered and resolved before each step dispatches.

Full v3/v4 plan-shape unification is deferred to T4 (plan.py decomposition):
the per-shape executors live as private helpers below — same v3 / v4
logic, no behaviour change. See D-P1-8 in docs/v2/decisions.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Union

from scripts.v2.planner.plan import PlanStep, QueryPlan
from scripts.v2.planner import plan_spec as _spec_mod
from scripts.v2.planner.invariants import apply_invariants
from scripts.v2.planner.plan_spec import PlanSpec
from scripts.v2.tool_registry import dispatch
from scripts.v2._types import ToolError, ToolResult, ToolWarning


PlanOrSpec = Union[QueryPlan, PlanSpec]


@dataclass
class StepEvent:
    # v5 Phase 5 — `budget_exceeded` event emitted when router aborts a
    # plan because RequestBudget.wall_clock_s ran out mid-DAG. Closes
    # B-R14-10/Q114-style runaway latency at the structural level.
    kind: Literal["step_start", "step_done", "step_skip", "budget_exceeded"]
    step_idx: int
    tool: str
    args: dict | None = None
    result: ToolResult | None = None
    reason: str | None = None  # for step_skip / budget_exceeded


@dataclass
class RouterResult:
    kind: Literal["clarify", "out_of_scope", "results", "no_steps"]
    plan: QueryPlan
    results: list[ToolResult] = field(default_factory=list)
    message: str | None = None  # clarify question or out_of_scope reason
    events: list[StepEvent] = field(default_factory=list)
    # v5 Phase 5 — set True when execution aborted on RequestBudget exceed.
    # Callers (rag_v2) read this to decide between rendering partial or
    # surfacing an ERROR_FRIENDLY view. Default False keeps backward compat.
    budget_exceeded: bool = False


def _inject(args: dict, prior_results: list[ToolResult],
            depends_on: list[int], inject_as: str | None) -> dict | None:
    """Thread output from a previous step into this step's args.

    Heuristic: when `inject_as == "pg_id"`, look up `data["first_id"]` on the
    most recent dependency. Extendable: add more named injections as we use
    them in plan templates.

    R-28 B121 — rank-indexed injections (`pg_id@N` / `word@N`) return None
    when the source row for rank N is unavailable (short source list,
    failed source, missing value). None means «this step must NOT
    dispatch» — callers skip it via `_skip_for_empty_injection` instead
    of running the tool with planned-but-empty args. Non-rank injections
    keep the old degrade (args returned unchanged; the tool surfaces its
    own invalid_args downstream)."""
    if not depends_on or inject_as is None:
        return args
    src = prior_results[depends_on[-1]]
    out = dict(args)
    if not src.ok or src.data is None:
        if "@" in inject_as:
            return None  # B121 — rank source failed → skip, not dispatch {}
        return out  # router will detect & error out below
    if inject_as == "pg_id":
        first_id = src.data.get("first_id") if isinstance(src.data, dict) else None
        if first_id:
            out["pg_id"] = first_id
    elif inject_as.startswith("pg_id@"):
        # R-27 WP1 learning_books — rank-indexed injection: pull row N
        # from the dependency's data["top"] list (top_books_by_downloads
        # shape) and inject its pg_id.
        # R-28 B120 — the row key is `id`: real v1 rows carry
        # {id, title, author, downloads} (golden fixture
        # scripts.rag_tools.top_books_by_downloads.json). The original
        # read of `pg_id` was a phantom key — never present in prod
        # rows — so this injection silently delivered {} to every
        # dependent step since 2.7.6 (B118's «Не указано в данных»
        # rows were this bug, not render variance).
        try:
            rank = int(inject_as.split("@", 1)[1])
        except ValueError:
            rank = -1
        rows = src.data.get("top") if isinstance(src.data, dict) else None
        if rank >= 0 and isinstance(rows, list) and rank < len(rows):
            row = rows[rank]
            pg = row.get("id") if isinstance(row, dict) else None
            if pg:
                out["pg_id"] = pg
                return out
        return None  # B121 — rank beyond source rows → skip
    elif inject_as.startswith("word@"):
        # R-27 WP1 Q20 — rank-indexed injection from learning_words
        # data["results"][N]["word"] for the enrich_word translate
        # fan-out («дай N слов из книги X с переводами»). Same shape as
        # pg_id@N above; `word` is in V1LearningWords.__row_keys__.
        try:
            rank = int(inject_as.split("@", 1)[1])
        except ValueError:
            rank = -1
        rows = src.data.get("results") if isinstance(src.data, dict) else None
        if rank >= 0 and isinstance(rows, list) and rank < len(rows):
            row = rows[rank]
            w = row.get("word") if isinstance(row, dict) else None
            if w:
                out["word"] = w
                return out
        return None  # B121 — rank beyond source rows → skip
    elif inject_as == "scope":
        # use the resolved book id as a scope dict
        first_id = src.data.get("first_id") if isinstance(src.data, dict) else None
        if first_id:
            out["scope"] = {"book": first_id}
    elif inject_as == "author_regex":
        # Sprint 11.4: pull the leader from top_authors_by(_country).data["top"][0]
        # and reshape "Surname, First" to v1's "^Surname," regex. Used by the
        # composite_compare plan to chain top-authors → affinity_by_author
        # for the leader of each country.
        if isinstance(src.data, dict):
            rows = src.data.get("top") or []
            if rows and isinstance(rows, list) and isinstance(rows[0], dict):
                author = (rows[0].get("author") or "").strip()
                if author:
                    surname = author.split(",", 1)[0].strip()
                    if surname:
                        out["author_regex"] = f"^{surname},"
    elif inject_as in src.data if isinstance(src.data, dict) else False:
        out[inject_as] = src.data[inject_as]
    return out


# B121 — rows-list key per rank-injection prefix. Mirrors the read in
# `_inject`; one place to extend when a new rank injection appears.
_RANK_SOURCE_KEYS = {"pg_id": "top", "word": "results"}


def _skip_for_empty_injection(plan: QueryPlan, step: PlanStep,
                              prior_results: list[ToolResult],
                              ) -> tuple[ToolResult, str]:
    """R-28 B121 — a step whose rank-indexed injection has no source row
    is SKIPPED, not dispatched with empty args.

    Returns (placeholder, reason). The placeholder keeps `results`
    index-aligned with `plan.steps` (depends_on indexes into results)
    and carries an `inject_shortfall` warning so the render payload
    shows WHY the step didn't run. Also stamps ONE shortfall render
    note onto the plan, reusing the WP3 (1e) count-honesty vocabulary
    (top_requested / top_returned) so renderer rule 14 treats the
    shortfall as the single source of truth instead of inventing the
    missing rows."""
    prefix = (step.inject_result_as or "").split("@", 1)[0]
    planned = sum(
        1 for s in plan.steps
        if (s.inject_result_as or "").startswith(prefix + "@")
        and s.depends_on == step.depends_on)
    avail = 0
    if step.depends_on:
        src = prior_results[step.depends_on[-1]]
        if src.ok and isinstance(src.data, dict):
            rows = src.data.get(_RANK_SOURCE_KEYS.get(prefix, "top"))
            avail = len(rows) if isinstance(rows, list) else 0
    reason = (f"injection {step.inject_result_as} unavailable — source "
              f"returned {avail} rows, plan requested {planned}")
    marker = "SHORTFALL ИСТОЧНИКА"
    if not any(marker in n for n in (plan.render_notes or [])):
        plan.render_notes = list(plan.render_notes or []) + [
            f"{marker}: план запрашивал {planned} шагов {step.tool} по "
            f"строкам источника (top_requested={planned}), но источник "
            f"вернул только {avail} строк (top_returned={avail}) — "
            f"исполнено {avail}. В ответе честно скажи, что показано "
            f"{avail} из {planned}; НЕ выдумывай недостающие строки."
        ]
    placeholder = ToolResult(
        ok=False, tool=step.tool, query=dict(step.args),
        error=ToolError(type="invalid_args", message=reason),
        warnings=[ToolWarning(
            code="inject_shortfall", message=reason,
            details={"inject_result_as": step.inject_result_as,
                     "available": avail, "planned": planned})],
    )
    return placeholder, reason


def execute(plan_or_spec: PlanOrSpec, *, budget=None) -> RouterResult:
    """Run a plan to completion. Always returns a RouterResult.

    T1 / D-P1-8 (2026-05-23) — polymorphic dispatch over QueryPlan vs
    PlanSpec; v3 and v4 share this entry point. Internal split lives
    in `_execute_query_plan` / `_execute_spec`.

    v5 Phase 5 — `budget` is an optional `RequestBudget`. After each
    step, router checks `budget.exceeded()` and aborts the remaining
    steps if true, returning what's already done plus a
    `budget_exceeded` StepEvent. Closes B-R14-10/Q114 (310s runaway)
    structurally — no plan can exceed the request envelope."""
    if isinstance(plan_or_spec, PlanSpec):
        return _execute_spec(plan_or_spec, budget=budget)
    return _execute_query_plan(plan_or_spec, budget=budget)


def _execute_query_plan(plan: QueryPlan, *, budget=None) -> RouterResult:
    """v3 linear executor — `plan: QueryPlan` from `plan.py`.

    Implementation moved here from the old `execute()` entry point under
    D-P1-8. Same logic, just renamed.
    """
    if plan.needs_clarify:
        return RouterResult(kind="clarify", plan=plan,
                            message=plan.clarify_question or "")
    if plan.out_of_scope_reason:
        return RouterResult(kind="out_of_scope", plan=plan,
                            message=plan.out_of_scope_reason)
    # Phase 4 — apply plan-level invariants (fan-out per multi-author,
    # timeout hook, clarify-guard hook) BEFORE executing. Builders now
    # emit a single primary step plus a `fan_out` marker; the invariant
    # expands the marker into N+1 steps per `multi_author_regex`. No
    # builder reimplements the fan-out loop anymore (R6).
    plan = apply_invariants(plan, budget=budget)
    if not plan.steps:
        return RouterResult(kind="no_steps", plan=plan,
                            message=plan.explain or "no tools to call")

    results: list[ToolResult] = []
    events: list[StepEvent] = []
    usage = None
    if budget is not None:
        try:
            from scripts.v2.budget import BudgetUsage
            usage = BudgetUsage()
        except Exception:
            usage = None

    for idx, step in enumerate(plan.steps):
        # Budget check BEFORE dispatching a new step — if we're already
        # over, surface immediately rather than running another heavy tool.
        if usage is not None and usage.exceeded(budget):
            events.append(StepEvent(
                kind="budget_exceeded",
                step_idx=idx,
                tool=step.tool,
                reason=f"wall_clock {usage.wall_clock_used_s:.1f}s "
                       f"exceeded budget {budget.wall_clock_s:.1f}s "
                       f"at step {idx}/{len(plan.steps)}",
            ))
            return RouterResult(kind="results", plan=plan,
                                results=results, events=events,
                                budget_exceeded=True)
        args = _inject(step.args, results, step.depends_on, step.inject_result_as)
        if args is None:
            # B121 — rank-indexed injection has no source row: skip the
            # step (no dispatch), keep results index-aligned, surface
            # the shortfall to the renderer via plan.render_notes.
            placeholder, reason = _skip_for_empty_injection(
                plan, step, results)
            results.append(placeholder)
            events.append(StepEvent(kind="step_skip", step_idx=idx,
                                    tool=step.tool, reason=reason))
            continue
        events.append(StepEvent(kind="step_start", step_idx=idx,
                                tool=step.tool, args=args))
        # Phase 5 chokepoint: budget идёт ВНУТРЬ dispatch, где effective
        # timeout = min(spec.timeout_s, budget.remaining_s). Гарантирует,
        # что ни один тул не переживёт request envelope. T5 (2026-05-23) —
        # legacy_dispatch удалён, `dispatch` сам идёт по v1-пути при промахе
        # в v2 REGISTRY, без отдельной точки вызова.
        result = dispatch(step.tool, args, budget=budget)
        results.append(result)
        if usage is not None:
            usage.tool_calls_used += 1
        events.append(StepEvent(kind="step_done", step_idx=idx,
                                tool=step.tool, result=result))
        if not result.ok and not step.optional:
            # Hard fail: stop here. Renderer will surface the error.
            return RouterResult(kind="results", plan=plan,
                                results=results, events=events)

    return RouterResult(kind="results", plan=plan,
                        results=results, events=events)


def _execute_spec(spec: PlanSpec, *, budget=None) -> RouterResult:
    """v4 DAG executor — `spec: PlanSpec` from `plan_spec.py`.

    Steps run in topological order. Before dispatch each step's args
    are resolved against the result map (`step_id → ToolResult.data`).
    Failures abort like the v3 path unless `optional=True`.

    v5 Phase 5 — `budget` is an optional `RequestBudget`. After each
    step, router checks `budget.exceeded()` and aborts the remaining
    steps if true, returning a partial RouterResult with
    `budget_exceeded=True`. Closes B-R14-10/Q114 (310s runaway)
    structurally — no plan can exceed the request envelope.

    Returns a RouterResult with `results` aligned to the topological
    order. The `plan` field is set to a stub QueryPlan with the same
    intent_hint / clarify so existing callers that read `result.plan`
    still get something usable.

    T1 / D-P1-8 (2026-05-23) — renamed from `execute_spec` to make the
    polymorphic `execute()` the single public entry point.
    """
    # Clarify-only plan → emit clarify directly.
    if spec.clarify and not spec.steps:
        return RouterResult(
            kind="clarify",
            plan=_spec_stub_query_plan(spec),
            message=spec.clarify,
        )
    if not spec.steps:
        return RouterResult(
            kind="no_steps",
            plan=_spec_stub_query_plan(spec),
            message=spec.rationale or "no steps in PlanSpec",
        )

    try:
        ordered = _spec_mod.topological_order(spec)
    except ValueError as e:
        return RouterResult(
            kind="clarify",
            plan=_spec_stub_query_plan(spec),
            message=f"invalid plan: {e}",
        )

    # results_by_id used both for $-ref resolution AND for the final
    # `results` list — but the final list mirrors topological order so
    # the renderer can iterate sequentially.
    results_by_id: dict[str, Any] = {}
    results_ordered: list[ToolResult] = []
    events: list[StepEvent] = []
    usage = None
    if budget is not None:
        try:
            from scripts.v2.budget import BudgetUsage
            usage = BudgetUsage()
        except Exception:
            usage = None
    # Sprint 20 — Stan 2026-05-19 prod: multi-word timeline fan-out
    # (3 parallel word_freq_timeline calls, no deps) aborted on the
    # first failure, killing 2 successful candidates. Pre-compute which
    # step ids have *dependents* — if a step's data isn't needed by
    # anyone else, a failure there shouldn't abort the rest. Treat such
    # leaf failures as «soft» — keep going, collect partial results,
    # renderer reports what worked + what didn't.
    dependents: dict[str, set[str]] = {s.id: set() for s in ordered}
    by_id = {s.id: s for s in ordered}
    for s in ordered:
        deps: set[str] = set(s.needs)
        _spec_mod._walk_refs(s.args, lambda step_id, _p: deps.add(step_id))
        for d in deps:
            if d in dependents:
                dependents[d].add(s.id)

    for idx, step in enumerate(ordered):
        # Budget check BEFORE dispatching a new step — if we're already
        # over, surface immediately rather than running another heavy tool.
        if usage is not None and usage.exceeded(budget):
            events.append(StepEvent(
                kind="budget_exceeded",
                step_idx=idx,
                tool=step.tool,
                reason=f"wall_clock {usage.wall_clock_used_s:.1f}s "
                       f"exceeded budget {budget.wall_clock_s:.1f}s "
                       f"at step {idx}/{len(ordered)}",
            ))
            return RouterResult(
                kind="results",
                plan=_spec_stub_query_plan(spec),
                results=results_ordered, events=events,
                budget_exceeded=True,
            )
        # Resolve any $sN.field refs in args against prior results.
        resolved_args = _spec_mod.resolve_refs(step.args, results_by_id)
        if not isinstance(resolved_args, dict):
            resolved_args = {}
        events.append(StepEvent(kind="step_start", step_idx=idx,
                                 tool=step.tool, args=resolved_args))
        # Phase 5 chokepoint: см. execute() выше.
        result = dispatch(step.tool, resolved_args, budget=budget)
        if usage is not None:
            usage.tool_calls_used += 1
        results_ordered.append(result)
        # Make the result's data visible to later $-refs. We attach the
        # whole `data` dict (most common case) under the step id; nested
        # paths like `$s1.first_id` work via walk_path.
        results_by_id[step.id] = (
            result.data if (result and result.data is not None) else None
        )
        events.append(StepEvent(kind="step_done", step_idx=idx,
                                 tool=step.tool, result=result))
        if not result.ok and not step.optional:
            # Hard-abort ONLY when failure would cascade: there are
            # downstream steps that need this step's output. Otherwise
            # this is a leaf in the DAG — keep going so siblings get a
            # chance. Renderer will see partial results + tool errors.
            if dependents.get(step.id):
                return RouterResult(
                    kind="results",
                    plan=_spec_stub_query_plan(spec),
                    results=results_ordered, events=events,
                )
            # leaf failure → continue

    return RouterResult(
        kind="results",
        plan=_spec_stub_query_plan(spec),
        results=results_ordered, events=events,
    )


def _spec_stub_query_plan(spec: PlanSpec) -> QueryPlan:
    """Adapt a PlanSpec into a QueryPlan stub.

    Renderers + observability code currently read `result.plan.intent` /
    `result.plan.steps`. Build a placeholder QueryPlan so we don't have
    to fork the downstream code paths.
    """
    # Lazily import Entities so this module stays importable when
    # entities.py is being tested in isolation.
    from scripts.v2.planner.entities import Entities

    steps = [
        PlanStep(tool=s.tool, args=s.args, depends_on=[], inject_result_as=None,
                  optional=s.optional)
        for s in spec.steps
    ]
    qp = QueryPlan(
        intent=spec.intent_hint or "v4_llm_plan",
        entities=Entities(),
        steps=steps,
        needs_clarify=bool(spec.clarify and not spec.steps),
        clarify_question=spec.clarify if spec.clarify else None,
        explain=spec.rationale or "v4 LLM-emitted plan",
    )
    return qp


def execute_stream(plan_or_spec: PlanOrSpec, *, budget=None) -> Iterator[dict]:
    """Generator variant for SSE wiring in chat_server.

    T1 / D-P1-8 (2026-05-23) — polymorphic over QueryPlan vs PlanSpec
    like `execute()`. Internal split lives in
    `_execute_query_plan_stream` / `_execute_spec_stream`.
    """
    if isinstance(plan_or_spec, PlanSpec):
        yield from _execute_spec_stream(plan_or_spec, budget=budget)
        return
    yield from _execute_query_plan_stream(plan_or_spec, budget=budget)


def _execute_query_plan_stream(plan: QueryPlan, *, budget=None) -> Iterator[dict]:
    """v3 streaming executor — `plan: QueryPlan`. SSE event shape:
    `intent` / `plan` / `tool_call` / `tool_result` / `done`.

    Phase 5: `budget` proxied into `dispatch` like `execute()` so the
    SSE path is timeout-bounded symmetrically with the blocking path.
    """
    yield {"event": "intent", "label": plan.intent,
           "explain": plan.explain,
           "needs_clarify": plan.needs_clarify}
    if plan.needs_clarify:
        yield {"event": "clarify", "question": plan.clarify_question or ""}
        yield {"event": "done", "kind": "clarify"}
        return
    if plan.out_of_scope_reason:
        yield {"event": "out_of_scope", "reason": plan.out_of_scope_reason}
        yield {"event": "done", "kind": "out_of_scope"}
        return
    # Phase 4 — invariant application matches `execute()` so SSE
    # consumers see the same fanned-out step list.
    plan = apply_invariants(plan, budget=budget)
    yield {"event": "plan", "steps": [{"tool": s.tool, "args": s.args}
                                      for s in plan.steps]}

    results: list[ToolResult] = []
    for idx, step in enumerate(plan.steps):
        args = _inject(step.args, results, step.depends_on, step.inject_result_as)
        if args is None:
            # B121 — same skip semantics as the blocking executor.
            # Unknown event kinds are ignored by both SSE clients
            # (chat_server JS switch default / api_loop trace catch-all).
            placeholder, reason = _skip_for_empty_injection(
                plan, step, results)
            results.append(placeholder)
            yield {"event": "tool_skip", "name": step.tool,
                   "reason": reason, "step_idx": idx}
            continue
        yield {"event": "tool_call", "name": step.tool, "args": args, "step_idx": idx}
        tr = dispatch(step.tool, args, budget=budget)
        results.append(tr)
        yield {"event": "tool_result", "name": step.tool,
               "ok": tr.ok, "ms": tr.runtime_ms,
               "summary": tr.to_llm_string(max_chars=240),
               "step_idx": idx}
        if not tr.ok and not step.optional:
            yield {"event": "done", "kind": "results_partial"}
            return

    yield {"event": "done", "kind": "results"}


def _execute_spec_stream(spec: PlanSpec, *, budget=None) -> Iterator[dict]:
    """v4 streaming DAG executor — `spec: PlanSpec`. Emits the same event
    shape as `_execute_query_plan_stream` so chat_server's SSE handler
    can pipe v4 plans without forking the protocol.

    Phase 5: `budget` proxied into `dispatch` for timeout enforcement.

    T1 / D-P1-8 (2026-05-23) — renamed from `execute_spec_stream`.
    """
    yield {"event": "intent", "label": spec.intent_hint or "v4_llm_plan",
           "explain": spec.rationale,
           "needs_clarify": bool(spec.clarify and not spec.steps),
           "via": "v4_llm_planner"}
    if spec.clarify and not spec.steps:
        yield {"event": "clarify", "question": spec.clarify}
        yield {"event": "done", "kind": "clarify"}
        return
    if not spec.steps:
        yield {"event": "done", "kind": "no_steps"}
        return

    try:
        ordered = _spec_mod.topological_order(spec)
    except ValueError as e:
        yield {"event": "clarify", "question": f"invalid plan: {e}"}
        yield {"event": "done", "kind": "clarify"}
        return

    yield {"event": "plan", "steps": [
        {"id": s.id, "tool": s.tool, "args": s.args, "needs": s.needs}
        for s in ordered
    ]}

    # Sprint 20 — same soft-leaf failure semantics as execute_spec.
    dependents: dict[str, set[str]] = {s.id: set() for s in ordered}
    for s in ordered:
        deps: set[str] = set(s.needs)
        _spec_mod._walk_refs(s.args, lambda step_id, _p: deps.add(step_id))
        for d in deps:
            if d in dependents:
                dependents[d].add(s.id)

    results_by_id: dict[str, Any] = {}
    for idx, step in enumerate(ordered):
        resolved_args = _spec_mod.resolve_refs(step.args, results_by_id)
        if not isinstance(resolved_args, dict):
            resolved_args = {}
        yield {"event": "tool_call", "name": step.tool, "args": resolved_args,
               "step_idx": idx, "step_id": step.id}
        tr = dispatch(step.tool, resolved_args, budget=budget)
        results_by_id[step.id] = (
            tr.data if (tr and tr.data is not None) else None
        )
        yield {"event": "tool_result", "name": step.tool,
               "ok": tr.ok, "ms": tr.runtime_ms,
               "summary": tr.to_llm_string(max_chars=240),
               "step_idx": idx, "step_id": step.id}
        if not tr.ok and not step.optional:
            if dependents.get(step.id):
                # Cascade abort: downstream needs this output
                yield {"event": "done", "kind": "results_partial"}
                return
            # Leaf failure → keep streaming the rest

    yield {"event": "done", "kind": "results"}
