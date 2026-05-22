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
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1AffinityByBook


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
    # E17 CACHE INVALIDATION (2026-05-22) — Stan prod proved retry-helper
    # was never running: empty cached entry from PRE-E14b deploys hit
    # cache_key (wrapper_version="v1") and returned without invoking the
    # new retry-step-down logic. Bumping wrapper_version invalidates all
    # stale empty entries; first request after deploy fully re-runs v1
    # with the retry helper.
    wrapper_version="v4-phase2-contract",
)
@v1_contract(v1_fn="learning_tools.affinity_by_book",
             schema=V1AffinityByBook)
def affinity_by_book(pg_id: str, top: int = 50,
                     pos_filter: list[str] | None = None,
                     min_corpus_count: int = 200,
                     exclude_proper_nouns: bool = True) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="affinity_by_book", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG1342')",
            query={"pg_id": pg_id},
        )
    from learning_tools import affinity_by_book as _v1

    # E14 architectural fix — shared retry-on-empty helper.
    # `compare_authors` had step-down chain (500→200→100→50) for Poe/
    # Lovecraft style rare-marker queries. Same UX issue for single-book
    # ADJ filter («характерные прилагательные в Dorian Gray» empty at
    # min_corpus_count=200). Lift to shared helper, apply here.
    # E14b ROOT CAUSE FIX (2026-05-22): v1 affinity_by_book returns
    # rows under key «top» (line 275 of learning_tools.py), NOT
    # «top_words». Old wrapper read wrong key → always empty → retry
    # chain triggered every time but couldn't find non-empty result
    # because is_empty_fn never returned False. Same class as B-R14-7
    # (learning_words «results» vs «words»). Read both keys; promote
    # the non-empty one to canonical «top_words» for downstream.
    from scripts.v2.tools._retry_on_empty import retry_with_lower_threshold
    raw = retry_with_lower_threshold(
        v1_fn=_v1,
        v1_args={
            "pg_id": pg_id, "top": top, "pos_filter": pos_filter,
            "min_corpus_count": min_corpus_count,
            "exclude_proper_nouns": exclude_proper_nouns,
        },
        threshold_arg="min_corpus_count",
        steps=(100, 50, 20, 10),
        # V1AffinityByBook canonical key is `top` (learning_tools.py:275).
        is_empty_fn=lambda r: not (r or {}).get("top"),
        min_initial=50,
    )
    query = {"pg_id": pg_id, "top": top, "pos_filter": pos_filter,
             "min_corpus_count": min_corpus_count,
             "exclude_proper_nouns": exclude_proper_nouns}
    if isinstance(raw, dict) and raw.get("min_corpus_count_used") is not None:
        query["min_corpus_count_used"] = raw["min_corpus_count_used"]
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="affinity_by_book",
            err_type=("not_found" if "no counts" in err.lower() or "no token" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    # Phase 2 — V1AffinityByBook canonical key is `top`.
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    # Sprint 19+ — surname blocklist (see authors._surname_filter docstring).
    # Same defence layer as affinity_by_author: PG-metadata surnames +
    # curated literary characters. Stan 2026-05-19 «фамилии не должны
    # участвовать в аффинити индексе».
    if rows:
        rows, surname_dropped = filter_surnames(rows)
        rows, artifact_dropped = filter_corpus_artifacts(rows)
        if isinstance(raw, dict):
            raw["top"] = rows
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
        # V1AffinityByBook canonical key is `title` (no `book_title`).
        title = raw.get("title") or pg_id
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
            # V1AffinityByBook rows: word, book_count, corpus_count, affinity.
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
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
