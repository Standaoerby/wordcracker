"""Plan-level invariants applied by the router.

Phase 4 (REFACTOR_BRIEF) — invariants live HERE, not inside individual
plan builders, so any new builder gets the same fan-out / timeout /
clarify-guard behavior for free. R6: «Никаких новых `_plan_*` с
копипастой fan-out / timeout / clarify». The brief lists fan-out по
авторам, применение тайм-аута и clarify-guard как инварианты роутера.

Currently exposed:

  apply_fan_out_invariant(plan, *, cap=3) -> QueryPlan
      For each `PlanStep` whose `fan_out` marker is set, clones the step
      per `plan.entities.multi_author_regex[:cap]`, swapping the author
      reference (in `scope.author` or in `author_regex` directly). Clones
      are `optional=True` and have `fan_out=None` so the invariant is
      idempotent. Closes E5 structurally — POS / etymology / emotion /
      collocates / contexts / author_vocab all share one implementation.

`apply_timeout_invariant` and `apply_clarify_guard_invariant` are
intentionally light shims today. Timeout is already enforced via
`RequestBudget` at the router level (Sprint 21+ / Phase 5 spec); the
shim is a hook point so future per-tool soft-timeout logic lands here,
not in builders. Clarify-guard wraps the copyright-OOS short-circuit
decorators used by individual builders — moved here so a builder can
opt in via plan metadata instead of re-decorating.
"""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.v2.planner.plan import PlanStep, QueryPlan


# Phase 4 — supported fan-out strategies. Centralized so the data model
# is small and explicit. Adding a new strategy means updating this set
# AND the clone helper below; nothing else in the codebase encodes the
# enumeration.
_FAN_OUT_STRATEGIES = frozenset({"scope_author", "author_regex"})


def _clone_step_for_extra(step: "PlanStep", extra_regex: str) -> "PlanStep | None":
    """Build a clone of `step` rewriting its author reference to
    `extra_regex`. Returns None if the strategy on `step.fan_out` isn't
    recognized — the caller treats that as «leave the step alone».

    Cloned step has:
      * a fresh deep-copied args dict (so mutating the clone doesn't
        leak into the original or vice versa),
      * `optional=True` (secondary authors should never hard-fail the
        plan — primary stays whatever it was),
      * `fan_out=None` (idempotency).
    """
    from scripts.v2.planner.plan import PlanStep

    if step.fan_out not in _FAN_OUT_STRATEGIES:
        return None

    new_args = deepcopy(step.args) if isinstance(step.args, dict) else {}
    if step.fan_out == "scope_author":
        scope = new_args.get("scope")
        if not isinstance(scope, dict):
            return None
        scope["author"] = extra_regex
        new_args["scope"] = scope
    elif step.fan_out == "author_regex":
        new_args["author_regex"] = extra_regex

    return PlanStep(
        tool=step.tool,
        args=new_args,
        depends_on=list(step.depends_on),
        inject_result_as=step.inject_result_as,
        optional=True,
        fan_out=None,
    )


def apply_fan_out_invariant(plan: "QueryPlan", *, cap: int = 3) -> "QueryPlan":
    """Expand any `fan_out`-marked step in `plan.steps` per
    `plan.entities.multi_author_regex[:cap]`.

    Mutates `plan.steps` in-place AND returns `plan` so callers can
    chain. Idempotent: cloned steps have `fan_out=None`, so running the
    invariant twice produces the same result.

    Skips silently when:
      * the plan has no entities object (defensive — some test stubs);
      * `entities.multi_author_regex` is empty;
      * no step carries a `fan_out` marker.
    """
    e = getattr(plan, "entities", None)
    if e is None:
        return plan
    extras = list(getattr(e, "multi_author_regex", []) or [])[:cap]
    if not extras:
        return plan

    new_steps: list = []
    expanded = False
    for step in plan.steps:
        marker = getattr(step, "fan_out", None)
        if not marker:
            new_steps.append(step)
            continue
        # Marker is consumed: emit the primary with fan_out=None so a
        # second invariant pass is a no-op. Then append clones for each
        # extra author.
        new_steps.append(_with_fan_out_cleared(step))
        for extra in extras:
            clone = _clone_step_for_extra(step, extra)
            if clone is not None:
                new_steps.append(clone)
                expanded = True

    if expanded:
        plan.steps = new_steps
    return plan


def _with_fan_out_cleared(step: "PlanStep") -> "PlanStep":
    """Return a copy of `step` with `fan_out=None`. Used to make the
    invariant idempotent — the primary step's marker is consumed."""
    from scripts.v2.planner.plan import PlanStep
    return PlanStep(
        tool=step.tool,
        args=step.args,
        depends_on=list(step.depends_on),
        inject_result_as=step.inject_result_as,
        optional=step.optional,
        fan_out=None,
    )


def apply_timeout_invariant(plan: "QueryPlan", *, budget=None) -> "QueryPlan":
    """Hook point for the request-budget timeout invariant.

    The current implementation is a passthrough: budget enforcement
    lives in `router.execute` (it polls `BudgetUsage.exceeded(budget)`
    between steps). The shim exists so future per-tool soft-timeout
    enforcement lands here (Phase 5 dispatch chokepoint) rather than
    in individual builders.
    """
    return plan


def apply_clarify_guard_invariant(plan: "QueryPlan") -> "QueryPlan":
    """Hook point for the clarify-guard invariant.

    Today the copyright / ambiguous-author guards run inside builders
    via `_with_copyright_check` / `_with_author_copyright_check`
    decorators. Phase 4 declares these as «invariants applied to every
    plan, not per-builder»; this shim is the canonical place for that
    to migrate. Builders that still decorate are unchanged — they just
    short-circuit before this runs.
    """
    return plan


def apply_invariants(plan: "QueryPlan", *, budget=None) -> "QueryPlan":
    """Apply every plan-level invariant in deterministic order. The
    router calls this exactly once at the top of `execute()` /
    `execute_stream()`. Order matters only as much as later passes may
    inspect what earlier ones produced; today they are independent.
    """
    plan = apply_clarify_guard_invariant(plan)
    plan = apply_fan_out_invariant(plan)
    plan = apply_timeout_invariant(plan, budget=budget)
    return plan
