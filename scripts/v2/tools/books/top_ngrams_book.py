"""v2 top_ngrams_by_book — book-scoped RAW frequency words / n-grams.

R-29 S1 / bug A — the honest book-scoped counterpart to
`top_ngrams_by_author`. «самые частотные слова в "Dracula"» returns the
most frequent words IN THE BOOK (raw counts), NOT an author aggregate over
~10 books and NOT affinity. For corpus-relative «фирменные/характерные»
words of a book, use `affinity_by_book` instead — the label here is strictly
«частотные слова в [книга]», never affinity-as-frequency.

v1 lives in scripts/rag_tools.py (`top_ngrams_by_book`).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1TopNgramsByBook


@tool(
    name="top_ngrams_by_book",
    category="books",
    description=(
        "Самые частотные слова / N-граммы В КОНКРЕТНОЙ КНИГЕ — сырые частоты "
        "(raw count), не affinity. n=1 слова, n=2 биграммы, n=3 триграммы. "
        "Используй для «частотные слова в [книга]», «топ слов романа X». "
        "ВСЕГДА после find_book — никогда не угадывай PG id из памяти. "
        "Для «фирменных/характерных» слов книги (affinity vs корпус) бери "
        "affinity_by_book — это РАЗНЫЕ метрики."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pg_id":      {"type": "string"},
            "n":          {"type": "integer", "description": "1, 2 или 3"},
            "top":        {"type": "integer", "description": "default 20"},
            "pos_filter": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="medium",
    cacheable=True,
    wrapper_version="v1",
)
@v1_contract(v1_fn="scripts.rag_tools.top_ngrams_by_book",
             schema=V1TopNgramsByBook)
def top_ngrams_by_book(pg_id: str, n: int = 1, top: int = 20,
                       pos_filter=None) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="top_ngrams_by_book", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG345')",
            query={"pg_id": pg_id, "n": n, "top": top},
        )
    from scripts.rag_tools import top_ngrams_by_book as _v1
    raw = _v1(pg_id=pg_id, n=n, top=top, pos_filter=pos_filter)
    query = {"pg_id": pg_id, "n": n, "top": top, "pos_filter": pos_filter}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="top_ngrams_by_book",
            err_type=("not_found" if "not found" in err.lower() else "internal"),
            message=err, query=query,
        )
    # V1TopNgramsByBook canonical rows key is `top` (row shape ngram/count).
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []

    # Honest-label signal (affinity-label-чек): this is RAW FREQUENCY in ONE
    # book — not corpus-relative affinity and not an author aggregate. Stamp a
    # render note so the LLM labels it «частотные слова в [книга]», never
    # «характерные/фирменные» (which belong to affinity_by_book).
    if isinstance(raw, dict):
        title = raw.get("title") or pg_id
        raw["_render_note"] = (
            f"RAW FREQUENCY in book {title} ({raw.get('pg_id') or pg_id}) — "
            f"these are the most frequent words/n-grams IN THIS BOOK "
            f"(stopwords filtered), NOT corpus-relative affinity and NOT an "
            f"author aggregate. Label honestly: «частотные слова в {title}»."
        )

    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            code="empty_top",
            message="no n-grams matched filters in this book",
        ))

    result = ToolResult.success(
        tool="top_ngrams_by_book", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        warnings=warnings, query=query,
    )
    _attach_top_ngrams_book_view(result, raw, rows, pg_id, n, top)
    return result


# =====================================================================
# view emission helper (separate fn — kept out of the @v1_contract AST
# surface, mirrors authors/top_ngrams.py::_attach_top_ngrams_view)
# =====================================================================


def _attach_top_ngrams_book_view(result, raw, rows, pg_id: str,
                                  n: int, top: int) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        title = (raw.get("title") if isinstance(raw, dict) else None) or pg_id
        ngram_kind = {1: "Частотные слова", 2: "Частотные биграммы",
                      3: "Частотные триграммы"}.get(n, f"Частотные {n}-граммы")
        # Honest headline: frequency IN the book, never «фирменные» (affinity).
        headline = f"{ngram_kind} — {title} ({pg_id})"
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "ngram", "count"],
                headline=headline,
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=(f"Для {title} не нашлось "
                                  f"{ngram_kind.lower()} при текущих фильтрах."),
                empty_message_en="No ngrams matched filters in this book.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1TopNgramsByBook row_keys: ngram, count.
            view_rows.append({
                "rank":  i,
                "ngram": r.get("ngram") or "—",
                "count": r.get("count") or "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "ngram", "count"],
            headline=headline,
            requested_n=top,
            provenance=vb.make_provenance(
                requested={"pg_id": pg_id, "n": n, "top": top},
                returned={"count": len(view_rows)},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger(
            "wordcracker.v2.tools.books.top_ngrams_book"
        ).exception("top_ngrams_by_book view emission failed")
