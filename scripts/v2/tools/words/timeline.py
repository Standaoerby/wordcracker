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
    description="Кривая частотности слова по эпохам (25-летние бакеты, через authoryearofbirth+30 либо pub_year).",
    input_schema={
        "type": "object",
        "properties": {
            "word":         {"type": "string"},
            "bucket_years": {"type": "integer", "description": "default 25"},
            "basis":        {"type": "string", "enum": ["auto", "pub_year", "birth_plus_30"]},
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
    return ToolResult.success(
        tool="word_freq_timeline", data=raw,
        coverage=Coverage(books_matched=raw.get("books_total", -1) if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=[ToolWarning("sparse_timeline", "fewer than 3 buckets have data")]
                 if len(buckets) < 3 else [],
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
