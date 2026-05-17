"""v2 top_books_by_downloads + top_books_by_recency + book_emotion_profile."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


@tool(
    name="top_books_by_downloads",
    category="books",
    description=(
        "Топ-N самых скачиваемых книг. Опциональный author_regex для one-author top."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string"},
        },
        "required": [],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
)
def top_books_by_downloads(top: int = 20, lang: str = "en",
                           author_regex: str | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import top_books_by_downloads as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_books_by_downloads", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(top=top, lang=lang, author_regex=author_regex)
    query = {"top": top, "lang": lang, "author_regex": author_regex}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="top_books_by_downloads",
                               err_type="not_found",
                               message=str(raw["error"]), query=query)
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="top_books_by_downloads", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )


@tool(
    name="top_books_by_recency",
    category="books",
    description=(
        "Топ-N свежих книг. metric='pg_id' (date added to PG, default) | "
        "'pub_year' (real publication year via Open Library enrichment)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string"},
            "metric":       {"type": "string",
                             "enum": ["pg_id", "pub_year"],
                             "description": "default 'pg_id'"},
        },
        "required": [],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
)
def top_books_by_recency(top: int = 20, lang: str = "en",
                         author_regex: str | None = None,
                         metric: str = "pg_id") -> ToolResult:
    try:
        from scripts.rag_tools import top_books_by_recency as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_books_by_recency", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(top=top, lang=lang, author_regex=author_regex, metric=metric)
    query = {"top": top, "lang": lang, "author_regex": author_regex,
             "metric": metric}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        # pub_year metric can fall back when batch isn't done — that's a
        # warning, not a hard error: surface to caller as not_found so the
        # planner can suggest the fallback metric.
        return ToolResult.fail(tool="top_books_by_recency", err_type="not_found",
                               message=err, query=query)
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="top_books_by_recency", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )


@tool(
    name="book_emotion_profile",
    category="books",
    description=(
        "Эмоциональный профиль книги через NRC: counts per emotion "
        "(fear/joy/anger/sadness/anticipation/disgust/surprise/trust)."
    ),
    input_schema={
        "type": "object",
        "properties": {"pg_id": {"type": "string"}},
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="cheap",
    cacheable=True,
)
def book_emotion_profile(pg_id: str) -> ToolResult:
    try:
        from scripts.rag_tools import book_emotion_profile as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_emotion_profile", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id)
    query = {"pg_id": pg_id}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_emotion_profile", err_type="not_found",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(
        tool="book_emotion_profile", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )
