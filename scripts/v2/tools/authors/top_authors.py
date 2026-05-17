"""v2 top_authors_by and top_authors_by_country.

Delegates to v1 implementations; wraps with ToolResult + Coverage. Both tools
register separately because their input schemas differ (country requires ISO-2)
and the planner picks between them based on `Entities.country`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


@tool(
    name="top_authors_by",
    category="authors",
    description=(
        "Топ-N авторов по метрике. metric='books' (число книг) | 'downloads' (суммарные скачивания) | "
        "'tokens' (суммарные токены, медленнее). Используй для «топ авторов», «самые популярные»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": ["books", "downloads", "tokens"],
                       "description": "default 'books'"},
            "top":    {"type": "integer", "description": "default 10"},
            "lang":   {"type": "string", "description": "default 'en'"},
            "include_generic": {"type": "boolean",
                                "description": "включать ли 'Various/Anonymous/Unknown' (default false)"},
        },
        "required": [],
    },
    requires=[],
    cost="medium",
    cacheable=True,
)
def top_authors_by(metric: str = "books", top: int = 10, lang: str = "en",
                   include_generic: bool = False) -> ToolResult:
    try:
        from scripts.rag_tools import top_authors_by as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="top_authors_by", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(metric=metric, top=top, lang=lang, include_generic=include_generic)
    query = {"metric": metric, "top": top, "lang": lang, "include_generic": include_generic}

    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="top_authors_by", err_type="invalid_args",
            message=str(raw["error"]), query=query,
        )

    rows = raw.get("top", []) if isinstance(raw, dict) else []
    return ToolResult.success(
        tool="top_authors_by", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )


@tool(
    name="top_authors_by_country",
    category="authors",
    description=(
        "Топ-N авторов из конкретной страны (ISO-2 code, e.g. 'GB'/'US'/'RU'). "
        "Использует Wikidata enrichment (Sprint 9.2). "
        "Используй для «топ британских/американских авторов», BrE vs AmE сравнений."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "country": {"type": "string", "description": "ISO-2: GB / US / RU / FR / DE ..."},
            "metric":  {"type": "string", "enum": ["books", "downloads"], "description": "default 'books'"},
            "top":     {"type": "integer", "description": "default 20"},
        },
        "required": ["country"],
    },
    requires=["country"],
    cost="medium",
    cacheable=True,
)
def top_authors_by_country(country: str, metric: str = "books", top: int = 20) -> ToolResult:
    if not country or not country.strip():
        return ToolResult.fail(
            tool="top_authors_by_country", err_type="invalid_args",
            message="country code required (e.g. 'GB')",
        )

    try:
        from scripts.rag_tools import top_authors_by_country as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="top_authors_by_country", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(country=country, metric=metric, top=top)
    query = {"country": country, "metric": metric, "top": top}

    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        err_type = "not_found" if ("no authors" in err or "empty" in err) else "internal"
        return ToolResult.fail(
            tool="top_authors_by_country", err_type=err_type,
            message=err, query=query,
        )

    rows = raw.get("top", []) if isinstance(raw, dict) else []
    return ToolResult.success(
        tool="top_authors_by_country", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )
