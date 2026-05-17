"""v2 affinity_by_book — book-scoped signature words.

Lives in scripts/learning_tools.py in v1 (because the original implementation
was part of the learning pipeline). v2 wraps it under category='books' so the
intent router can find it under the book taxonomy.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult, ToolWarning


@tool(
    name="affinity_by_book",
    category="books",
    description=(
        "Фирменные слова конкретной книги по affinity (частота в книге vs корпус). "
        "ВСЕГДА после find_book — никогда не угадывай PG id из памяти."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pg_id":               {"type": "string"},
            "top":                 {"type": "integer", "description": "default 50"},
            "pos_filter":          {"type": "array", "items": {"type": "string"}},
            "min_corpus_count":    {"type": "integer", "description": "default 200"},
            "exclude_proper_nouns": {"type": "boolean", "description": "default true"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="medium",
    cacheable=True,
)
def affinity_by_book(pg_id: str, top: int = 50,
                     pos_filter: list[str] | None = None,
                     min_corpus_count: int = 200,
                     exclude_proper_nouns: bool = True) -> ToolResult:
    try:
        from scripts.learning_tools import affinity_by_book as _v1
    except ImportError as e:
        return ToolResult.fail(tool="affinity_by_book", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id, top=top, pos_filter=pos_filter,
              min_corpus_count=min_corpus_count,
              exclude_proper_nouns=exclude_proper_nouns)
    query = {"pg_id": pg_id, "top": top, "pos_filter": pos_filter,
             "min_corpus_count": min_corpus_count,
             "exclude_proper_nouns": exclude_proper_nouns}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="affinity_by_book",
            err_type=("not_found" if "no counts" in err.lower() or "no token" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    rows = (raw.get("top_words") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            code="empty_top", message="no signature words above min_corpus_count",
        ))
    return ToolResult.success(
        tool="affinity_by_book", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        warnings=warnings, query=query,
    )
