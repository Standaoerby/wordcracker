"""Similarity-router plan builder.

`similar_to` — ambiguous «в стиле X» that resolves to either
`book_similar` or `author_closest` depending on which entity slot
the extractor filled.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import QueryPlan
from scripts.v2.planner.builders.author import _plan_author_closest
from scripts.v2.planner.builders.book import _plan_book_similar


def _plan_similar_to(e: Entities) -> QueryPlan:
    """«в стиле X» / «типа X» — X may be a book OR an author. Resolve
    by which entity slot the extractor filled, then delegate to the
    appropriate concrete plan. Mostly hits when neither book_similar
    nor author_closest's specific rules matched first (priority 130
    keeps it below those)."""
    if e.book_id or e.book_title:
        return _plan_book_similar(e)
    if e.author_regex:
        # Reuse author_closest semantics — «похож по стилю» = closest
        # stylistic neighbours via author_influences/Burrows Delta.
        return _plan_author_closest(e)
    # Neither resolved → clarify with both-options hint
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            "«В стиле X» — X это книга или автор? Уточни:\n"
            "• «в стиле книги "
            "«Pride and Prejudice»» → похожие книги\n"
            "• «в стиле автора Doyle» → похожие авторы по Burrows Delta"
        ),
        explain="similar_to — entity not resolved",
    )
