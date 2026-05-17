"""v2 author_profile + author_influences + author_attribution — Burrows Delta + combo."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult


@tool(
    name="author_profile",
    category="authors",
    description="Combo: metadata + stats + signature + bigrams + diversity + influences + emotions. Параллельно.",
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "country":      {"type": "string", "description": "optional country filter"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="heavy",
    cacheable=True,
    timeout_s=60,
)
def author_profile(author_regex: str, country: str | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import author_profile as _v1
    except ImportError as e:
        return ToolResult.fail(tool="author_profile", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(author_regex=author_regex, country=country)
    query = {"author_regex": author_regex, "country": country}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_profile", err_type="not_found",
                               message=str(raw["error"]), query=query)
    md = (raw.get("metadata") if isinstance(raw, dict) else None) or {}
    return ToolResult.success(
        tool="author_profile", data=raw,
        coverage=Coverage(books_matched=md.get("books_total", -1), books_total=-1),
        query=query,
    )


@tool(
    name="author_influences",
    category="authors",
    description="Близкие по стилю авторы по Burrows Delta. Closest neighbours.",
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "top":          {"type": "integer", "description": "default 10"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
)
def author_influences(author_regex: str, top: int = 10) -> ToolResult:
    try:
        from scripts.rag_tools import author_influences as _v1
    except ImportError as e:
        return ToolResult.fail(tool="author_influences", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(author_regex=author_regex, top=top)
    query = {"author_regex": author_regex, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_influences", err_type="not_found",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(
        tool="author_influences", data=raw,
        coverage=Coverage(),
        query=query,
    )


@tool(
    name="author_attribution",
    category="authors",
    description="Burrows Delta attribution: дан текст, найти top-N candidate авторов.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "сам текст (>= 500 слов)"},
            "top":  {"type": "integer", "description": "default 5"},
        },
        "required": ["text"],
    },
    requires=[],
    cost="medium",
    cacheable=False,  # texts are unique, no point caching
)
def author_attribution(text: str, top: int = 5) -> ToolResult:
    try:
        from scripts.rag_tools import author_attribution as _v1
    except ImportError as e:
        return ToolResult.fail(tool="author_attribution", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(text=text, top=top)
    query = {"text_chars": len(text), "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_attribution", err_type="invalid_args",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(tool="author_attribution", data=raw, query=query)
