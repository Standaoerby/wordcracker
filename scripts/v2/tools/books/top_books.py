"""v2 top_books_by_downloads + top_books_by_recency + book_emotion_profile."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


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
)
def top_books_by_downloads(top: int = 20, lang: str = "en",
                           author_regex: str | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import top_books_by_downloads as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_books_by_downloads", err_type="internal",
                               message=f"v1 unavailable: {e}")
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
)
def top_books_by_recency(top: int = 20, lang: str = "en",
                         author_regex: str | None = None,
                         metric: str = "pg_id") -> ToolResult:
    try:
        from scripts.rag_tools import top_books_by_recency as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_books_by_recency", err_type="internal",
                               message=f"v1 unavailable: {e}")
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
    # E24 (2026-05-22) — view now fills `count` from per_million when
    # share is source (was None → «Вхождений» column empty in persona
    # Q4/Q12 — exactly the column the user looks at for frequency).
    wrapper_version="v3-e24-emotion-counts",
)
def book_emotion_profile(pg_id: str) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="book_emotion_profile", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG84')",
            query={"pg_id": pg_id},
        )
    try:
        from scripts.rag_tools import book_emotion_profile as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_emotion_profile", err_type="internal",
                               message=f"v1 unavailable: {e}")
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
            v = (r.get(metric) or r.get("downloads") or r.get("pub_year")
                 or r.get("id"))
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
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.books.top_books").warning(
            "top_books view emission failed: %s", e,
        )


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
        title = raw.get("book_title") or raw.get("title") or pg_id
        # v1 actual keys come first; legacy fallback for test mocks.
        share_raw = raw.get("share_among_primary_emotions")
        per_million_raw = raw.get("per_million")
        emotions_raw = (share_raw or per_million_raw
                        or raw.get("emotions") or raw.get("profile")
                        or raw.get("distribution") or {})
        emotions: list[dict] = []
        if isinstance(emotions_raw, dict):
            # If we have shares already (sum to ~1), use directly; if not
            # (e.g. per_million), recompute shares.
            values = [v for v in emotions_raw.values()
                      if isinstance(v, (int, float))]
            total = sum(values) if values else 0
            already_shares = (0.95 <= total <= 1.05) and bool(share_raw)
            # E24 (2026-05-22) — persona test Q4/Q12 «эмоциональный профиль
            # Frankenstein/Dracula» showed «Вхождений» (count) column
            # empty. v1 returns BOTH share_among_primary_emotions AND
            # per_million. When share is source, count was None; now
            # populate from per_million as a meaningful per-million
            # frequency so the column is filled.
            per_million_for: dict = (per_million_raw
                                      if isinstance(per_million_raw, dict)
                                      else {})
            for emo, val in emotions_raw.items():
                if not isinstance(val, (int, float)):
                    continue
                if already_shares:
                    share = float(val)
                    pm_val = per_million_for.get(emo)
                    # Show per-million frequency as count (more meaningful
                    # than the share fraction)
                    count = (round(float(pm_val)) if isinstance(pm_val, (int, float))
                              else None)
                else:
                    share = (float(val) / total) if total else 0.0
                    count = val
                emotions.append({
                    "emotion": emo,
                    "share": share,
                    "count": count,
                })
        elif isinstance(emotions_raw, list):
            for e in emotions_raw:
                if isinstance(e, dict):
                    emotions.append({
                        "emotion": e.get("emotion") or e.get("name", "—"),
                        "share": e.get("share"),
                        "count": e.get("count"),
                    })
        # Compute dominant from share if v1 didn't supply it. NRC ranking:
        # top 3 emotions by share.
        dominant = raw.get("dominant") or raw.get("top_emotions") or []
        if not dominant and emotions:
            dominant = [
                e["emotion"] for e in
                sorted(emotions, key=lambda x: x.get("share") or 0,
                       reverse=True)[:3]
            ]
        if not isinstance(dominant, list):
            dominant = [str(dominant)]
        view = vb.build_emotion_profile(
            book_title=title,
            pg_id=pg_id,
            emotions=emotions,
            dominant=dominant,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.books.top_books").warning(
            "book_emotion_profile view emission failed: %s", e,
        )
