"""Entity resolvers — single delegation to `entity_resolver`.

Two @tool wrappers that the LLM planner composes into plans:

    resolve_author_name(query: str) → {author_regex, canonical, confidence, candidates}
    resolve_book_title(query: str)  → {pg_id, title, author, confidence, candidates}

They turn ambiguous, possibly mistyped, possibly Cyrillic user phrasings
into the canonical inputs that downstream tools (`affinity_by_author`,
`book_readability`, etc.) expect.

Phase 1 (2026-05-22) — the legacy off-flag layered branch
(AUTHOR_ALIASES + _fuzzy_author_candidates inline) was deleted along
with the WC_V5_RESOLVER gate. Authors go through v6 (via
`entity_resolver.resolve_author` adapter) and books go through the
v5 book resolver (no v6 book linker yet).

Fields emitted:
  - author_regex / pg_id+title (canonical for downstream tools)
  - canonical, confidence, candidates
  - normalization_trace (list of normalization steps for trace/debug)
  - data.view: typed RenderableView for the answer (NOT_FOUND / CLARIFY
    when applicable)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.meta.resolve_entity")


# ---------- resolve_author_name ----------


@tool(
    name="resolve_author_name",
    category="search",
    description=(
        "Резолвит свободную формулировку имени автора («Конан Дойль», "
        "«Doyle», «у Достоевского», «john milton») в канонический "
        "author_regex для downstream tools (affinity_by_author, "
        "compare_authors). Внутри — v6 layered linker (mention detection "
        "+ multi-factor scoring + decision thresholds)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                       "description": "user phrasing of an author name"},
        },
        "required": ["query"],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
)
def resolve_author_name(query: str) -> ToolResult:
    return _resolve_author(query)


# ---------- resolve_book_title ----------


@tool(
    name="resolve_book_title",
    category="search",
    description=(
        "Резолвит свободную формулировку названия книги («Beowulf», "
        "«Pride and Prejudice», «Преступление и наказание») в "
        "канонический PG id. Внутри — KNOWN_BOOKS + RU→EN title alias map "
        "+ v1 find_book + downloads-based ranking."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query":  {"type": "string"},
            "author": {"type": "string",
                       "description": "optional author hint for disambiguation"},
        },
        "required": ["query"],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
)
def resolve_book_title(query: str, author: str = "") -> ToolResult:
    return _resolve_book(query, author_hint=author)


# =====================================================================
# Delegations to scripts.v2.entity_resolver
# =====================================================================


def _resolve_author(query: str) -> ToolResult:
    """Delegate to entity_resolver.resolve_author. Emits both legacy
    fields (for downstream tools that read `data.author_regex`) AND a
    typed view (for view-aware renderers)."""
    from scripts.v2 import entity_resolver as er
    from scripts.v2 import view_builders as vb
    from scripts.v2.view_types import DataValidity

    if not (query or "").strip():
        return ToolResult.fail(
            tool="resolve_author_name", err_type="invalid_args",
            message="query is required",
        )

    res = er.resolve_author(query)

    if res.decision == "not_found":
        out = ToolResult.fail(
            tool="resolve_author_name", err_type="not_found",
            message=f"no author matched query {query!r}",
            details={
                "query": query,
                "normalization_trace": res.normalization_trace,
                "confidence_reason": res.confidence_reason,
            },
        )
        # Even on fail, attach a view so callers can show
        # candidates ("did you mean") instead of a blank error.
        try:
            vb.attach_view(out, vb.build_not_found(
                entity_type="author",
                query=query,
                message_ru="Не нашёл автора. Уточни написание или попробуй "
                           "канонический англ. вариант (Doyle / Hugo / Tolstoy).",
                candidates=[c.to_dict() for c in res.candidates[:5]],
            ), data_validity=DataValidity.EMPTY_UNEXPECTED)
        except Exception as e:
            log.debug("attach_view (not_found) failed: %s", e)
        return out

    if res.decision == "clarify_needed":
        # Tool returns ok=True with a clarify view + low confidence.
        # Downstream callers check confidence < threshold; renderer
        # consumes the view.
        out = ToolResult.success(
            tool="resolve_author_name",
            data={
                "author_regex": (res.candidates[0].key if res.candidates else None),
                "canonical": (res.candidates[0].display if res.candidates else ""),
                "confidence": res.confidence,
                "source": "v6_resolver",
                "candidates": [c.to_dict() for c in res.candidates],
                "normalization_trace": res.normalization_trace,
                "decision": "clarify_needed",
                "confidence_reason": res.confidence_reason,
                "query": query,
            },
            warnings=[ToolWarning(
                code="ambiguous",
                message=res.confidence_reason or "low confidence resolve",
            )],
            coverage=Coverage(books_matched=-1, books_total=-1),
            query={"query": query},
        )
        try:
            alts = []
            for c in res.candidates[:5]:
                alts.append(
                    f"{c.display} (загрузок: {c.prominence:,}, "
                    f"книг: {c.books_in_corpus})".replace(",", " ")
                )
            vb.attach_view(out, vb.build_clarify(
                question_ru=f"Несколько авторов подходят под «{query}». Кого из них?",
                alternatives=alts,
                why=res.confidence_reason,
            ), data_validity=DataValidity.PARTIAL)
        except Exception as e:
            log.debug("attach_view (clarify) failed: %s", e)
        return out

    # decision == "resolved"
    resolved = res.resolved or {}
    out = ToolResult.success(
        tool="resolve_author_name",
        data={
            "author_regex": resolved.get("author_regex"),
            "canonical": resolved.get("display"),
            "confidence": res.confidence,
            "source": f"v6/{resolved.get('source', 'unknown')}",
            "prominence": resolved.get("prominence", 0),
            "books_in_corpus": resolved.get("books_in_corpus", 0),
            "candidates": [c.to_dict() for c in res.candidates],
            "normalization_trace": res.normalization_trace,
            "decision": "resolved",
            "confidence_reason": res.confidence_reason,
            "query": query,
        },
        coverage=Coverage(books_matched=-1, books_total=-1),
        query={"query": query},
    )
    # Resolved view goes alongside legacy data fields — used by composite
    # views; standalone callers can ignore.
    try:
        vb.attach_view(out, vb.build_top_n_table(
            rows=[{
                "rank": 1,
                "display": resolved.get("display"),
                "regex": resolved.get("author_regex"),
                "downloads": resolved.get("prominence", 0),
                "books": resolved.get("books_in_corpus", 0),
                "confidence": f"{res.confidence:.2f}",
            }],
            columns=["rank", "display", "regex", "downloads", "books", "confidence"],
            headline=f"Резолв «{query}»",
            requested_n=1,
            caveats=res.normalization_trace,
            language="ru",
        ), data_validity=DataValidity.OK)
    except Exception as e:
        log.debug("attach_view (resolved) failed: %s", e)
    return out


def _resolve_book(query: str, *, author_hint: str = "") -> ToolResult:
    """Delegate to entity_resolver.resolve_book. Same shape as
    _resolve_author — legacy fields preserved, typed view added."""
    from scripts.v2 import entity_resolver as er
    from scripts.v2 import view_builders as vb
    from scripts.v2.view_types import DataValidity

    if not (query or "").strip():
        return ToolResult.fail(
            tool="resolve_book_title", err_type="invalid_args",
            message="query is required",
        )

    res = er.resolve_book(query, author_hint=author_hint)

    if res.decision == "not_found":
        out = ToolResult.fail(
            tool="resolve_book_title", err_type="not_found",
            message=f"no book matched {query!r}",
            details={
                "query": query, "author": author_hint or None,
                "normalization_trace": res.normalization_trace,
            },
        )
        try:
            vb.attach_view(out, vb.build_not_found(
                entity_type="book",
                query=query,
                message_ru="Не нашёл книгу. Если она в копирайте — "
                           "загрузи свою копию через /admin/.",
                candidates=[c.to_dict() for c in res.candidates[:5]],
            ), data_validity=DataValidity.EMPTY_UNEXPECTED)
        except Exception as e:
            log.debug("attach_view (book not_found) failed: %s", e)
        return out

    if res.decision == "clarify_needed":
        out = ToolResult.success(
            tool="resolve_book_title",
            data={
                "pg_id": (res.candidates[0].key if res.candidates else None),
                "title": (res.candidates[0].display if res.candidates else ""),
                "confidence": res.confidence,
                "source": "v5_resolver",
                "candidates": [c.to_dict() for c in res.candidates],
                "normalization_trace": res.normalization_trace,
                "decision": "clarify_needed",
                "confidence_reason": res.confidence_reason,
                "query": query,
            },
            warnings=[ToolWarning(
                code="ambiguous",
                message=res.confidence_reason or "low confidence resolve",
            )],
            coverage=Coverage(books_matched=len(res.candidates),
                              books_total=len(res.candidates)),
            query={"query": query, "author": author_hint or None},
        )
        try:
            alts = []
            for c in res.candidates[:5]:
                a = c.extra.get("author", "") if isinstance(c.extra, dict) else ""
                bits = [c.display, f"({c.key})"]
                if a:
                    bits.append(f"— {a}")
                if c.prominence:
                    bits.append(f"загрузок: {c.prominence:,}".replace(",", " "))
                alts.append(" ".join(bits))
            vb.attach_view(out, vb.build_clarify(
                question_ru=f"Несколько книг подходят под «{query}». Какую?",
                alternatives=alts,
                why=res.confidence_reason,
            ), data_validity=DataValidity.PARTIAL)
        except Exception as e:
            log.debug("attach_view (book clarify) failed: %s", e)
        return out

    # decision == "resolved"
    resolved = res.resolved or {}
    out = ToolResult.success(
        tool="resolve_book_title",
        data={
            "pg_id": resolved.get("pg_id"),
            "title": resolved.get("title"),
            "author": resolved.get("author"),
            "confidence": res.confidence,
            "source": f"v5/{resolved.get('source', 'unknown')}",
            "candidates": [c.to_dict() for c in res.candidates],
            "normalization_trace": res.normalization_trace,
            "decision": "resolved",
            "confidence_reason": res.confidence_reason,
            "query": query,
        },
        coverage=Coverage(books_matched=1, books_total=1),
        warnings=([] if resolved.get("pg_id") else [ToolWarning(
            code="copyright",
            message=f"{resolved.get('title')} resolved but no PG id (copyright)",
        )]),
        query={"query": query, "author": author_hint or None},
    )
    try:
        vb.attach_view(out, vb.build_book_lookup(
            book={
                "pg_id": resolved.get("pg_id"),
                "title": resolved.get("title"),
                "author": resolved.get("author"),
            },
            caveats=res.normalization_trace,
            language="ru",
        ), data_validity=DataValidity.OK)
    except Exception as e:
        log.debug("attach_view (book resolved) failed: %s", e)
    return out


__all__ = ["resolve_author_name", "resolve_book_title"]
