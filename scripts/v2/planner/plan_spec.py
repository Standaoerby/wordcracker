"""PlanSpec — v4 typed plan with DAG dependencies.

This is the data type the v4 LLM planner emits and the router executes.
It's deliberately a separate module from the v3 `plan.py` so the
rules-based fast path stays untouched while we roll out v4 behind a
feature flag.

Forward-compat contract:
    - PlanSpec is JSON-serializable both ways (LLM → JSON → PlanSpec
      and PlanSpec → JSON → router events).
    - Step args support `$sN.field[.subfield]` references — they're
      resolved by the router at execution time from prior step results.
    - Tool existence + arg shape are validated BEFORE the router runs
      anything. Invalid plans bounce to clarify instead of leaking
      half-executed state to the user.

The dataclasses are intentionally separate from `plan.py:QueryPlan`
because v4 plans express dependency DAGs (not just linear chains)
and because the validation surface is richer. The router gets a
`bridge_to_query_plan` helper so it can execute v4 plans on the
existing event/SSE infrastructure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


_STEP_ID_RE = re.compile(r"^s\d+$")
_REF_RE = re.compile(r"^\$(s\d+)(?:\.([A-Za-z_][\w.]*))?$")


# ---------- data types ----------


@dataclass
class PlanStepSpec:
    """One node in the DAG.

    `args` may contain `$sN.field` strings — the router replaces them
    with the actual value from step sN's `ToolResult.data` at run time.
    Nested references like `$s1.user.name` walk the dict.

    `needs` declares which step ids this step depends on. The router
    uses this for topological ordering; circular deps fail validation.

    `optional=True` means a failure of this step does not abort the
    whole plan (renderer still gets a partial result set).
    """
    id: str
    tool: str
    args: dict = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)
    optional: bool = False
    rationale: str = ""


@dataclass
class PlanSpec:
    """A complete LLM-emitted plan. JSON-roundtrippable."""
    intent_hint: str = ""
    rationale: str = ""
    steps: list[PlanStepSpec] = field(default_factory=list)
    render_hint: Optional[str] = None
    expected_cost: Literal["cheap", "medium", "heavy"] = "medium"
    # When the LLM cannot construct a valid plan, it returns clarify
    # instead. We store it on PlanSpec so callers can treat clarify-style
    # responses uniformly (rules path also emits clarify).
    clarify: Optional[str] = None
    # Optional: structured next-step suggestions for the renderer
    next_steps: list[str] = field(default_factory=list)


@dataclass
class ValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    step_id: Optional[str] = None


@dataclass
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


# ---------- JSON (de)serialization ----------


def from_json(payload: dict) -> PlanSpec:
    """Build a PlanSpec from the JSON object the LLM emits.

    Tolerant to a few format quirks (`tool_name` vs `tool`, `step_id`
    vs `id`) so we don't reject minor prompt-following lapses. The
    validator gets the final word on whether the plan is executable.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"PlanSpec payload must be a dict, got {type(payload).__name__}")

    if "clarify" in payload and not payload.get("steps"):
        return PlanSpec(clarify=str(payload["clarify"]).strip() or None)

    steps_raw = payload.get("steps") or []
    steps: list[PlanStepSpec] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            continue
        step_id = str(s.get("id") or s.get("step_id") or f"s{i+1}")
        tool = str(s.get("tool") or s.get("tool_name") or "").strip()
        args = s.get("args") or s.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        needs = s.get("needs") or s.get("depends_on") or []
        if not isinstance(needs, list):
            needs = []
        needs = [str(n) for n in needs]
        steps.append(PlanStepSpec(
            id=step_id, tool=tool, args=args, needs=needs,
            optional=bool(s.get("optional", False)),
            rationale=str(s.get("rationale") or "").strip(),
        ))

    return PlanSpec(
        intent_hint=str(payload.get("intent_hint") or payload.get("intent") or "").strip(),
        rationale=str(payload.get("rationale") or "").strip(),
        steps=steps,
        render_hint=(str(payload["render_hint"]).strip()
                     if payload.get("render_hint") else None),
        expected_cost=payload.get("expected_cost") or "medium",
        clarify=(str(payload["clarify"]).strip()
                 if payload.get("clarify") else None),
        next_steps=[str(x) for x in (payload.get("next_steps") or [])],
    )


def to_json(plan: PlanSpec) -> dict:
    return {
        "intent_hint": plan.intent_hint,
        "rationale": plan.rationale,
        "steps": [
            {"id": s.id, "tool": s.tool, "args": s.args,
             "needs": s.needs, "optional": s.optional,
             "rationale": s.rationale}
            for s in plan.steps
        ],
        "render_hint": plan.render_hint,
        "expected_cost": plan.expected_cost,
        "clarify": plan.clarify,
        "next_steps": list(plan.next_steps),
    }


# ---------- reference resolution ----------


def parse_ref(val: Any) -> Optional[tuple[str, Optional[str]]]:
    """If `val` is a `$sN[.field.path]` reference, return (step_id, path).

    Path may be None (whole result) or a dot-separated walk into
    ToolResult.data.
    """
    if not isinstance(val, str):
        return None
    m = _REF_RE.match(val)
    if not m:
        return None
    return m.group(1), m.group(2)


def walk_path(obj: Any, path: Optional[str]) -> Any:
    """Resolve `path` against `obj`. Path == None returns obj itself.

    Supports dot-walking dict keys and integer-indexed list access:
        path="top.0.word" + obj={"top":[{"word":"..."}]}  →  word
    """
    if not path:
        return obj
    cur = obj
    for tok in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(tok)
        elif isinstance(cur, list):
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                return None
        else:
            try:
                cur = getattr(cur, tok)
            except AttributeError:
                return None
    return cur


def resolve_refs(args: Any, results_by_id: dict[str, Any]) -> Any:
    """Recursively resolve `$sN[.field]` references inside an args structure.

    `results_by_id` maps step id → the data object to look refs into
    (typically ToolResult.data, but the router decides exact shape).
    Unresolvable refs become None — the validator catches missing-data
    cases before execution.
    """
    if isinstance(args, list):
        return [resolve_refs(x, results_by_id) for x in args]
    if isinstance(args, dict):
        return {k: resolve_refs(v, results_by_id) for k, v in args.items()}
    ref = parse_ref(args)
    if ref is None:
        return args
    step_id, path = ref
    src = results_by_id.get(step_id)
    return walk_path(src, path)


# ---------- validator ----------


def validate(plan: PlanSpec, *,
             registry: Optional[dict] = None,
             max_steps: int = 12,
             max_heavy: int = 6) -> ValidationReport:
    """Validate a PlanSpec against the tool registry.

    Checks performed:
      - clarify-only plans pass with no other constraints
      - step ids unique, match /^s\\d+$/
      - tool names exist in REGISTRY
      - required input_schema fields are present (or come from a $ref)
      - `needs` references existing earlier step ids
      - no cycles
      - $sN.field references point at existing step ids
      - step count budget, heavy-tool budget

    Returns ValidationReport with ok=False iff any `error`-severity
    issue is recorded. Warnings don't block execution but flow into
    observability.
    """
    issues: list[ValidationIssue] = []

    # Clarify-only is a valid terminal state.
    if plan.clarify and not plan.steps:
        return ValidationReport(ok=True, issues=[])

    if not plan.steps:
        issues.append(ValidationIssue(
            "error", "empty_plan",
            "PlanSpec has no steps and no clarify message",
        ))
        return ValidationReport(ok=False, issues=issues)

    if len(plan.steps) > max_steps:
        issues.append(ValidationIssue(
            "error", "too_many_steps",
            f"plan has {len(plan.steps)} steps > {max_steps} max",
        ))

    # Lazy registry import so this module doesn't force tool side-effects.
    if registry is None:
        try:
            from scripts.v2.tool_registry import REGISTRY as _R
            registry = _R
        except Exception:
            registry = {}

    ids_seen: set[str] = set()
    heavy_count = 0

    for s in plan.steps:
        # id shape
        if not s.id or not _STEP_ID_RE.match(s.id):
            issues.append(ValidationIssue(
                "error", "bad_step_id",
                f"step id {s.id!r} must match /^s\\d+$/",
                step_id=s.id,
            ))
        if s.id in ids_seen:
            issues.append(ValidationIssue(
                "error", "duplicate_step_id",
                f"step id {s.id!r} used more than once",
                step_id=s.id,
            ))
        ids_seen.add(s.id)

        # tool existence
        spec = registry.get(s.tool) if isinstance(registry, dict) else None
        if spec is None:
            issues.append(ValidationIssue(
                "error", "unknown_tool",
                f"tool {s.tool!r} not in registry",
                step_id=s.id,
            ))
            continue

        cost = getattr(spec, "cost", "medium")
        if cost == "heavy":
            heavy_count += 1

        # arg presence vs input_schema
        schema = getattr(spec, "input_schema", None) or {}
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        for req in required:
            if req not in s.args:
                issues.append(ValidationIssue(
                    "error", "missing_required_arg",
                    f"step {s.id}: tool {s.tool!r} requires arg {req!r}",
                    step_id=s.id,
                ))
        # Any extra keys that aren't in the schema get a warning
        for key in s.args:
            if properties and key not in properties and not key.startswith("_"):
                issues.append(ValidationIssue(
                    "warning", "unknown_arg",
                    f"step {s.id}: tool {s.tool!r} got arg {key!r} "
                    f"not in input_schema.properties",
                    step_id=s.id,
                ))

        # needs references valid earlier steps
        for n in s.needs:
            if n == s.id:
                issues.append(ValidationIssue(
                    "error", "self_dependency",
                    f"step {s.id} declares itself in needs",
                    step_id=s.id,
                ))
            elif n not in {ss.id for ss in plan.steps}:
                issues.append(ValidationIssue(
                    "error", "unknown_dep",
                    f"step {s.id} needs unknown step {n!r}",
                    step_id=s.id,
                ))

        # $sN.field references — check that the target id exists
        _walk_refs(s.args, lambda step_id, _path: (
            None if step_id in {ss.id for ss in plan.steps} else
            issues.append(ValidationIssue(
                "error", "ref_unknown_step",
                f"step {s.id} args ref {step_id!r} not declared",
                step_id=s.id,
            ))
        ))

    # cycle check via topo sort
    if not _is_dag(plan.steps):
        issues.append(ValidationIssue(
            "error", "cycle",
            "needs/$-ref dependencies form a cycle",
        ))

    if heavy_count > max_heavy:
        issues.append(ValidationIssue(
            "error", "too_many_heavy",
            f"plan has {heavy_count} heavy tools > {max_heavy} max",
        ))

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues)


def _walk_refs(node: Any, fn) -> None:
    if isinstance(node, list):
        for x in node:
            _walk_refs(x, fn)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_refs(v, fn)
    else:
        ref = parse_ref(node)
        if ref:
            fn(ref[0], ref[1])


def _is_dag(steps: list[PlanStepSpec]) -> bool:
    """Topological sort by both `needs` and discovered $-ref deps.

    Returns True iff a complete ordering exists.
    """
    by_id = {s.id: s for s in steps}
    deps: dict[str, set[str]] = {s.id: set(s.needs) for s in steps}
    for s in steps:
        # discovered refs
        _walk_refs(s.args, lambda step_id, _p: deps[s.id].add(step_id))
    # filter to known ids only (unknown ids are reported elsewhere)
    deps = {k: {x for x in v if x in by_id} for k, v in deps.items()}
    visited: dict[str, int] = {}  # 0=temp, 1=permanent
    stack: list[str] = []

    def dfs(node: str) -> bool:
        state = visited.get(node, -1)
        if state == 0:  # cycle
            return False
        if state == 1:
            return True
        visited[node] = 0
        for nxt in deps.get(node, ()):
            if not dfs(nxt):
                return False
        visited[node] = 1
        stack.append(node)
        return True

    for s in steps:
        if visited.get(s.id, -1) != 1:
            if not dfs(s.id):
                return False
    return True


def topological_order(plan: PlanSpec) -> list[PlanStepSpec]:
    """Return steps in dependency-respecting order. Raises ValueError on cycle.

    Combines explicit `needs` with discovered `$sN.field` refs in args.
    """
    by_id = {s.id: s for s in plan.steps}
    deps: dict[str, set[str]] = {s.id: set(s.needs) for s in plan.steps}
    for s in plan.steps:
        _walk_refs(s.args, lambda step_id, _p: deps[s.id].add(step_id))

    visited: dict[str, int] = {}
    order: list[PlanStepSpec] = []

    def dfs(node: str) -> None:
        state = visited.get(node, -1)
        if state == 0:
            raise ValueError(f"cycle detected at {node!r}")
        if state == 1:
            return
        visited[node] = 0
        for nxt in deps.get(node, ()):
            if nxt in by_id:
                dfs(nxt)
        visited[node] = 1
        order.append(by_id[node])

    for s in plan.steps:
        if visited.get(s.id, -1) != 1:
            dfs(s.id)
    return order


__all__ = [
    "PlanSpec",
    "PlanStepSpec",
    "ValidationIssue",
    "ValidationReport",
    "from_json",
    "to_json",
    "parse_ref",
    "walk_path",
    "resolve_refs",
    "validate",
    "topological_order",
]
