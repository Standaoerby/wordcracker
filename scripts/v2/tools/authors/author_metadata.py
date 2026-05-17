"""v2 author_metadata — quick stats for a single author.

Delegates to v1 rag_tools.author_metadata; wraps in ToolResult."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult


@tool(
    name="author_metadata",
    category="authors",
    description=(
        "Быстрая мета по автору: годы жизни, язык, количество книг, total downloads, "
        "примеры названий. Используй для «когда родился X», «сколько у X книг»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string",
                             "description": "Regex по колонке author, обычно '^Surname,', e.g. '^Doyle,'"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="cheap",
    cacheable=True,
)
def author_metadata(author_regex: str) -> ToolResult:
    if not author_regex or not author_regex.strip():
        return ToolResult.fail(
            tool="author_metadata", err_type="invalid_args",
            message="author_regex is required",
        )

    try:
        from scripts.rag_tools import author_metadata as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="author_metadata", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(author_regex)
    query = {"author_regex": author_regex}

    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="author_metadata",
            err_type="not_found" if "no books" in raw.get("error", "") else "internal",
            message=str(raw["error"]),
            details={k: v for k, v in raw.items() if k != "error"},
            query=query,
        )

    book_count = raw.get("books_total") or raw.get("book_count") or len(raw.get("sample_titles", []))
    return ToolResult.success(
        tool="author_metadata", data=raw,
        coverage=Coverage(books_matched=int(book_count or 0), books_total=-1),
        query=query,
    )
