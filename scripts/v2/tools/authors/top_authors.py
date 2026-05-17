"""v2 top_authors_by and top_authors_by_country.

Delegates to v1 implementations; wraps with ToolResult + Coverage. Both tools
register separately because their input schemas differ (country requires ISO-2)
and the planner picks between them based on `Entities.country`.

Sprint 11.2: metric='tokens' uses a pre-computed JSON cache built by
`scripts/v2/build_author_tokens.py` for ~1000× speedup (60s live scan
→ 50ms lookup).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


# Sprint 11.2 cache: pre-built author → tokens lookup table. Built by
# `scripts/v2/build_author_tokens.py` after each corpus refresh.
_AUTHOR_TOKENS_CACHE_PATH = Path("/workspace/spgc/derived/author_tokens.json")
_author_tokens_cache: dict | None = None


def _load_author_tokens() -> dict | None:
    """Returns the JSON cache or None when file missing — caller falls back
    to live scan."""
    global _author_tokens_cache
    if _author_tokens_cache is not None:
        return _author_tokens_cache
    if not _AUTHOR_TOKENS_CACHE_PATH.exists():
        return None
    try:
        import json
        _author_tokens_cache = json.loads(
            _AUTHOR_TOKENS_CACHE_PATH.read_text(encoding="utf-8"))
        return _author_tokens_cache
    except (OSError, json.JSONDecodeError):
        return None


# Generic-author skip substrings mirror v1's filter so cached output
# matches live-scan output (Various / Anonymous / Encyclopedia / etc).
_GENERIC_SUBSTRINGS = (
    "various", "anonymous", "unknown", "encyclop", "catholic church",
)


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
    query = {"metric": metric, "top": top, "lang": lang,
             "include_generic": include_generic}

    # Sprint 11.2 fast path: metric=tokens used to take 60s scanning every
    # per-book counts file. Pre-built JSON cache makes it 50ms.
    if metric == "tokens":
        cached = _load_author_tokens()
        if cached is not None:
            rows = []
            for author, info in cached.items():
                if not include_generic:
                    low = author.lower()
                    if any(s in low for s in _GENERIC_SUBSTRINGS):
                        continue
                rows.append({"author": author,
                             "tokens": int(info.get("tokens", 0)),
                             "books_with_counts": int(info.get("books", 0))})
            rows.sort(key=lambda r: r["tokens"], reverse=True)
            rows = rows[:top]
            return ToolResult.success(
                tool="top_authors_by",
                data={"metric": "tokens", "top_n": top, "lang": lang,
                      "top": rows, "_cache_hit": True},
                coverage=Coverage(books_matched=len(rows), books_total=-1),
                warnings=[ToolWarning(
                    "cached_aggregate",
                    "used pre-computed author_tokens.json (run "
                    "scripts/v2/build_author_tokens.py to refresh after "
                    "corpus updates)",
                )],
                query=query,
            )
        # Fall through to live scan if cache absent

    try:
        from scripts.rag_tools import top_authors_by as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="top_authors_by", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(metric=metric, top=top, lang=lang, include_generic=include_generic)

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
