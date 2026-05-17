"""Tool Registry — single source of truth for v2 tools.

Contract: docs/v2/SPECS.md §3.

Tools self-register via @tool decorator:

    @tool(name="corpus_overview", category="corpus_meta",
          description="…", requires=[], cost="cheap",
          input_schema={"type": "object", "properties": {}, "required": []})
    def corpus_overview() -> ToolResult: ...

Lookup / dispatch:

    spec = REGISTRY["corpus_overview"]
    result = dispatch("corpus_overview", {})

OpenAI/Ollama schema for the LLM is built from the registry:

    tools_spec = build_tools_spec()
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from scripts.v2.corpus_version import current_source_info
from scripts.v2.filters import FilterSpec
from scripts.v2.types import ToolError, ToolResult, now_ms_since

log = logging.getLogger("wordcracker.v2.registry")

Category = Literal[
    "search", "statistics", "authors", "books",
    "words", "learning", "emotion", "corpus_meta",
]
Cost = Literal["cheap", "medium", "heavy"]


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., ToolResult]
    category: Category
    description: str
    input_schema: dict
    requires: list[str] = field(default_factory=list)
    cost: Cost = "medium"
    timeout_s: int = 30
    cacheable: bool = True
    output_data_schema: dict | None = None


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    category: Category,
    description: str,
    input_schema: dict,
    requires: Iterable[str] = (),
    cost: Cost = "medium",
    timeout_s: int = 30,
    cacheable: bool = True,
    output_data_schema: dict | None = None,
) -> Callable:
    """Decorator that registers the wrapped function under `name`.

    The wrapped function MUST return a ToolResult. Use ToolResult.success / .fail
    helpers. Exceptions are caught by dispatch() and turned into ToolError.
    """
    def decorate(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        if name in REGISTRY:
            raise ValueError(f"tool '{name}' already registered")
        REGISTRY[name] = ToolSpec(
            name=name,
            fn=fn,
            category=category,
            description=description,
            input_schema=input_schema,
            requires=list(requires),
            cost=cost,
            timeout_s=timeout_s,
            cacheable=cacheable,
            output_data_schema=output_data_schema,
        )
        return fn
    return decorate


def build_tools_spec(category_filter: list[str] | None = None) -> list[dict]:
    """Emit OpenAI/Ollama function-calling schema for the LLM."""
    out = []
    for spec in REGISTRY.values():
        if category_filter and spec.category not in category_filter:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        })
    return out


def dispatch(name: str, args: dict | None = None) -> ToolResult:
    """Invoke a registered tool by name with `args`. Always returns a ToolResult.

    No cache layer here yet (Sprint 1 task 1.x covers that — added later under
    cache.py). For now we just time and error-wrap."""
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult.fail(
            tool=name, err_type="not_found",
            message=f"unknown tool '{name}'",
            details={"available": sorted(REGISTRY)},
        )
    args = _coerce_args(spec, args or {})
    t0 = time.perf_counter()
    try:
        result = spec.fn(**args)
    except TypeError as e:
        result = ToolResult.fail(
            tool=name, err_type="invalid_args",
            message=str(e), details={"got": _safe_args_repr(args)},
        )
    except Exception as e:
        log.exception("tool %s crashed", name)
        result = ToolResult.fail(
            tool=name, err_type="internal",
            message=str(e), details={"exc_type": type(e).__name__},
        )
    if not isinstance(result, ToolResult):
        # Soft contract violation — wrap raw return so we never crash the router.
        result = ToolResult.from_legacy(
            tool=name, raw=result,
            runtime_ms=now_ms_since(t0),
            source_info=current_source_info(),
        )
    result.runtime_ms = now_ms_since(t0)
    if result.source_info is None:
        result.source_info = current_source_info()
    if not result.tool:
        result.tool = name
    return result


def _coerce_args(spec: ToolSpec, args: dict) -> dict:
    """Cast `filter` dicts to FilterSpec where the schema expects it.

    Keeps the LLM-facing schema as plain JSON while tools internally work with
    typed FilterSpec instances."""
    props = (spec.input_schema or {}).get("properties", {})
    out = dict(args)
    for k, v in args.items():
        if k == "filter" and isinstance(v, dict):
            out[k] = FilterSpec(**{kk: vv for kk, vv in v.items()
                                   if kk in FilterSpec.__dataclass_fields__})
        # `scope` legacy: accept v1 shape (dict/str), coerce to FilterSpec
        if k == "scope" and not isinstance(v, FilterSpec):
            try:
                out[k] = FilterSpec.from_legacy_scope(v)
            except ValueError:
                pass
    return out


def _safe_args_repr(args: dict) -> dict:
    """Make sure error details are JSON-serializable."""
    try:
        json.dumps(args, default=str)
        return args
    except Exception:
        return {k: type(v).__name__ for k, v in args.items()}
