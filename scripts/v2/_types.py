"""v2 ToolResult envelope + adjacent types.

Contract: docs/v2/SPECS.md §1.

Design notes:
- dataclasses, not pydantic — no extra dep, plain stdlib.
- `to_dict()` drops None/empty fields to keep tool messages compact.
- `to_llm_string()` returns abbreviated JSON suitable for the assistant tool-role
  content (drops source_info, runtime_ms — planner logs these separately).
- `from_legacy()` wraps pre-v2 tool returns so the v1 dispatcher can keep
  working while tools migrate one at a time.

v5 Phase 0 extension ([[architecture_refactor_v5_plan]] §P1):
- Optional `view: RenderableView | None` — declarative render-schema.
  Phase 0: tools may emit; renderer ignores. Phase 3: renderer uses it
  as the single source of truth, eliminating fabrication.
- Optional `data_validity: DataValidity | None` — semantic-level signal
  beyond ok/fail. Golden tests assert validity; renderer surfaces
  EMPTY_UNEXPECTED / BROKEN states explicitly instead of silently
  rendering empty tables.

Both fields default to None for full backward compatibility — existing
tools that don't emit them continue to work identically.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.v2.view_types import RenderableView, DataValidity


@dataclass
class ToolWarning:
    code: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class Coverage:
    books_matched: int = -1
    books_total: int = -1
    tokens_analyzed: int | None = None
    metadata_completeness: dict = field(default_factory=dict)


@dataclass
class SourceInfo:
    corpus_version: str
    analytics_version: str
    spgc_baseline: str = "SPGC-2018-07-18"
    chroma_collection: str | None = None
    # Sprint 19 — when the tool result references at least one user-
    # uploaded book (U-prefix id), set has_user_uploads=True. The
    # renderer adds a small footer telling the user that part of the
    # answer comes from their own uploads, not the canonical SPGC
    # baseline. Doesn't change tool behaviour — purely informational.
    has_user_uploads: bool = False
    user_upload_count: int = 0


@dataclass
class ToolError:
    type: Literal["invalid_args", "not_found", "timeout", "internal", "rate_limited"]
    message: str
    details: dict = field(default_factory=dict)
    retryable: bool = False


@dataclass
class ToolResult:
    ok: bool
    tool: str
    query: dict = field(default_factory=dict)
    data: Any = None
    warnings: list[ToolWarning] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    source_info: SourceInfo | None = None
    runtime_ms: int = 0
    cache_hit: bool = False
    error: ToolError | None = None
    # v5 Phase 0 — declarative render-schema (optional, backward compatible).
    # When set, the v5 renderer reads this instead of inferring shape from
    # `data`. Phase 0: emit-only; Phase 3: renderer enforces.
    view: "RenderableView | None" = None
    # v5 Phase 0 — semantic-level validity signal beyond ok/fail.
    # Golden tests assert specific values (e.g. learning_words on PG1342
    # level=B2 must be DataValidity.OK, never BROKEN).
    data_validity: "DataValidity | None" = None

    # ----- builders -----

    @classmethod
    def success(cls, tool: str, data: Any, *, query: dict | None = None,
                warnings: list[ToolWarning] | None = None,
                coverage: Coverage | None = None,
                source_info: SourceInfo | None = None) -> ToolResult:
        return cls(
            ok=True, tool=tool, query=query or {}, data=data,
            warnings=warnings or [],
            coverage=coverage or Coverage(),
            source_info=source_info,
        )

    @classmethod
    def fail(cls, tool: str, err_type: str, message: str, *,
             details: dict | None = None, retryable: bool = False,
             query: dict | None = None) -> ToolResult:
        return cls(
            ok=False, tool=tool, query=query or {}, data=None,
            error=ToolError(type=err_type, message=message,
                            details=details or {}, retryable=retryable),
        )

    @classmethod
    def from_legacy(cls, tool: str, raw: Any, *,
                    runtime_ms: int, source_info: SourceInfo | None,
                    query: dict | None = None) -> ToolResult:
        """Wrap a v1 tool return into v2 envelope.

        Pre-v2 tools either return a plain dict or {"error": ..., "details": ...}.
        We don't have coverage/warnings — leave them empty; callers can still
        thread the result through the v2 router without crashes."""
        if isinstance(raw, dict) and "error" in raw:
            return cls(
                ok=False, tool=tool, query=query or {}, data=None,
                error=ToolError(
                    type="internal", message=str(raw.get("error")),
                    details={k: v for k, v in raw.items() if k != "error"},
                    retryable=False,
                ),
                runtime_ms=runtime_ms, source_info=source_info,
            )
        return cls(
            ok=True, tool=tool, query=query or {}, data=raw,
            runtime_ms=runtime_ms, source_info=source_info,
        )

    # ----- serialization -----

    def to_dict(self, *, drop_empty: bool = True) -> dict:
        # Snapshot non-dataclass fields (view, data_validity) before asdict()
        # so we can render them as plain dicts/strings, not enum objects.
        view_dict = self.view.to_dict() if self.view is not None else None
        validity_val = (
            self.data_validity.value if self.data_validity is not None else None
        )
        # Detach v5 fields, call asdict on the dataclass slice, re-attach
        # them in serializable form. This keeps asdict() happy and gives
        # downstream consumers JSON-clean output.
        saved_view, saved_validity = self.view, self.data_validity
        self.view, self.data_validity = None, None
        try:
            d = asdict(self)
        finally:
            self.view, self.data_validity = saved_view, saved_validity
        if view_dict is not None:
            d["view"] = view_dict
        if validity_val is not None:
            d["data_validity"] = validity_val
        if drop_empty:
            d = _strip_empty(d)
        return d

    def to_llm_string(self, *, max_chars: int = 4000) -> str:
        """Compact JSON for the assistant tool-role message content.

        Drops fields useful only for logging/observability so the LLM sees just
        the data + warnings + minimal coverage. Truncates to keep context lean."""
        d = self.to_dict()
        for k in ("source_info", "runtime_ms", "cache_hit"):
            d.pop(k, None)
        s = json.dumps(d, ensure_ascii=False, default=str)
        if len(s) > max_chars:
            s = s[:max_chars] + '..."(truncated)"'
        return s

    def with_cache_hit(self, hit: bool = True) -> ToolResult:
        self.cache_hit = hit
        return self


def _strip_empty(obj):
    """Recursively drop keys whose value is None / empty list / empty dict."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v2 = _strip_empty(v)
            if v2 is None:
                continue
            if isinstance(v2, (list, dict)) and not v2:
                continue
            out[k] = v2
        return out
    if isinstance(obj, list):
        return [_strip_empty(x) for x in obj]
    return obj


def now_ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
