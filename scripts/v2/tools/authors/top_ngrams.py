"""v2 top_ngrams_by_author + lexical_diversity."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


@tool(
    name="top_ngrams_by_author",
    category="authors",
    description=(
        "Топ N-грамм у автора. n=1 unigrams, n=2 bigrams, n=3 trigrams. "
        "Используй для «фирменные обороты», «частые связки слов», «биграммы X». "
        "Передай author_regex='.*' если фильтруешь только по period/country."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "n":            {"type": "integer", "description": "1, 2 или 3"},
            "top":          {"type": "integer", "description": "default 20"},
            "pos_filter":   {"type": "array", "items": {"type": "string"}},
            "year_from":    {"type": "integer"},
            "year_to":      {"type": "integer"},
            "country":      {"type": "string"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="heavy",
    cacheable=True,
    timeout_s=120,
)
def top_ngrams_by_author(author_regex: str, n: int = 2, top: int = 20,
                         pos_filter=None, year_from=None, year_to=None,
                         country=None) -> ToolResult:
    try:
        from scripts.rag_tools import top_ngrams_by_author as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_ngrams_by_author", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(author_regex=author_regex, n=n, top=top, pos_filter=pos_filter,
              year_from=year_from, year_to=year_to, country=country)
    query = {"author_regex": author_regex, "n": n, "top": top,
             "pos_filter": pos_filter, "year_from": year_from,
             "year_to": year_to, "country": country}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="top_ngrams_by_author", err_type="not_found",
                               message=str(raw["error"]), query=query)
    rows = (raw.get("top_ngrams") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="top_ngrams_by_author", data=raw,
        coverage=Coverage(books_matched=raw.get("books_used", -1)
                                       if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=[ToolWarning("empty_top", "no ngrams matched filters")]
                 if not rows else [],
        query=query,
    )


@tool(
    name="lexical_diversity",
    category="authors",
    description=(
        "Лексическая разнообразность: TTR + per-book averages. "
        "Используй для «какая лексическая плотность у X», «насколько разнообразный словарь»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {"type": "object",
                      "description": "{'book': PGid} | {'author': regex} | 'all_corpus'"},
        },
        "required": ["scope"],
    },
    requires=["scope"],
    cost="medium",
    cacheable=True,
)
def lexical_diversity(scope) -> ToolResult:
    try:
        from scripts.rag_tools import lexical_diversity as _v1
    except ImportError as e:
        return ToolResult.fail(tool="lexical_diversity", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope)
    query = {"scope": scope}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="lexical_diversity",
            err_type=("invalid_args" if "scope" in err.lower() else "not_found"),
            message=err, query=query,
        )
    n_books = raw.get("books_total", -1) if isinstance(raw, dict) else -1
    return ToolResult.success(
        tool="lexical_diversity", data=raw,
        coverage=Coverage(books_matched=n_books, books_total=-1),
        query=query,
    )
