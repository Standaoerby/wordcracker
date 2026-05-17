"""Legacy dispatch — call any v1 tool through the v2 envelope.

Until every v1 tool is migrated to the @tool decorator, the router needs a
uniform way to invoke either. This module exposes:

    dispatch_any(name, args) -> ToolResult

which:
  1. First tries v2 REGISTRY (the new tools).
  2. Falls back to v1 TOOL_DISPATCH from scripts/rag_query.py.
  3. Wraps v1 returns in ToolResult.from_legacy().

This lets `plan.py` reference all 32 tools by name without caring whether
they've been ported yet.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from scripts.v2.corpus_version import current_source_info
from scripts.v2.tool_registry import REGISTRY, dispatch as v2_dispatch
from scripts.v2._types import ToolResult, now_ms_since

log = logging.getLogger("wordcracker.v2.legacy_dispatch")

_LEGACY_DISPATCH_CACHE: dict[str, Any] = {"dispatch": None, "loaded": False}


def _load_legacy_dispatch() -> dict | None:
    if _LEGACY_DISPATCH_CACHE["loaded"]:
        return _LEGACY_DISPATCH_CACHE["dispatch"]
    _LEGACY_DISPATCH_CACHE["loaded"] = True
    try:
        # rag_query.TOOL_DISPATCH already merges rag_tools + learning_tools.
        from scripts.rag_query import TOOL_DISPATCH
        _LEGACY_DISPATCH_CACHE["dispatch"] = TOOL_DISPATCH
    except ImportError as e:
        log.warning("v1 dispatch unavailable: %s", e)
        _LEGACY_DISPATCH_CACHE["dispatch"] = None
    return _LEGACY_DISPATCH_CACHE["dispatch"]


def dispatch_any(name: str, args: dict | None = None) -> ToolResult:
    """Dispatch name through v2 first, fall back to v1."""
    args = args or {}
    if name in REGISTRY:
        return v2_dispatch(name, args)

    legacy = _load_legacy_dispatch()
    if legacy is None or name not in legacy:
        return ToolResult.fail(
            tool=name, err_type="not_found",
            message=f"unknown tool '{name}' (not in v2 registry, v1 not loaded)",
            details={"v2_available": sorted(REGISTRY),
                     "v1_loaded": legacy is not None},
        )

    fn = legacy[name]
    t0 = time.perf_counter()
    try:
        raw = fn(**args)
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


def all_tool_names() -> set[str]:
    """Names available across v2 + v1 (loaded lazily)."""
    legacy = _load_legacy_dispatch() or {}
    return set(REGISTRY) | set(legacy)
