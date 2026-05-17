"""v2 book_readability + book_archaic_words."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult


@tool(
    name="book_readability",
    category="books",
    description="Flesch + FK Grade + CEFR estimate для одной книги. Используй после find_book.",
    input_schema={
        "type": "object",
        "properties": {
            "pg_id":        {"type": "string"},
            "sample_chars": {"type": "integer", "description": "default 200000"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="cheap",
    cacheable=True,
)
def book_readability(pg_id: str, sample_chars: int = 200_000) -> ToolResult:
    try:
        from scripts.rag_tools import book_readability as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_readability", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id, sample_chars=sample_chars)
    query = {"pg_id": pg_id, "sample_chars": sample_chars}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_readability", err_type="not_found",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(
        tool="book_readability", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )


@tool(
    name="book_archaic_words",
    category="books",
    description="Архаизмы/устаревшие слова в книге — seed list + enrich cache lookups.",
    input_schema={
        "type": "object",
        "properties": {
            "pg_id": {"type": "string"},
            "top":   {"type": "integer", "description": "default 30"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="medium",
    cacheable=True,
)
def book_archaic_words(pg_id: str, top: int = 30) -> ToolResult:
    try:
        from scripts.learning_tools import book_archaic_words as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_archaic_words", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id, top=top)
    query = {"pg_id": pg_id, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_archaic_words", err_type="not_found",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(
        tool="book_archaic_words", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )
