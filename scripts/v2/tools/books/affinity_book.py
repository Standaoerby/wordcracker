"""v2 affinity_by_book — book-scoped signature words.

Lives in scripts/learning_tools.py in v1 (because the original implementation
was part of the learning pipeline). v2 wraps it under category='books' so the
intent router can find it under the book taxonomy.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.tools.authors._surname_filter import filter_surnames
from scripts.v2.tools.authors._corpus_artifacts import filter_corpus_artifacts


@tool(
    name="affinity_by_book",
    category="books",
    description=(
        "Фирменные слова конкретной книги по affinity (частота в книге vs корпус). "
        "ВСЕГДА после find_book — никогда не угадывай PG id из памяти."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pg_id":               {"type": "string"},
            "top":                 {"type": "integer", "description": "default 50"},
            "pos_filter":          {"type": "array", "items": {"type": "string"}},
            "min_corpus_count":    {"type": "integer", "description": "default 200"},
            "exclude_proper_nouns": {"type": "boolean", "description": "default true"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="medium",
    cacheable=True,
)
def affinity_by_book(pg_id: str, top: int = 50,
                     pos_filter: list[str] | None = None,
                     min_corpus_count: int = 200,
                     exclude_proper_nouns: bool = True) -> ToolResult:
    try:
        from scripts.learning_tools import affinity_by_book as _v1
    except ImportError as e:
        return ToolResult.fail(tool="affinity_by_book", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id, top=top, pos_filter=pos_filter,
              min_corpus_count=min_corpus_count,
              exclude_proper_nouns=exclude_proper_nouns)
    query = {"pg_id": pg_id, "top": top, "pos_filter": pos_filter,
             "min_corpus_count": min_corpus_count,
             "exclude_proper_nouns": exclude_proper_nouns}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="affinity_by_book",
            err_type=("not_found" if "no counts" in err.lower() or "no token" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    rows = (raw.get("top_words") if isinstance(raw, dict) else None) or []
    # Sprint 19+ — surname blocklist (see authors._surname_filter docstring).
    # Same defence layer as affinity_by_author: PG-metadata surnames +
    # curated literary characters. Stan 2026-05-19 «фамилии не должны
    # участвовать в аффинити индексе».
    if rows:
        rows, surname_dropped = filter_surnames(rows)
        rows, artifact_dropped = filter_corpus_artifacts(rows)
        if isinstance(raw, dict):
            raw["top_words"] = rows
            notes = []
            if surname_dropped:
                notes.append(f"v2 surname filter dropped {surname_dropped} "
                              f"character/author names")
            if artifact_dropped:
                notes.append(f"v2 corpus-artifact filter dropped "
                              f"{artifact_dropped} markup tokens (e.g. xvth)")
            if notes:
                prev = raw.get("_render_note", "")
                raw["_render_note"] = (prev + " " + "; ".join(notes)).strip()
    # Sprint 20 — count-honesty signal (mirrors affinity_by_author).
    # When filtering knocks the list below `top`, surface the delta
    # so the renderer doesn't claim the requested count.
    actual = len(rows) if rows else 0
    if isinstance(raw, dict):
        raw["top_requested"] = top
        raw["top_returned"] = actual
        if actual < top:
            existing = raw.get("_render_note") or ""
            count_note = (
                f"ACTUAL COUNT: tool returned {actual} words after "
                f"PROPN / surname / corpus filtering — NOT the {top} "
                f"requested. Use {actual} in the answer."
            )
            raw["_render_note"] = (
                (existing + " | " if existing else "") + count_note
            )
    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            code="empty_top", message="no signature words above min_corpus_count",
        ))
    elif actual < top:
        warnings.append(ToolWarning(
            code="under_filled",
            message=(f"requested top={top}, returned {actual} after "
                      f"filtering — renderer must say {actual}"),
        ))
    result = ToolResult.success(
        tool="affinity_by_book", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        warnings=warnings, query=query,
    )

    # v5 Phase 2.5 — TOP_N_TABLE view emission. Same pattern as
    # affinity_by_author. Book scope means we put the PG id in headline.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return result
        title = raw.get("book_title") or raw.get("title") or pg_id
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "affinity"],
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=f"Нет фирменных слов для {title} при min_corpus_count={min_corpus_count}.",
                empty_message_en=f"No signature words for {title}.",
                empty_filters_applied={"min_corpus_count": min_corpus_count,
                                        "pos_filter": pos_filter},
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            view_rows.append({
                "rank": i,
                "word": r.get("word") or r.get("token") or "—",
                "affinity": (f"{r.get('affinity'):.3f}"
                              if isinstance(r.get("affinity"), (int, float))
                              else "—"),
            })
        view_caveats = []
        if raw.get("proper_noun_filter"):
            view_caveats.append(raw["proper_noun_filter"])
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "affinity"],
            headline=f"Фирменные слова — {title} ({pg_id})",
            requested_n=top,
            caveats=view_caveats,
            provenance=vb.make_provenance(
                requested={"pg_id": pg_id, "top": top,
                           "min_corpus_count": min_corpus_count,
                           "pos_filter": pos_filter},
                returned={"count": len(view_rows)},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.books.affinity_book").warning(
            "affinity_by_book view emission failed: %s", e,
        )
    return result
