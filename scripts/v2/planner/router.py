"""Tool Router — execute a QueryPlan step-by-step, deterministic.

Contract: docs/v2/PLANNER.md §5.

The router doesn't talk to the LLM. It just runs the steps in order, threads
prior `ToolResult.data` into later args, and stops on hard failures (unless
the step is `optional`). This is what kills the "narrates plan, never calls
tool" failure mode from v1.1.7 — there's no LLM in the loop here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

from scripts.v2.legacy_dispatch import dispatch_any
from scripts.v2.planner.plan import PlanStep, QueryPlan
from scripts.v2.types import ToolResult


@dataclass
class StepEvent:
    kind: Literal["step_start", "step_done", "step_skip"]
    step_idx: int
    tool: str
    args: dict | None = None
    result: ToolResult | None = None
    reason: str | None = None  # for step_skip


@dataclass
class RouterResult:
    kind: Literal["clarify", "out_of_scope", "results", "no_steps"]
    plan: QueryPlan
    results: list[ToolResult] = field(default_factory=list)
    message: str | None = None  # clarify question or out_of_scope reason
    events: list[StepEvent] = field(default_factory=list)


def _inject(args: dict, prior_results: list[ToolResult],
            depends_on: list[int], inject_as: str | None) -> dict:
    """Thread output from a previous step into this step's args.

    Heuristic: when `inject_as == "pg_id"`, look up `data["first_id"]` on the
    most recent dependency. Extendable: add more named injections as we use
    them in plan templates."""
    if not depends_on or inject_as is None:
        return args
    src = prior_results[depends_on[-1]]
    out = dict(args)
    if not src.ok or src.data is None:
        return out  # router will detect & error out below
    if inject_as == "pg_id":
        first_id = src.data.get("first_id") if isinstance(src.data, dict) else None
        if first_id:
            out["pg_id"] = first_id
    elif inject_as == "scope":
        # use the resolved book id as a scope dict
        first_id = src.data.get("first_id") if isinstance(src.data, dict) else None
        if first_id:
            out["scope"] = {"book": first_id}
    elif inject_as in src.data if isinstance(src.data, dict) else False:
        out[inject_as] = src.data[inject_as]
    return out


def execute(plan: QueryPlan) -> RouterResult:
    """Run a plan to completion. Always returns a RouterResult."""
    if plan.needs_clarify:
        return RouterResult(kind="clarify", plan=plan,
                            message=plan.clarify_question or "")
    if plan.out_of_scope_reason:
        return RouterResult(kind="out_of_scope", plan=plan,
                            message=plan.out_of_scope_reason)
    if not plan.steps:
        return RouterResult(kind="no_steps", plan=plan,
                            message=plan.explain or "no tools to call")

    results: list[ToolResult] = []
    events: list[StepEvent] = []

    for idx, step in enumerate(plan.steps):
        args = _inject(step.args, results, step.depends_on, step.inject_result_as)
        events.append(StepEvent(kind="step_start", step_idx=idx,
                                tool=step.tool, args=args))
        result = dispatch_any(step.tool, args)
        results.append(result)
        events.append(StepEvent(kind="step_done", step_idx=idx,
                                tool=step.tool, result=result))
        if not result.ok and not step.optional:
            # Hard fail: stop here. Renderer will surface the error.
            return RouterResult(kind="results", plan=plan,
                                results=results, events=events)

    return RouterResult(kind="results", plan=plan,
                        results=results, events=events)


def execute_stream(plan: QueryPlan) -> Iterator[dict]:
    """Generator variant for SSE wiring in chat_server.

    Yields dicts with the same shape v1 emits, plus the v2-specific
    `intent`/`plan` events at the start."""
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
    yield {"event": "plan", "steps": [{"tool": s.tool, "args": s.args}
                                      for s in plan.steps]}

    results: list[ToolResult] = []
    for idx, step in enumerate(plan.steps):
        args = _inject(step.args, results, step.depends_on, step.inject_result_as)
        yield {"event": "tool_call", "name": step.tool, "args": args, "step_idx": idx}
        tr = dispatch_any(step.tool, args)
        results.append(tr)
        yield {"event": "tool_result", "name": step.tool,
               "ok": tr.ok, "ms": tr.runtime_ms,
               "summary": tr.to_llm_string(max_chars=240),
               "step_idx": idx}
        if not tr.ok and not step.optional:
            yield {"event": "done", "kind": "results_partial"}
            return

    yield {"event": "done", "kind": "results"}
