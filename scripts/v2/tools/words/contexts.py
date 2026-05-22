"""v2 word_contexts (per-author) + word_contexts_global."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1WordContexts, V1WordContextsGlobal,
)


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
    # R-23 Tier 0 — E9 fix (snippet vs context vs text key) + B17 dedup
    # landed without a version bump, so stale cached results from
    # before the fixes were still being served.
    wrapper_version="v3-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.word_contexts",
             schema=V1WordContexts)
def word_contexts(author_regex: str, word: str, window: int = 10,
                  max_samples: int = 5) -> ToolResult:
    from scripts.rag_tools import word_contexts as _v1
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
    result = ToolResult.success(
        tool="word_contexts", data=raw,
        coverage=Coverage(books_matched=len(samples), books_total=-1),
        warnings=warnings,
        query=query,
    )
    _attach_word_contexts_view(result, samples, word=word,
                                scope_label=author_regex.lstrip("^").rstrip(",").strip())
    return result


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
    # R-23 Tier 0 — see word_contexts: same context-key fix.
    wrapper_version="v3-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.word_contexts_global",
             schema=V1WordContextsGlobal)
def word_contexts_global(word: str, k: int = 12,
                         snippet_chars: int = 280) -> ToolResult:
    from scripts.rag_tools import word_contexts_global as _v1
    raw = _v1(word=word, k=k, snippet_chars=snippet_chars)
    query = {"word": word, "k": k, "snippet_chars": snippet_chars}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_contexts_global", err_type="not_found",
                               message=str(raw["error"]), query=query)
    samples = (raw.get("samples") if isinstance(raw, dict) else None) or []
    result = ToolResult.success(
        tool="word_contexts_global", data=raw,
        coverage=Coverage(books_matched=len({s.get("pg_id") for s in samples}),
                          books_total=-1),
        warnings=[ToolWarning("no_samples", "no contexts found")]
                 if not samples else [],
        query=query,
    )
    _attach_word_contexts_view(result, samples, word=word,
                                scope_label="весь корпус")
    return result


# =====================================================================
# v5 Phase 2.5 — view emission helper
# =====================================================================


def _attach_word_contexts_view(result, samples, *, word: str,
                                 scope_label: str) -> None:
    """Defensive view emission for E9 (snippet=None) closure.

    R-22 P9 — prod rendered «None» as literal string for each snippet.
    ROOT CAUSE: v1 `word_contexts` returns samples with field `context`
    (NOT `snippet` or `text`). Old wrapper code looked for `snippet`/
    `text` only — value defaulted to None, propagated through view
    payload, renderer stringified to «None».

    Fix here is two-layer:
      1. Read ANY plausible field — `snippet` OR `text` OR `context`
      2. Filter out samples with empty/whitespace text BEFORE building
         view. If all samples empty → emit empty_state explicit caveat.
    """
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        contexts = []
        for s in (samples or [])[:10]:
            if not isinstance(s, dict):
                continue
            # E9 root cause fix: v1 uses `context` field, not `snippet`
            snippet_text = (
                s.get("snippet")
                or s.get("text")
                or s.get("context")
                or ""
            )
            # Defensive: skip rows with no actual text (don't pollute
            # view with empty/None snippets)
            if not snippet_text or not str(snippet_text).strip():
                continue
            contexts.append({
                "snippet": str(snippet_text).strip(),
                "pg_id": s.get("pg_id") or s.get("id"),
                "title": s.get("title") or "",
                "author": s.get("author") or "",
            })
        view = vb.build_word_contexts(
            word=word,
            contexts=contexts,
            scope_label=scope_label,
            language="ru",
        )
        validity = DataValidity.OK if contexts else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.contexts").exception(
            "word_contexts view emission failed"
        )
