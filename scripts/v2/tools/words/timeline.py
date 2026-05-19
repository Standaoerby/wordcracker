"""v2 word_freq_timeline + words_disappearing_after."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


@tool(
    name="word_freq_timeline",
    category="words",
    description=(
        "Кривая частотности слова по эпохам (25-летние бакеты). "
        "ИСПОЛЬЗУЙ basis='auto' (default) — это authoryearofbirth+30 "
        "(writing prime proxy), покрывает все ~75k книг корпуса. "
        "basis='pub_year' покрывает только ~4 книги (Open Library "
        "enrichment ещё неполный) — НЕ выбирай его для запросов про "
        "1800-1950 годы. basis='birth_plus_30' = синоним 'auto'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "word":         {"type": "string"},
            "bucket_years": {"type": "integer", "description": "default 25"},
            "basis":        {"type": "string",
                              "enum": ["auto", "birth_plus_30", "pub_year"],
                              "description":
                              "default 'auto' (= birth_plus_30, full corpus). "
                              "pub_year coverage ≈ 4 books only — avoid"},
        },
        "required": ["word"],
    },
    requires=["word"],
    cost="medium",
    cacheable=True,
)
def word_freq_timeline(word: str, bucket_years: int = 25,
                      basis: str = "auto") -> ToolResult:
    try:
        from scripts.rag_tools import word_freq_timeline as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_freq_timeline", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(word=word, bucket_years=bucket_years, basis=basis)
    query = {"word": word, "bucket_years": bucket_years, "basis": basis}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_freq_timeline", err_type="not_found",
                               message=str(raw["error"]), query=query)

    buckets = (raw.get("timeline") if isinstance(raw, dict) else None) or []

    # Sprint 20 — Stan 2026-05-19: LLM-planner emitted basis='pub_year',
    # which has ~4 books coverage. Result: 1 bucket "2000-2024" with 0
    # occurrences, renderer correctly cited it and said «не охватывают
    # 1880-1950». Auto-fallback to 'auto' when pub_year gives <3
    # buckets OR all zeros — saves the user from a misleading answer.
    auto_fallback_used = False
    if (basis == "pub_year"
            and isinstance(raw, dict)
            and (len(buckets) < 3
                 or all((b.get("occurrences", 0) == 0) for b in buckets))):
        raw_auto = _v1(word=word, bucket_years=bucket_years, basis="auto")
        if isinstance(raw_auto, dict) and not raw_auto.get("error"):
            buckets_auto = raw_auto.get("timeline") or []
            if len(buckets_auto) >= 3 or any(
                    b.get("occurrences", 0) > 0 for b in buckets_auto):
                raw = raw_auto
                buckets = buckets_auto
                auto_fallback_used = True
                raw["basis_originally_requested"] = "pub_year"
                raw["basis_fallback_reason"] = (
                    "pub_year coverage is ~4 books (sparse / all-zero); "
                    "auto-fallback to birth_plus_30 for full corpus coverage"
                )
                existing = raw.get("_render_note") or ""
                fallback_note = (
                    "Tool wrapper auto-fell-back from basis='pub_year' "
                    "(sparse coverage) to basis='auto' "
                    "(birth_plus_30 — full ~75k books). Renderer must "
                    "report the new buckets, NOT the empty pub_year result."
                )
                raw["_render_note"] = (
                    (existing + " | " if existing else "") + fallback_note
                )

    warnings: list[ToolWarning] = []
    if len(buckets) < 3:
        warnings.append(ToolWarning(
            code="sparse_timeline",
            message=f"only {len(buckets)} bucket(s) — coverage is thin",
        ))
    if auto_fallback_used:
        warnings.append(ToolWarning(
            code="basis_auto_fallback",
            message=("pub_year coverage was ~4 books → fell back to "
                      "birth_plus_30 for usable timeline"),
        ))

    return ToolResult.success(
        tool="word_freq_timeline", data=raw,
        coverage=Coverage(
            books_matched=raw.get("books_total", -1) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings,
        query=query,
    )


@tool(
    name="words_disappearing_after",
    category="words",
    description="Слова, которые резко вышли из употребления после заданного года.",
    input_schema={
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "cutoff year, default 1920"},
            "top":  {"type": "integer", "description": "default 25"},
        },
        "required": [],
    },
    requires=[],
    cost="heavy",
    cacheable=True,
)
def words_disappearing_after(year: int = 1920, top: int = 25) -> ToolResult:
    try:
        from scripts.rag_tools import words_disappearing_after as _v1
    except ImportError as e:
        return ToolResult.fail(tool="words_disappearing_after", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(year=year, top=top)
    query = {"year": year, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="words_disappearing_after",
            err_type=("not_found" if "not enough" in err.lower() or "no books" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    rows = (raw.get("words") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    if isinstance(raw, dict) and raw.get("books_before"):
        warnings.append(ToolWarning(
            code="coverage",
            message=f"{raw.get('books_before')} books before {year}, "
                    f"{raw.get('books_after', 0)} after",
        ))
    return ToolResult.success(
        tool="words_disappearing_after", data=raw,
        coverage=Coverage(
            books_matched=(raw.get("books_before", 0) + raw.get("books_after", 0))
                          if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
