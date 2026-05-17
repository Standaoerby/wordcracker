"""v2 affinity_by_author + compare_authors — author-level stylistic stats."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult, ToolWarning


@tool(
    name="affinity_by_author",
    category="authors",
    description=(
        "Фирменные слова автора по метрике affinity (частота у автора vs корпус). "
        "Используй для «фирменные слова X», «характерные», «маркеры стиля». "
        "POS-фильтр через pos_filter=['ADJ'/'NOUN'/'VERB']."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex":     {"type": "string"},
            "top":              {"type": "integer", "description": "default 50"},
            "min_author_count": {"type": "integer", "description": "default 5"},
            "min_corpus_count": {"type": "integer",
                                 "description": "default 0; bump to 500+ для filtering OOV/имен"},
            "pos_filter":       {"type": "array", "items": {"type": "string"}},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
)
def affinity_by_author(author_regex: str, top: int = 50,
                       min_author_count: int = 5, min_corpus_count: int = 0,
                       pos_filter: list[str] | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import affinity_by_author as _v1
    except ImportError as e:
        return ToolResult.fail(tool="affinity_by_author", err_type="internal",
                               message=f"v1 unavailable: {e}")

    raw = _v1(author_regex=author_regex, top=top,
              min_author_count=min_author_count,
              min_corpus_count=min_corpus_count, pos_filter=pos_filter)
    query = {"author_regex": author_regex, "top": top,
             "min_author_count": min_author_count,
             "min_corpus_count": min_corpus_count, "pos_filter": pos_filter}

    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="affinity_by_author",
            err_type=("not_found" if "no books" in err.lower() else "internal"),
            message=err, query=query,
        )

    rows = (raw.get("top_words") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            code="empty_top",
            message="affinity returned no words — perhaps min_corpus_count too high",
        ))
    return ToolResult.success(
        tool="affinity_by_author", data=raw,
        coverage=Coverage(books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=warnings, query=query,
    )


@tool(
    name="compare_authors",
    category="authors",
    description=(
        "Сравнение двух авторов: топ фирменных слов каждого, пересечение, "
        "cosine similarity affinity-векторов. Для «сравни X и Y»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author1_regex":    {"type": "string"},
            "author2_regex":    {"type": "string"},
            "top":              {"type": "integer", "description": "default 20"},
            "min_corpus_count": {"type": "integer", "description": "default 500"},
        },
        "required": ["author1_regex", "author2_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
)
def compare_authors(author1_regex: str, author2_regex: str, top: int = 20,
                    min_corpus_count: int = 500) -> ToolResult:
    try:
        from scripts.rag_tools import compare_authors as _v1
    except ImportError as e:
        return ToolResult.fail(tool="compare_authors", err_type="internal",
                               message=f"v1 unavailable: {e}")

    raw = _v1(author1_regex=author1_regex, author2_regex=author2_regex,
              top=top, min_corpus_count=min_corpus_count)
    query = {"author1_regex": author1_regex, "author2_regex": author2_regex,
             "top": top, "min_corpus_count": min_corpus_count}

    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        # Surface the "no matching books" case as not_found, so the renderer
        # can suggest alternatives rather than treating it as a hard failure.
        return ToolResult.fail(
            tool="compare_authors",
            err_type=("not_found" if "no books" in err.lower() or "not produced" in err.lower()
                      else "internal"),
            message=err, query=query,
        )

    # If either author's top list is empty, flag — that's the v1.1.7 partial.
    warnings: list[ToolWarning] = []
    if isinstance(raw, dict):
        for label, key in (("author1", "top_unique_a"), ("author2", "top_unique_b")):
            if not raw.get(key):
                warnings.append(ToolWarning(
                    code=f"{label}_empty",
                    message=f"{label} produced no signature words — "
                            f"check if author exists in SPGC corpus",
                ))

    return ToolResult.success(
        tool="compare_authors", data=raw,
        coverage=Coverage(
            books_matched=raw.get("books_a", -1) + raw.get("books_b", -1)
                          if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
