"""v2 top_books_by_downloads + top_books_by_recency + book_emotion_profile."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1TopBooksByDownloads, V1TopBooksByRecency, V1BookEmotionProfile,
)


@tool(
    name="top_books_by_downloads",
    category="books",
    description=(
        "Топ-N самых скачиваемых книг. Опциональный author_regex для one-author top."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string"},
        },
        "required": [],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.top_books_by_downloads",
             schema=V1TopBooksByDownloads)
def top_books_by_downloads(top: int = 20, lang: str = "en",
                           author_regex: str | None = None) -> ToolResult:
    from scripts.rag_tools import top_books_by_downloads as _v1
    raw = _v1(top=top, lang=lang, author_regex=author_regex)
    query = {"top": top, "lang": lang, "author_regex": author_regex}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="top_books_by_downloads",
                               err_type="not_found",
                               message=str(raw["error"]), query=query)
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    result = ToolResult.success(
        tool="top_books_by_downloads", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )
    _attach_top_books_view(result, rows, metric="downloads", top=top,
                            author_regex=author_regex)
    return result


@tool(
    name="top_books_by_recency",
    category="books",
    description=(
        "Топ-N свежих книг. metric='pg_id' (date added to PG, default) | "
        "'pub_year' (real publication year via Open Library enrichment)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string"},
            "metric":       {"type": "string",
                             "enum": ["pg_id", "pub_year"],
                             "description": "default 'pg_id'"},
        },
        "required": [],
    },
    requires=[],
    cost="cheap",
    cacheable=True,
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.top_books_by_recency",
             schema=V1TopBooksByRecency)
def top_books_by_recency(top: int = 20, lang: str = "en",
                         author_regex: str | None = None,
                         metric: str = "pg_id") -> ToolResult:
    from scripts.rag_tools import top_books_by_recency as _v1
    raw = _v1(top=top, lang=lang, author_regex=author_regex, metric=metric)
    query = {"top": top, "lang": lang, "author_regex": author_regex,
             "metric": metric}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        # pub_year metric can fall back when batch isn't done — that's a
        # warning, not a hard error: surface to caller as not_found so the
        # planner can suggest the fallback metric.
        return ToolResult.fail(tool="top_books_by_recency", err_type="not_found",
                               message=err, query=query)
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    result = ToolResult.success(
        tool="top_books_by_recency", data=raw,
        coverage=Coverage(books_matched=len(rows), books_total=-1),
        query=query,
    )
    _attach_top_books_view(result, rows, metric=metric, top=top,
                            author_regex=author_regex)
    return result


@tool(
    name="book_emotion_profile",
    category="books",
    description=(
        "Эмоциональный профиль книги через NRC: counts per emotion "
        "(fear/joy/anger/sadness/anticipation/disgust/surprise/trust)."
    ),
    input_schema={
        "type": "object",
        "properties": {"pg_id": {"type": "string"}},
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="cheap",
    cacheable=True,
    # W-3 (2026-05-24) — wrapper now also stamps `raw["emotions"]` as
    # a flat list-of-rows so the LLM render payload no longer has to
    # join `share_among_primary_emotions` × `per_million` mentally
    # (that join failing was the «Вхождений» = «—» symptom). Bumping
    # the version invalidates cached results from v4 which lacked the
    # `emotions` key.
    wrapper_version="v5-w3-emotion-rows",
)
@v1_contract(v1_fn="scripts.rag_tools.book_emotion_profile",
             schema=V1BookEmotionProfile)
def book_emotion_profile(pg_id: str) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="book_emotion_profile", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG84')",
            query={"pg_id": pg_id},
        )
    from scripts.rag_tools import book_emotion_profile as _v1
    raw = _v1(pg_id=pg_id)
    query = {"pg_id": pg_id}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_emotion_profile", err_type="not_found",
                               message=str(raw["error"]), query=query)
    result = ToolResult.success(
        tool="book_emotion_profile", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )
    _attach_emotion_profile_view(result, raw, pg_id)
    return result


# =====================================================================
# v5 Phase 2.5 — view emission helpers
# =====================================================================


def _attach_top_books_view(result, rows, *, metric: str, top: int,
                             author_regex: str | None = None) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        headline_parts = [f"Топ книг по {metric}"]
        if author_regex:
            headline_parts.append(
                f"автор {author_regex.lstrip('^').rstrip(',').strip()}"
            )
        headline = " — ".join(headline_parts)

        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "title", "author", metric],
                headline=headline,
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru="В корпусе нет книг по запросу.",
                empty_message_en="No matching books.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return

        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1TopBooksByDownloads / V1TopBooksByRecency row_keys:
            # id, title, author, downloads, author_birth, pub_year, pg_id.
            v = r.get(metric)
            if v is None:
                v = r.get("downloads") if metric != "pub_year" else r.get("pub_year")
            view_rows.append({
                "rank": i,
                "title": r.get("title") or "—",
                "author": r.get("author") or "—",
                metric: v if v is not None else "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "title", "author", metric],
            headline=headline,
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.books.top_books").exception(
            "top_books view emission failed"
        )


def _build_emotion_rows(raw: dict) -> list[dict]:
    """Project v1's dict-of-dicts (`share_among_primary_emotions`,
    `per_million`) into a flat list of typed rows the renderer template
    and the LLM render payload can both read.

    W-3 (2026-05-24): the LLM-render path used to receive only the raw
    dict-of-dicts shape; the LLM had to mentally JOIN share + per_million
    per emotion to fill the «Вхождений» column. When the join failed —
    Stan persona Q4/Q12 «Эмоциональный профиль Frankenstein» — that
    column rendered with «—» in every cell. Exposing a normalized list
    of `{emotion, share, count, per_million}` rows removes the join
    burden: the LLM iterates rows, renders one cell per documented key.
    """
    share_raw = raw.get("share_among_primary_emotions")
    per_million_raw = raw.get("per_million")
    emotions_raw = share_raw or per_million_raw or {}
    emotions: list[dict] = []
    if not isinstance(emotions_raw, dict):
        return emotions
    values = [v for v in emotions_raw.values()
              if isinstance(v, (int, float))]
    total = sum(values) if values else 0
    already_shares = (0.95 <= total <= 1.05) and bool(share_raw)
    per_million_for: dict = (per_million_raw
                              if isinstance(per_million_raw, dict)
                              else {})
    for emo, val in emotions_raw.items():
        if not isinstance(val, (int, float)):
            continue
        pm_val = per_million_for.get(emo)
        if already_shares:
            share = float(val)
            # Show per-million frequency as count (more meaningful
            # than the share fraction) — E24 «Вхождений» column.
            count = (round(float(pm_val))
                      if isinstance(pm_val, (int, float)) else None)
        else:
            share = (float(val) / total) if total else 0.0
            count = (round(float(val))
                      if isinstance(val, (int, float)) else None)
        emotions.append({
            "emotion": emo,
            "share": share,
            "count": count,
            # Renderer-friendly alias so a column-name guess of
            # «per_million» still hits a value. Identical to count when
            # v1 surfaces per_million directly.
            "per_million": (float(pm_val)
                             if isinstance(pm_val, (int, float))
                             else (float(val) if not already_shares
                                                  and isinstance(val, (int, float))
                                                  else None)),
        })
    # Sort emotions by share descending so the table leads with the
    # dominant emotions; matches what the typed-view template would do.
    emotions.sort(key=lambda x: x.get("share") or 0, reverse=True)
    return emotions


def _attach_emotion_profile_view(result, raw, pg_id: str) -> None:
    # E15 P0 FIX (2026-05-22): v1 book_emotion_profile (rag_tools.py:1900)
    # returns keys `per_million` (dict {emo: float}),
    # `share_among_primary_emotions` (dict {emo: share}), and
    # `sample_anchor_words`, NOT `emotions`/`profile`/`distribution`.
    # View has been silently empty since Phase 2.5. Same class as B-R14-7
    # / E9 / E14b / E15. Read v1's actual keys first; legacy names kept
    # as fallback for test mocks.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return
        # V1BookEmotionProfile canonical: title, share_among_primary_emotions,
        # per_million, sample_anchor_words.
        title = raw.get("title") or pg_id
        emotions = _build_emotion_rows(raw)
        # W-3 (2026-05-24) — surface the normalized row list on raw so
        # the LLM render payload sees the same rows the typed-view
        # template would render. `_normalize_payload_tool_results`
        # iterates lists-of-dicts; without `emotions` the LLM only saw
        # dict-of-dicts (share + per_million) and dropped the join.
        raw["emotions"] = emotions
        # V1BookEmotionProfile doesn't expose `dominant`/`top_emotions` at
        # top level; compute from emotions list.
        dominant: list = [e["emotion"] for e in emotions[:3]]
        view = vb.build_emotion_profile(
            book_title=title,
            pg_id=pg_id,
            emotions=emotions,
            dominant=dominant,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.books.top_books").exception(
            "book_emotion_profile view emission failed"
        )
