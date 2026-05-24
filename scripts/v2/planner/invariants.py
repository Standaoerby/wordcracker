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

    W-5 honesty contract (2026-05-24): when the user named ≥2 authors
    but none of the steps carries a `fan_out` marker AND the plan
    isn't already a multi-object compare flavour (compare_authors /
    author_compare with primary+secondary in distinct args), stamp a
    render note so the renderer DISCLOSES that secondary authors were
    dropped — instead of silently replying for the primary only.
    `_plan_top_authors`-shaped queries that legitimately don't fit a
    multi-author shape («топ слов у По и Лавкрафта» — top_ngrams takes
    one author) are the canonical case.
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
        _stamp_multi_author_render_note(plan, extras)
    else:
        _maybe_stamp_dropped_authors_note(plan, extras)
    return plan


# W-5 (2026-05-24) — intents that already encode multi-object shape
# in their own args (compare_authors takes author1_regex + author2_regex;
# country_compare emits two top_authors_by_country steps; book_compare
# fans out per book). For these the renderer already sees both objects
# and the «dropped authors» disclosure would be redundant / wrong.
_MULTI_OBJECT_NATIVE_INTENTS = frozenset({
    "author_compare",
    "country_compare",
    "composite_compare",
    "book_compare",
    "book_readability_compare",
    "clarify",
    "out_of_scope",
    "introduction",
})


def _maybe_stamp_dropped_authors_note(
    plan: "QueryPlan", extras: list[str],
) -> None:
    """W-5 (2026-05-24) — honesty fallback for queries where the user
    named multiple authors but the chosen intent can't fan out.

    Without this, «топ слов у По и Лавкрафта» silently answers about
    only Poe — the plan has a single `top_ngrams_by_author` step that
    accepts one `author_regex`, no `fan_out` marker. The composite isn't
    supported for this intent, but the secondary author was visible to
    the extractor, so the honest reply is to disclose («показал только
    первого; для сравнения двух — спроси «сравни X и Y»»).
    """
    if plan.intent in _MULTI_OBJECT_NATIVE_INTENTS:
        return
    if not extras:
        return
    primary = getattr(plan.entities, "author_regex", None) or "(primary)"
    others = ", ".join(extras)
    existing = " ".join(plan.render_notes or []).lower()
    if "не поддерж" in existing or "drop" in existing:
        return
    plan.render_notes = list(plan.render_notes or []) + [
        f"Пользователь упомянул нескольких авторов ({primary} + {others}), "
        f"но интент «{plan.intent}» не поддерживает composite-вывод по "
        "нескольким авторам — план запросил данные только по primary. "
        "В ответе ЧЕСТНО скажи: «показал только первого ({primary}); "
        "для сравнения двух — спроси «сравни X и Y» или «характерные "
        "слова X и Y»». НЕ молчи про второго."
    ]


def apply_dropped_books_invariant(plan: "QueryPlan") -> "QueryPlan":
    """W-5 (2026-05-24) — sibling of fan-out: stamp a render note when
    the user named ≥2 books but the plan kept only one (no per-book
    fan-out for this intent).

    «архаизмы в Dracula и Frankenstein», «эмоции в Dracula и
    Frankenstein», «уровень сложности Dracula и Frankenstein» — each
    of these has a single-book tool path (book_archaic_words,
    book_emotion_profile, book_readability) and silently drops the
    second book. The fix at builder level is to add multi-book fan-out
    to each (like book_compare / book_readability_compare already do),
    but until then this disclosure ensures the renderer doesn't reply
    «here's Dracula» without telling the user Frankenstein was dropped.
    """
    e = getattr(plan, "entities", None)
    if e is None:
        return plan
    extras_ids = list(getattr(e, "multi_book_ids", []) or [])
    extras_titles = list(getattr(e, "multi_book_titles", []) or [])
    extras_count = sum(1 for v in extras_ids if v) + sum(
        1 for t in extras_titles if t)
    if extras_count == 0:
        return plan
    if plan.intent in _MULTI_OBJECT_NATIVE_INTENTS:
        return plan
    # If a step already mentions a fan-out-style multi-book chain, the
    # builder is handling it — don't double-up the disclosure.
    book_args_count = sum(
        1 for s in plan.steps
        if isinstance(s.args, dict) and (
            "pg_id" in s.args or "title" in s.args
        )
    )
    if book_args_count >= 2:
        return plan
    primary = getattr(e, "book_id", None) or getattr(e, "book_title", None) or "(primary)"
    others = ", ".join(
        v for v in (extras_ids + extras_titles) if v
    ) or "(secondary)"
    existing = " ".join(plan.render_notes or []).lower()
    if "не поддерж" in existing or "несколько книг" in existing:
        return plan
    plan.render_notes = list(plan.render_notes or []) + [
        f"Пользователь упомянул несколько книг ({primary} + {others}), "
        f"но интент «{plan.intent}» работает только с одной книгой за раз "
        "— план запросил данные только по primary. В ответе ЧЕСТНО скажи: "
        f"«показал только {primary}; для сравнения двух — спроси «сравни "
        "X и Y» или «что сложнее X или Y»». НЕ молчи про вторую книгу."
    ]
    return plan


def _stamp_multi_author_render_note(
    plan: "QueryPlan", extras: list[str],
) -> None:
    """W-5 (2026-05-24) — append a composite render note so the LLM
    renderer doesn't silently collapse the secondary authors.

    Without this, the plan fans out to N tool calls but the renderer
    sees two near-identical result sets and surfaces only the first
    author («слова страха у По и Лавкрафта одновременно» → ответ про
    одного По). The fan-out invariant is the canonical place for this
    instruction because it's also the canonical place that knows the
    user named multiple authors — every plan that goes through fan-out
    needs the same renderer guarantee.

    Idempotent: skipped when an existing render_notes entry already
    mentions «per-author» / «обоих авторов».
    """
    primary = getattr(plan.entities, "author_regex", None) or "(primary)"
    others = ", ".join(extras) or "(none)"
    existing = " ".join(plan.render_notes or []).lower()
    if "per-author" in existing or "по автору" in existing:
        return
    plan.render_notes = list(plan.render_notes or []) + [
        "Это multi-author запрос — в плане ≥2 шага, по одному на "
        f"автора ({primary} + {others}). В ответе ОБЯЗАТЕЛЬНО покажи "
        "результат ПО АВТОРУ (per-author): либо отдельная таблица/секция "
        "на каждого, либо одна таблица с автором как ключом (колонкой "
        "или группой строк). НЕ сворачивай к одному автору, НЕ предлагай "
        "«уточнить про второго» — данные уже есть. Если для какого-то "
        "автора шаг упал/пуст — честно скажи об этом, не молчи."
    ]


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
    plan = apply_dropped_books_invariant(plan)
    plan = apply_timeout_invariant(plan, budget=budget)
    return plan
