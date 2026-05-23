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

import contextlib
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Literal

from scripts.v2.corpus_version import current_source_info
from scripts.v2.filters import FilterSpec
from scripts.v2._types import ToolError, ToolResult, now_ms_since

log = logging.getLogger("wordcracker.v2.registry")


# Phase 5 chokepoint — единый механизм тайм-аута для v1 и v2 тулзов.
# Изначально dispatch() игнорировал spec.timeout_s; v3.3.1 повесил
# SIGALRM-обёртку, но только на v2-путь, и без связи с request budget.
# T5 (2026-05-23) — `legacy_dispatch.py` удалён, обе ветки (v2 REGISTRY
# и legacy TOOL_DISPATCH из scripts.rag_query) проходят через ОДИН
# `dispatch()` ниже и единый `_signal_timeout(effective_timeout_s(...))`.
@contextlib.contextmanager
def _signal_timeout(seconds: int) -> Iterator[None]:
    """Raise TimeoutError if block doesn't finish within `seconds`.

    Signal-based (Linux only — needs SIGALRM). On Windows / non-Unix
    the context manager is a no-op so dev boxes keep working without
    enforcement; prod (Server-on-Wheels = Ubuntu) enforces."""
    if not hasattr(signal, "SIGALRM") or seconds <= 0:
        yield
        return

    def _handler(signum, frame):
        raise TimeoutError(f"tool exceeded {seconds}s timeout")

    try:
        old_handler = signal.signal(signal.SIGALRM, _handler)
    except ValueError:
        # Not in main thread → signal API unavailable; fall through.
        yield
        return
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# Дефолтный потолок per-tool — после Phase 5 это просто верхняя планка
# на случай, когда нет request-budget. Per-tool оверрайды убраны (R6/Фаза 5):
# реальный cap = min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s).
DEFAULT_TOOL_TIMEOUT_S = 60


def effective_timeout_s(spec_timeout_s: int, budget) -> int:
    """Compute effective tool timeout = min(spec ceiling, budget.remaining).

    Returns 0 (= disabled) only when both inputs say «no limit». A budget
    with remaining <= 0 returns 1 — каждый тул получает хотя бы один
    тик, чтобы корректно ответить timeout, а не зависнуть.
    """
    if budget is None:
        return spec_timeout_s
    try:
        remaining = max(0.0, budget.remaining_s())
    except Exception:
        return spec_timeout_s
    if spec_timeout_s <= 0:
        # spec=unlimited → respect budget remaining
        return max(1, int(remaining))
    return max(1, min(spec_timeout_s, int(remaining)))

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
    # v3.3.1 — default bumped 30→60. Per-tool wrappers override when
    # they legitimately need longer (e.g. author_profile composite,
    # vocab_passport). Was 30 + unenforced; now 60 + enforced via
    # signal.alarm in dispatch(). Tools that don't fit in 60s either
    # need optimization OR explicit higher timeout_s in their @tool.
    timeout_s: int = 60
    cacheable: bool = True
    output_data_schema: dict | None = None
    # Sprint 21+ (Stan B100 cache invalidation): bump this when the
    # wrapper's output SHAPE changes (new fields, schema renames). Old
    # cached results under the previous version key become unreachable
    # but harmless — they just stop being served. Avoids «I shipped a
    # fix but prod still serves stale data» entirely. Default "v1" for
    # all unchanged tools; tools mid-evolution use "v2" / "v3" etc.
    wrapper_version: str = "v1"


REGISTRY: dict[str, ToolSpec] = {}


# T5 (2026-05-23) — lazy v1 TOOL_DISPATCH loader, ранее жил в
# legacy_dispatch._LEGACY_DISPATCH_CACHE. После удаления legacy_dispatch.py
# обе ветки (v2 REGISTRY и v1 TOOL_DISPATCH) идут через `dispatch()`.
# Тестам, которые подменяют sys.modules["scripts.rag_query"], нужен
# сброс через `_reset_legacy_cache_for_tests()`.
_LEGACY_DISPATCH_CACHE: dict[str, Any] = {"dispatch": None, "loaded": False}


def _load_legacy_dispatch() -> dict | None:
    """Lazy import scripts.rag_query.TOOL_DISPATCH (merge of rag_tools +
    learning_tools v1 functions). Cached so cold import cost is paid once.
    Returns None if import fails (unit-test env w/o rag_query).
    """
    if _LEGACY_DISPATCH_CACHE["loaded"]:
        return _LEGACY_DISPATCH_CACHE["dispatch"]
    _LEGACY_DISPATCH_CACHE["loaded"] = True
    try:
        from scripts.rag_query import TOOL_DISPATCH
        _LEGACY_DISPATCH_CACHE["dispatch"] = TOOL_DISPATCH
    except ImportError as e:
        log.warning("v1 dispatch unavailable: %s", e)
        _LEGACY_DISPATCH_CACHE["dispatch"] = None
    return _LEGACY_DISPATCH_CACHE["dispatch"]


def _reset_legacy_cache_for_tests() -> None:
    """Test helper: force re-import on next dispatch() call.

    Tests that swap `sys.modules["scripts.rag_query"]` with a stub must
    call this so the cached reference doesn't shadow the new module.
    """
    _LEGACY_DISPATCH_CACHE["dispatch"] = None
    _LEGACY_DISPATCH_CACHE["loaded"] = False


def all_tool_names() -> set[str]:
    """All tool names callable via dispatch() — v2 REGISTRY ∪ v1 TOOL_DISPATCH.

    v1 dispatch is loaded lazily (same as dispatch() itself). T5 (2026-05-23)
    — moved here from legacy_dispatch so dispatch() and its name-enumeration
    helper live in the same module.
    """
    legacy = _load_legacy_dispatch() or {}
    return set(REGISTRY) | set(legacy)


def tool(
    *,
    name: str,
    category: Category,
    description: str,
    input_schema: dict,
    requires: Iterable[str] = (),
    cost: Cost = "medium",
    timeout_s: int = 60,         # v3.3.1: was 30; now enforced via signal.alarm
    cacheable: bool = True,
    output_data_schema: dict | None = None,
    wrapper_version: str = "v1",
) -> Callable:
    """Decorator that registers the wrapped function under `name`.

    The wrapped function MUST return a ToolResult. Use ToolResult.success / .fail
    helpers. Exceptions are caught by dispatch() and turned into ToolError.

    `wrapper_version`: bump when the output shape changes so old cached
    entries get bypassed automatically on deploy (no manual cache eviction).
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
            wrapper_version=wrapper_version,
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


def dispatch(name: str, args: dict | None = None, *, budget=None) -> ToolResult:
    """Invoke a registered tool by name with `args`. Always returns a ToolResult.

    `budget` — optional `RequestBudget`. When provided, the effective tool
    timeout is `min(spec.timeout_s, budget.remaining_s)`.

    T5 (2026-05-23) — единственная точка вызова тула. После удаления
    `legacy_dispatch.py` `dispatch()` сам маршрутизирует: сначала v2
    REGISTRY, при промахе — v1 TOOL_DISPATCH из scripts.rag_query. Обе
    ветки проходят через тот же `_signal_timeout(effective_timeout_s(...))`,
    так что request budget держится симметрично.

    Cache: when spec.cacheable is True, we look up (name, args) in the disk +
    in-process LRU before running the tool. Stale (older corpus_version) hits
    come back with a `stale_cache` warning attached so the renderer can flag
    them. Successful results are written back. Failures are not cached.

    Cache lookups/writes are best-effort: any cache layer error logs and falls
    through to executing the tool, so we never crash on a corrupt cache file.

    Legacy (v1) tools are not cached here — they self-cache inside their
    own implementations and have no `wrapper_version` to invalidate against.
    """
    args = args or {}
    spec = REGISTRY.get(name)
    if spec is None:
        return _dispatch_legacy(name, args, budget=budget)
    args = _coerce_args(spec, args)

    if spec.cacheable:
        try:
            from scripts.v2 import cache as _cache
            cached = _cache.cache_get(name, args,
                                       wrapper_version=spec.wrapper_version)
            if cached is not None:
                return cached
        except Exception as e:
            log.warning("cache_get failed for %s: %s", name, e)

    eff_timeout = effective_timeout_s(spec.timeout_s, budget)
    t0 = time.perf_counter()
    try:
        # Phase 5 chokepoint: effective timeout = min(spec, budget.remaining).
        with _signal_timeout(eff_timeout):
            result = spec.fn(**args)
    except TimeoutError as e:
        log.warning("tool %s timed out after %ds (spec=%ds, budget=%s)",
                    name, eff_timeout, spec.timeout_s,
                    f"{budget.remaining_s():.1f}s" if budget is not None else "none")
        result = ToolResult.fail(
            tool=name, err_type="timeout",
            message=str(e),
            details={"timeout_s": eff_timeout,
                     "spec_timeout_s": spec.timeout_s,
                     "budget_bound": budget is not None,
                     "wall_clock_ms": now_ms_since(t0)},
            retryable=True,
        )
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

    if spec.cacheable and result.ok:
        try:
            from scripts.v2 import cache as _cache
            _cache.cache_put(name, args, result,
                             wrapper_version=spec.wrapper_version)
        except Exception as e:
            log.warning("cache_put failed for %s: %s", name, e)
    return result


def _dispatch_legacy(name: str, args: dict, *, budget=None) -> ToolResult:
    """Run a v1 (pre-@tool) tool through the same timeout/budget chokepoint.

    Called by `dispatch()` when `name` is not in v2 REGISTRY. Effective
    timeout = `min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s)` — v1 tools
    have no per-tool spec, so they use the default ceiling. The
    `_signal_timeout` wrapper enforces it on Linux (SIGALRM); on Windows
    it's a no-op and we rely on the wall-clock budget check in the router.
    """
    legacy = _load_legacy_dispatch()
    if legacy is None or name not in legacy:
        return ToolResult.fail(
            tool=name, err_type="not_found",
            message=f"unknown tool '{name}' (not in v2 registry, v1 not loaded)"
                    if legacy is None
                    else f"unknown tool '{name}'",
            details={"v2_available": sorted(REGISTRY),
                     "v1_loaded": legacy is not None},
        )

    fn = legacy[name]
    eff_timeout = effective_timeout_s(DEFAULT_TOOL_TIMEOUT_S, budget)
    t0 = time.perf_counter()
    try:
        with _signal_timeout(eff_timeout):
            raw = fn(**args)
    except TimeoutError as e:
        log.warning("legacy tool %s timed out after %ds (budget=%s)",
                    name, eff_timeout,
                    f"{budget.remaining_s():.1f}s" if budget is not None else "none")
        return ToolResult.fail(
            tool=name, err_type="timeout", message=str(e),
            details={"timeout_s": eff_timeout,
                     "budget_bound": budget is not None,
                     "wall_clock_ms": now_ms_since(t0)},
            retryable=True, query=args,
        )
    except TypeError as e:
        return ToolResult.fail(
            tool=name, err_type="invalid_args", message=str(e), query=args,
        )
    except Exception as e:
        log.exception("legacy tool %s crashed", name)
        return ToolResult.fail(
            tool=name, err_type="internal", message=str(e), query=args,
        )

    return ToolResult.from_legacy(
        tool=name, raw=raw,
        runtime_ms=now_ms_since(t0),
        source_info=current_source_info(),
        query=args,
    )


def _coerce_args(spec: ToolSpec, args: dict) -> dict:
    """Cast `filter` dicts to FilterSpec where the schema expects it.

    Keeps the LLM-facing schema as plain JSON while tools internally work with
    typed FilterSpec instances.

    Note: we deliberately do NOT coerce `scope` here. v2 wrappers thin-wrap v1
    tools that still expect plain dict scope, so silently converting their args
    to a FilterSpec instance breaks them (no `.get()` method, etc). Only the
    explicit `filter` key — which is v2-native by convention — gets the cast.
    """
    out = dict(args)
    for k, v in args.items():
        if k == "filter" and isinstance(v, dict):
            out[k] = FilterSpec(**{kk: vv for kk, vv in v.items()
                                   if kk in FilterSpec.__dataclass_fields__})
    return out


def _safe_args_repr(args: dict) -> dict:
    """Make sure error details are JSON-serializable."""
    try:
        json.dumps(args, default=str)
        return args
    except Exception:
        return {k: type(v).__name__ for k, v in args.items()}
