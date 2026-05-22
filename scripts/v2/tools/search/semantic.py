"""v2 semantic_search — ChromaDB embedding lookup."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1SemanticSearch


@tool(
    name="semantic_search",
    category="search",
    description=(
        "Семантический поиск по корпусу через ChromaDB (multilingual MiniLM-L12). "
        "Для «найди упоминания X», «где описывается Y». Возвращает chunks с PG-ссылками. "
        "Автоматически переводит RU-запросы через qwen перед embedding."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query":         {"type": "string"},
            "k":             {"type": "integer", "description": "default 8"},
            "author_filter": {"type": "string",
                              "description": "Optional regex по author, e.g. '^Dostoyevsky,'"},
        },
        "required": ["query"],
    },
    requires=[],
    cost="medium",
    cacheable=True,
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.semantic_search",
             schema=V1SemanticSearch)
def semantic_search(query: str, k: int = 8,
                    author_filter: str | None = None) -> ToolResult:
    from scripts.rag_tools import semantic_search as _v1
    raw = _v1(query=query, k=k, author_filter=author_filter)
    qq = {"query": query, "k": k, "author_filter": author_filter}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="semantic_search", err_type="internal",
                               message=str(raw["error"]), query=qq)
    rows = (raw.get("results") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="semantic_search", data=raw,
        coverage=Coverage(books_matched=len({r.get("metadata", {}).get("pg_id")
                                             for r in rows if r}),
                          books_total=-1),
        warnings=[ToolWarning("no_results", "ChromaDB returned 0 chunks")]
                 if not rows else [],
        query=qq,
    )
