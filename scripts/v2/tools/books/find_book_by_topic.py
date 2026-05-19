"""v2 find_book_by_topic — semantic book search by topic.

Sprint 16 Phase F. Sprint 19+ patches:
  - min_rerank_score filter — drops irrelevant matches after BGE rerank
    (Stan 2026-05-19 «магическая школа» surfaced PG29178 Little Red
    Riding Hood at rerank_score ~0.12 alongside legit hits at 0.6+)
  - Force-translate Russian queries → English before semantic search.
    Cross-lingual MiniLM is OK but precision lifts when the query is
    in the corpus's native language.
  - rerank_score in renderer output via _render_note.

Wraps `hybrid_search` and dedupes by pg_id so we return one entry per
BOOK (not per chunk). Useful for «найди книгу про викторианский Лондон»
/ «посоветуй роман о море» / «book about Victorian gas lamps» style
queries where the user wants candidate books, not specific passages.
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


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _translate_topic(topic: str) -> tuple[str, str | None]:
    """Force-translate Cyrillic topics to English via v1 helper.

    Returns (topic_to_use, original_if_translated). When the input is
    already ASCII / English, returns (topic, None) and skips the
    network round-trip.
    """
    if not _has_cyrillic(topic):
        return topic, None
    try:
        from scripts.rag_tools import _maybe_translate
    except ImportError:
        return topic, None
    try:
        translated = _maybe_translate(topic.strip())
        if translated and translated.lower() != topic.strip().lower():
            return translated, topic
    except Exception:
        pass
    return topic, None


@tool(
    name="find_book_by_topic",
    category="books",
    description=(
        "Семантический поиск книг по теме / topical match. «Найди книгу про "
        "викторианский Лондон», «book about gothic horror», «роман о море». "
        "Возвращает top-k уникальных книг с лучшим chunk-snippet и pg_id для "
        "цепочки в book-scoped tools. Не путать с find_book (title lookup). "
        "Sprint 19+: BGE rerank threshold + RU→EN translation для precision."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic":             {"type": "string", "description": "topical query на любом языке"},
            "top":               {"type": "integer", "description": "сколько уникальных книг (default 8)"},
            "per_retriever":     {"type": "integer", "description": "k for each retriever before dedup (default 60)"},
            "author_filter":     {"type": "string",  "description": "опциональный regex для фильтра автора"},
            "rerank_with":       {"type": "string",  "description": "опциональный плагин из scoring.REGISTRY, e.g. 'bge_reranker'"},
            "min_rerank_score":  {"type": "number",  "description": "drop matches with rerank_score below this (default 0.4, BGE normalized)"},
            "translate":         {"type": "boolean", "description": "auto-translate RU topic→EN before semantic search (default true)"},
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
    min_rerank_score: float = 0.4,
    translate: bool = True,
) -> ToolResult:
    if not topic or not topic.strip():
        return ToolResult.fail(
            tool="find_book_by_topic", err_type="invalid_args",
            message="topic is required",
            query={"topic": topic},
        )

    # Sprint 19+ — RU→EN translation pass. Cross-lingual MiniLM works
    # but precision improves substantially when the query is in EN
    # (the corpus is EN, embeddings cluster around English semantics).
    original_topic = topic
    translated_from = None
    if translate:
        topic, translated_from = _translate_topic(topic)

    # Delegate to hybrid_search — it handles RRF + optional rerank.
    # We always pull more chunks than `top` to leave room for dedup
    # AND for the rerank-threshold filter below.
    sub_args: dict[str, Any] = {
        "query": topic,
        "k": max(top * 4, 40),       # extra headroom for threshold drops
        "per_retriever": per_retriever,
    }
    if author_filter:
        sub_args["author_filter"] = author_filter
    if rerank_with:
        sub_args["rerank_with"] = rerank_with

    sub = v2_dispatch("hybrid_search", sub_args)
    query = {"topic": original_topic, "top": top, "per_retriever": per_retriever,
             "author_filter": author_filter, "rerank_with": rerank_with,
             "min_rerank_score": min_rerank_score, "translate": translate,
             "translated_from": translated_from}

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

    # Sprint 19+ — rerank-score threshold filter. BGE cross-encoder
    # gives a normalized 0-1 relevance score; <0.4 is typically noise
    # (Stan's «магическая школа» query landed Little Red Riding Hood
    # at ~0.12 because semantic search caught «school» without
    # «magic» context). Skip the filter when rerank didn't run.
    reranked_by = sub.data.get("reranked_by")
    if reranked_by and min_rerank_score > 0:
        chunks = [
            ch for ch in chunks
            if not isinstance(ch.get("rerank_score"), (int, float))
            or ch["rerank_score"] >= min_rerank_score
        ]

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
            message=f"only {len(books)} unique books survived threshold "
                    f"(min_rerank_score={min_rerank_score}); "
                    f"lower threshold or increase per_retriever",
        ))

    # Sprint 19+ — render hint: show rerank_score in the answer table
    # so the user sees confidence per row. Plus disclose translation
    # if it happened so the user can adjust phrasing.
    notes: list[str] = []
    if reranked_by:
        notes.append(
            "В таблице вывода ОБЯЗАТЕЛЬНО покажи колонку `rerank_score` "
            f"(0-1, BGE cross-encoder). Threshold отбора был "
            f"min_rerank_score={min_rerank_score} — все оставшиеся "
            "строки выше этого порога. Это сигнал доверия модели."
        )
    if translated_from:
        notes.append(
            f"Запрос пользователя был на русском («{translated_from}»), "
            f"переведён в semantic search как «{topic}». Сообщи это "
            "пользователю одной строкой («поиск выполнялся по "
            f"английскому запросу «{topic}» — корпус EN»)."
        )

    return ToolResult.success(
        tool="find_book_by_topic",
        data={
            "topic":               original_topic,
            "topic_searched_as":   topic,
            "translated_from":     translated_from,
            "matches":             books,
            "books_returned":      len(books),
            "total_chunks_seen":   sub.data.get("matches", []) and len(sub.data["matches"]),
            "reranked_by":         reranked_by,
            "min_rerank_score":    min_rerank_score if reranked_by else None,
            # convenience for chaining into book-scoped tools
            "first_id":            books[0]["pg_id"] if books else None,
            "_render_note":        " ".join(notes) if notes else None,
        },
        warnings=warnings,
        coverage=Coverage(books_matched=len(books), books_total=-1),
        query=query,
    )
