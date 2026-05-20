"""v2 word_contexts (per-author) + word_contexts_global."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


@tool(
    name="word_contexts",
    category="words",
    description="±N токенов контекста для слова у указанного автора.",
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "word":         {"type": "string"},
            "window":       {"type": "integer", "description": "default 10"},
            "max_samples":  {"type": "integer", "description": "default 5"},
        },
        "required": ["author_regex", "word"],
    },
    requires=["author", "word"],
    cost="cheap",
    cacheable=True,
)
def word_contexts(author_regex: str, word: str, window: int = 10,
                  max_samples: int = 5) -> ToolResult:
    try:
        from scripts.rag_tools import word_contexts as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_contexts", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(author_regex=author_regex, word=word, window=window,
              max_samples=max_samples)
    query = {"author_regex": author_regex, "word": word,
             "window": window, "max_samples": max_samples}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_contexts", err_type="not_found",
                               message=str(raw["error"]), query=query)
    samples = (raw.get("samples") if isinstance(raw, dict) else None) or []
    # Sprint 20+ B17 — multi-author word_contexts had identical snippets
    # under different PG ids (Doyle contexts 1=3, 2=4 in Stan Round 11
    # Q30). Generic snippet dedup.
    dedup_dropped = 0
    if samples:
        from scripts.v2.tools._result_filters import dedup_by_key
        samples, dedup_dropped = dedup_by_key(samples, key="snippet")
        if dedup_dropped and isinstance(raw, dict):
            raw["samples"] = samples
            raw["_filter_drops"] = {"dedup_by_key": dedup_dropped}
    warnings: list[ToolWarning] = []
    if not samples:
        warnings.append(ToolWarning(
            "no_samples", "word not found in author's corpus",
        ))
    elif dedup_dropped:
        warnings.append(ToolWarning(
            "snippet_dedup",
            f"deduped {dedup_dropped} identical snippet(s) — "
            f"same passage indexed under multiple PG ids",
        ))
    return ToolResult.success(
        tool="word_contexts", data=raw,
        coverage=Coverage(books_matched=len(samples), books_total=-1),
        warnings=warnings,
        query=query,
    )


@tool(
    name="word_contexts_global",
    category="words",
    description="Контексты слова у разных авторов глобально по корпусу.",
    input_schema={
        "type": "object",
        "properties": {
            "word":          {"type": "string"},
            "k":             {"type": "integer", "description": "default 12"},
            "snippet_chars": {"type": "integer", "description": "default 280"},
        },
        "required": ["word"],
    },
    requires=["word"],
    cost="medium",
    cacheable=True,
)
def word_contexts_global(word: str, k: int = 12,
                         snippet_chars: int = 280) -> ToolResult:
    try:
        from scripts.rag_tools import word_contexts_global as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_contexts_global", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(word=word, k=k, snippet_chars=snippet_chars)
    query = {"word": word, "k": k, "snippet_chars": snippet_chars}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_contexts_global", err_type="not_found",
                               message=str(raw["error"]), query=query)
    samples = (raw.get("samples") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="word_contexts_global", data=raw,
        coverage=Coverage(books_matched=len({s.get("pg_id") for s in samples}),
                          books_total=-1),
        warnings=[ToolWarning("no_samples", "no contexts found")]
                 if not samples else [],
        query=query,
    )
