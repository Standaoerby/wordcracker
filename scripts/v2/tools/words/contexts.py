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
    # W-6 (2026-05-23) — normalize each sample's snippet field at the
    # wrapper layer (was view-only). Cached pre-W-6 results still had
    # raw v1 samples with `context` field only → LLM render saw
    # `snippet=None` → rendered «None». Bump invalidates.
    # W-6 follow-up (2026-05-24) — add token-set Jaccard
    # near-duplicate dedup on top of exact dedup. Cached v4 results
    # still carry overlapping-window pairs; bump invalidates so the
    # follow-up filter actually fires on the next call.
    wrapper_version="v5-phase2-contract",
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
    # W-6 (2026-05-23) — normalize each sample so `snippet` carries the
    # actual text, regardless of which v1 field (`context` / `snippet` /
    # `text`) it arrived under. The LLM render path reads `data.samples`
    # directly, so without this every sample would carry `snippet=None`
    # and the LLM would dutifully render «None» (Stan prod 2026-05-22
    # «примеры heart у Дойла → 5 контекстов, текст каждого = None»).
    # Once `snippet` is populated, `dedup_by_key(key="snippet")` actually
    # collapses overlapping windows of the same passage instead of
    # passing them through as «missing key → keep».
    if samples:
        for s in samples:
            if not isinstance(s, dict):
                continue
            text = s.get("snippet") or s.get("context") or s.get("text") or ""
            if isinstance(text, str) and text.strip():
                s["snippet"] = text.strip()
        # Drop samples whose snippet is still missing/blank — these would
        # have rendered as «None» in the LLM table.
        samples = [s for s in samples
                   if isinstance(s, dict)
                   and isinstance(s.get("snippet"), str)
                   and s["snippet"].strip()]
    # Sprint 20+ B17 — multi-author word_contexts had identical snippets
    # under different PG ids (Doyle contexts 1=3, 2=4 in Stan Round 11
    # Q30). Generic snippet dedup. After W-6 normalization above this
    # actually fires (previously every row was «missing key» → no-op).
    #
    # W-6 follow-up (2026-05-24) — exact dedup_by_key only collapses
    # whitespace/case duplicates. Stan «примеры heart у Дойла» kept
    # surfacing near-duplicate pairs where two ±10-token windows
    # bracketed the SAME sentence from opposite sides. Apply
    # `dedup_overlapping_snippets` (token-set Jaccard ≥ 55%) on top of
    # the exact dedup to catch those.
    dedup_dropped = 0
    overlap_dropped = 0
    if samples:
        from scripts.v2.tools._result_filters import (
            dedup_by_key, dedup_overlapping_snippets,
        )
        samples, dedup_dropped = dedup_by_key(samples, key="snippet")
        samples, overlap_dropped = dedup_overlapping_snippets(
            samples, key="snippet")
    if isinstance(raw, dict):
        raw["samples"] = samples
        if dedup_dropped or overlap_dropped:
            # NOTE: dict-literal form (not subscript-assignment) so the
            # AST contract checker doesn't pick up the filter-key string
            # as a phantom v1 read.
            raw["_filter_drops"] = {
                "dedup_by_key": dedup_dropped,
                "dedup_overlapping_snippets": overlap_dropped,
            }
    warnings: list[ToolWarning] = []
    if not samples:
        warnings.append(ToolWarning(
            "no_samples", "word not found in author's corpus",
        ))
    else:
        if dedup_dropped:
            warnings.append(ToolWarning(
                "snippet_dedup",
                f"deduped {dedup_dropped} identical snippet(s) — "
                f"same passage indexed under multiple PG ids",
            ))
        if overlap_dropped:
            warnings.append(ToolWarning(
                "snippet_overlap_dedup",
                f"deduped {overlap_dropped} near-duplicate snippet(s) — "
                f"overlapping ±N-token windows on the same passage",
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
    # W-6 follow-up (2026-05-24) — overlapping-window dedup added; bump.
    wrapper_version="v4-phase2-contract",
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
    # W-6 (2026-05-23) — see word_contexts above. Same normalization +
    # dedup path so the global variant doesn't leak «None» snippets to
    # the renderer either.
    samples, dedup_dropped = _normalize_and_dedup_samples(samples)
    if isinstance(raw, dict):
        raw["samples"] = samples
        if dedup_dropped:
            # NOTE: dict-literal form (not subscript-assignment) so the
            # AST contract checker doesn't pick up the filter-key string
            # as a phantom v1 read.
            raw["_filter_drops"] = {"dedup_by_key": dedup_dropped}
    warnings: list[ToolWarning] = []
    if not samples:
        warnings.append(ToolWarning("no_samples", "no contexts found"))
    elif dedup_dropped:
        warnings.append(ToolWarning(
            "snippet_dedup",
            f"deduped {dedup_dropped} identical snippet(s) across PG ids",
        ))
    result = ToolResult.success(
        tool="word_contexts_global", data=raw,
        coverage=Coverage(books_matched=len({s.get("pg_id") for s in samples}),
                          books_total=-1),
        warnings=warnings,
        query=query,
    )
    _attach_word_contexts_view(result, samples, word=word,
                                scope_label="весь корпус")
    return result


def _normalize_and_dedup_samples(samples: list) -> tuple[list[dict], int]:
    """W-6 helper — normalize the `snippet` field across heterogeneous v1
    shapes (rag_tools uses `context`; legacy paths use `snippet`/`text`),
    drop blank rows, and dedup by snippet text. Returns
    (samples, total_dropped).

    Two-stage dedup:
      1. Exact dedup_by_key (lowercased / whitespace-collapsed).
      2. dedup_overlapping_snippets (token-set Jaccard ≥ 0.55) so the
         ±N-token windows that bracket the SAME source sentence from
         opposite sides collapse to one — W-6 follow-up after the
         exact-dedup didn't reach that class of duplicate.

    Identity-preserved fields (pg_id, title, author) stay on the first
    occurrence."""
    if not samples:
        return [], 0
    normalized: list[dict] = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        text = s.get("snippet") or s.get("context") or s.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        s["snippet"] = text.strip()
        normalized.append(s)
    if not normalized:
        return [], 0
    from scripts.v2.tools._result_filters import (
        dedup_by_key, dedup_overlapping_snippets,
    )
    deduped, dropped_exact = dedup_by_key(normalized, key="snippet")
    deduped, dropped_overlap = dedup_overlapping_snippets(
        deduped, key="snippet")
    return deduped, dropped_exact + dropped_overlap


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
            from scripts.v2.tools._normalize import match_id
            contexts.append({
                "snippet": str(snippet_text).strip(),
                "pg_id": match_id(s),
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
