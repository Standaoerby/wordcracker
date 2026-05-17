"""v2 word_collocates."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult, ToolWarning


@tool(
    name="word_collocates",
    category="words",
    description="Слова в окне ±N токенов вокруг target word в scope.",
    input_schema={
        "type": "object",
        "properties": {
            "scope":              {"type": "object"},
            "word":               {"type": "string"},
            "window":             {"type": "integer", "description": "default 4"},
            "top":                {"type": "integer", "description": "default 20"},
            "exclude_stopwords":  {"type": "boolean", "description": "default true"},
            "max_books":          {"type": "integer", "description": "default 8000"},
        },
        "required": ["scope", "word"],
    },
    requires=["word", "scope"],
    cost="medium",
    cacheable=True,
)
def word_collocates(scope, word: str, window: int = 4, top: int = 20,
                    exclude_stopwords: bool = True,
                    max_books: int = 8000) -> ToolResult:
    try:
        from scripts.rag_tools import word_collocates as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_collocates", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope, word=word, window=window, top=top,
              exclude_stopwords=exclude_stopwords, max_books=max_books)
    query = {"scope": scope, "word": word, "window": window, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_collocates",
                               err_type="invalid_args" if "scope" in str(raw["error"]).lower()
                                                       else "not_found",
                               message=str(raw["error"]), query=query)
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    if isinstance(raw, dict) and raw.get("books_capped"):
        warnings.append(ToolWarning(
            code="books_capped",
            message=f"scope had {raw.get('books_total')} books; capped at {max_books}",
            details={"books_total": raw.get("books_total"), "capped": max_books},
        ))
    return ToolResult.success(
        tool="word_collocates", data=raw,
        coverage=Coverage(
            books_matched=raw.get("books_total", len(rows)) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
