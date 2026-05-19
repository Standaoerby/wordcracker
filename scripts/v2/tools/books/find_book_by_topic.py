"""v2 find_book_by_topic — semantic book search by topic.

Sprint 16 Phase F.

Wraps `hybrid_search` and dedupes by pg_id so we return one entry per
BOOK (not per chunk). Useful for «найди книгу про викторианский Лондон»
/ «посоветуй роман о море» / «book about Victorian gas lamps» style
queries where the user wants candidate books, not specific passages.

Key differences from raw hybrid_search:
  - Deduplicates: best chunk per pg_id wins, others discarded
  - Returns book-shaped rows (pg_id, title, author, snippet, score)
  - Optional `rerank_with` for BGE cross-encoder pass (Phase B4)
  - `per_retriever` defaults higher (60) than hybrid_search's 30 so
    we have enough diversity to fill `top` unique books after dedup.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import dispatch as v2_dispatch, tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.find_book_by_topic")


@tool(
    name="find_book_by_topic",
    category="books",
    description=(
        "Семантический поиск книг по теме / topical match. «Найди книгу про "
        "викторианский Лондон», «book about gothic horror», «роман о море». "
        "Возвращает top-k уникальных книг с лучшим chunk-snippet и pg_id для "
        "цепочки в book-scoped tools. Не путать с find_book (title lookup)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic":         {"type": "string", "description": "topical query на любом языке"},
            "top":           {"type": "integer", "description": "сколько уникальных книг (default 8)"},
            "per_retriever": {"type": "integer", "description": "k for each retriever before dedup (default 60)"},
            "author_filter": {"type": "string", "description": "опциональный regex для фильтра автора"},
            "rerank_with":   {"type": "string", "description": "опциональный плагин из scoring.REGISTRY, e.g. 'bge_reranker'"},
        },
        "required": ["topic"],
    },
    requires=[],
    cost="medium",
    cacheable=True,
)
def find_book_by_topic(
    topic: str,
    top: int = 8,
    per_retriever: int = 60,
    author_filter: str | None = None,
    rerank_with: str | None = None,
) -> ToolResult:
    if not topic or not topic.strip():
        return ToolResult.fail(
            tool="find_book_by_topic", err_type="invalid_args",
            message="topic is required",
            query={"topic": topic},
        )

    # Delegate to hybrid_search — it handles RRF + optional rerank.
    # We always pull more chunks than `top` to leave room for dedup.
    sub_args: dict[str, Any] = {
        "query": topic,
        "k": max(top * 3, 30),       # enough headroom for dedup
        "per_retriever": per_retriever,
    }
    if author_filter:
        sub_args["author_filter"] = author_filter
    if rerank_with:
        sub_args["rerank_with"] = rerank_with

    sub = v2_dispatch("hybrid_search", sub_args)
    query = {"topic": topic, "top": top, "per_retriever": per_retriever,
             "author_filter": author_filter, "rerank_with": rerank_with}

    if not sub.ok:
        return ToolResult.fail(
            tool="find_book_by_topic",
            err_type=(sub.error.type if sub.error else "internal"),
            message=(sub.error.message if sub.error
                     else "hybrid_search failed"),
            query=query,
        )

    chunks = sub.data.get("matches", []) if isinstance(sub.data, dict) else []
    if not chunks:
        return ToolResult(
            ok=False, tool="find_book_by_topic", query=query,
            data={"matches": [], "total_chunks_seen": 0,
                  "books_returned": 0},
            warnings=[ToolWarning(
                code="no_topical_matches",
                message=f"no books matched topic {topic!r}",
            )],
            coverage=Coverage(books_matched=0, books_total=-1),
            error=None,
        )

    # Dedup by pg_id — keep the best-scored chunk per book. Chunks are
    # already ordered by rrf_score (and rerank_score if reranker ran).
    seen: dict[str, dict] = {}
    for ch in chunks:
        pg = ch.get("pg_id")
        if not pg or pg in seen:
            continue
        seen[pg] = {
            "pg_id":         pg,
            "title":         ch.get("title"),
            "author":        ch.get("author"),
            "rrf_score":     ch.get("rrf_score"),
            "rerank_score":  ch.get("rerank_score"),
            "lexical_rank":  ch.get("lexical_rank"),
            "semantic_rank": ch.get("semantic_rank"),
            "snippet":       ch.get("snippet"),
        }
        if len(seen) >= top:
            break

    books = list(seen.values())
    warnings = list(sub.warnings) if sub.warnings else []
    if len(books) < top:
        warnings.append(ToolWarning(
            code="few_unique_books",
            message=f"only {len(books)} unique books in top-{len(chunks)} chunks; "
                    f"increase per_retriever to {per_retriever * 2}",
        ))

    return ToolResult.success(
        tool="find_book_by_topic",
        data={
            "topic":              topic,
            "matches":            books,
            "books_returned":     len(books),
            "total_chunks_seen":  len(chunks),
            "reranked_by":        sub.data.get("reranked_by"),
            # convenience for chaining into book-scoped tools
            "first_id":           books[0]["pg_id"] if books else None,
        },
        warnings=warnings,
        coverage=Coverage(books_matched=len(books), books_total=-1),
        query=query,
    )
