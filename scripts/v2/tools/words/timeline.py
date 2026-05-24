"""v2 word_freq_timeline + words_disappearing_after + words_appearing_after."""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1WordFreqTimeline, V1WordsDisappearingAfter, V1WordsAppearingAfter,
)


# V1 returns bucket period as a literal string of two 4-digit years
# separated by an ASCII hyphen (rag_tools.py:1249 — f"{int(period)}-
# {int(period)+bucket_years-1}"). The bucketizer pins that shape so a
# malformed value (None, missing, or unexpected punctuation) lands as
# a loud ToolWarning instead of silently rendering "None–None".
_PERIOD_RE = re.compile(r"^\s*(\d{3,4})\s*-\s*(\d{3,4})\s*$")


@tool(
    name="word_freq_timeline",
    category="words",
    description=(
        "Кривая частотности слова по эпохам (25-летние бакеты). "
        "ИСПОЛЬЗУЙ basis='auto' (default) — это authoryearofbirth+30 "
        "(writing prime proxy), покрывает все ~75k книг корпуса. "
        "basis='pub_year' покрывает только ~4 книги (Open Library "
        "enrichment ещё неполный) — НЕ выбирай его для запросов про "
        "1800-1950 годы. basis='birth_plus_30' = синоним 'auto'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "word":         {"type": "string"},
            "bucket_years": {"type": "integer", "description": "default 25"},
            "basis":        {"type": "string",
                              "enum": ["auto", "birth_plus_30", "pub_year"],
                              "description":
                              "default 'auto' (= birth_plus_30, full corpus). "
                              "pub_year coverage ≈ 4 books only — avoid"},
        },
        "required": ["word"],
    },
    requires=["word"],
    cost="medium",
    cacheable=True,
    # W-2 (2026-05-24) — bump bust caches keyed on the old shape
    # (`period: "1825-1849"`, missing freq_per_million alias). After the
    # bump cached results re-normalize through the wrapper.
    wrapper_version="v3-w2-render-row-shape",
)
@v1_contract(v1_fn="scripts.rag_tools.word_freq_timeline",
             schema=V1WordFreqTimeline)
def word_freq_timeline(word: str, bucket_years: int = 25,
                      basis: str = "auto") -> ToolResult:
    from scripts.rag_tools import word_freq_timeline as _v1
    raw = _v1(word=word, bucket_years=bucket_years, basis=basis)
    query = {"word": word, "bucket_years": bucket_years, "basis": basis}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_freq_timeline", err_type="not_found",
                               message=str(raw["error"]), query=query)

    buckets = (raw.get("timeline") if isinstance(raw, dict) else None) or []

    # W-2 (2026-05-24) — railway/telegraph/steam «None–None» repro.
    # Normalize v1 bucket rows once at wrapper boundary so the LLM
    # render payload (r.data.timeline) and the typed view's series
    # both carry display-ready period labels and a populated
    # freq_per_million column. The LLM no longer sees the ASCII-hyphen
    # v1 shape (`period: "1825-1849"`) — it sees the en-dash label
    # alongside renderer-friendly aliases, so a hallucinated column
    # name still hits a real value. Replaces in-place so the rest of
    # this function (auto-fallback, view emission) operates on the
    # canonical shape.
    if isinstance(raw, dict):
        rows, malformed_initial = _normalize_timeline_rows(buckets)
        raw["timeline"] = rows
        buckets = rows
    else:
        malformed_initial = 0

    # Sprint 20 — Stan 2026-05-19: LLM-planner emitted basis='pub_year',
    # which has ~4 books coverage. Result: 1 bucket "2000-2024" with 0
    # occurrences, renderer correctly cited it and said «не охватывают
    # 1880-1950». Auto-fallback to 'auto' when pub_year gives <3
    # buckets OR all zeros — saves the user from a misleading answer.
    auto_fallback_used = False
    if (basis == "pub_year"
            and isinstance(raw, dict)
            and (len(buckets) < 3
                 or all((b.get("occurrences", 0) == 0) for b in buckets))):
        raw_auto = _v1(word=word, bucket_years=bucket_years, basis="auto")
        if isinstance(raw_auto, dict) and not raw_auto.get("error"):
            buckets_auto_raw = raw_auto.get("timeline") or []
            if len(buckets_auto_raw) >= 3 or any(
                    b.get("occurrences", 0) > 0 for b in buckets_auto_raw):
                # Normalize fallback rows the same way so the LLM/view
                # don't see one shape in the primary branch and another
                # after auto-fallback.
                buckets_auto, malformed_fallback = (
                    _normalize_timeline_rows(buckets_auto_raw))
                raw_auto["timeline"] = buckets_auto
                raw = raw_auto
                buckets = buckets_auto
                malformed_initial = malformed_fallback
                auto_fallback_used = True
                raw["basis_originally_requested"] = "pub_year"
                raw["basis_fallback_reason"] = (
                    "pub_year coverage is ~4 books (sparse / all-zero); "
                    "auto-fallback to birth_plus_30 for full corpus coverage"
                )
                existing = raw.get("_render_note") or ""
                fallback_note = (
                    "Tool wrapper auto-fell-back from basis='pub_year' "
                    "(sparse coverage) to basis='auto' "
                    "(birth_plus_30 — full ~75k books). Renderer must "
                    "report the new buckets, NOT the empty pub_year result."
                )
                raw["_render_note"] = (
                    (existing + " | " if existing else "") + fallback_note
                )

    warnings: list[ToolWarning] = []
    if len(buckets) < 3:
        warnings.append(ToolWarning(
            code="sparse_timeline",
            message=f"only {len(buckets)} bucket(s) — coverage is thin",
        ))
    if auto_fallback_used:
        warnings.append(ToolWarning(
            code="basis_auto_fallback",
            message=("pub_year coverage was ~4 books → fell back to "
                      "birth_plus_30 for usable timeline"),
        ))
    if malformed_initial:
        # Surface period-shape failure loudly so the prod path doesn't
        # quietly render «?–?» without anyone noticing (W-2).
        warnings.append(ToolWarning(
            code="period_unparseable",
            message=(
                f"{malformed_initial} bucket(s) had unparseable period "
                f"boundaries; rendered as «?–?». Expect v1 shape "
                f"'YYYY-YYYY'."
            ),
        ))

    # v1 word_freq_timeline doesn't expose a books_total — coverage stays
    # opaque (per V1WordFreqTimeline contract).
    result = ToolResult.success(
        tool="word_freq_timeline", data=raw,
        coverage=Coverage(books_matched=-1, books_total=-1),
        warnings=warnings,
        query=query,
    )
    _attach_timeline_view(result, buckets, word=word,
                           basis=raw.get("basis", basis) if isinstance(raw, dict) else basis)
    return result


@tool(
    name="words_disappearing_after",
    category="words",
    description="Слова, которые резко вышли из употребления после заданного года.",
    input_schema={
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "cutoff year, default 1920"},
            "top":  {"type": "integer", "description": "default 25"},
        },
        "required": [],
    },
    requires=[],
    cost="heavy",
    cacheable=True,
    # E18 (2026-05-22) — E15 now reads v1's «top» key (not «words») and
    # nested pre_bucket/post_bucket counts (not flat books_before/after).
    wrapper_version="v3-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.words_disappearing_after",
             schema=V1WordsDisappearingAfter)
def words_disappearing_after(year: int = 1920, top: int = 25) -> ToolResult:
    from scripts.rag_tools import words_disappearing_after as _v1
    raw = _v1(year=year, top=top)
    query = {"year": year, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="words_disappearing_after",
            err_type=("not_found" if "not enough" in err.lower() or "no books" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    # Phase 2 — V1WordsDisappearingAfter declares canonical `top` +
    # nested pre_bucket/post_bucket with `books` field. Phantom fallbacks
    # (`words`, flat `books_before`/`books_after`) removed per R3.
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    books_before = 0
    books_after = 0
    if isinstance(raw, dict):
        pre_b = raw.get("pre_bucket") or {}
        post_b = raw.get("post_bucket") or {}
        if isinstance(pre_b, dict):
            books_before = int(pre_b.get("books") or 0)
        if isinstance(post_b, dict):
            books_after = int(post_b.get("books") or 0)
    if books_before or books_after:
        warnings.append(ToolWarning(
            code="coverage",
            message=f"{books_before} books before {year}, "
                    f"{books_after} after",
        ))
    result = ToolResult.success(
        tool="words_disappearing_after", data=raw,
        coverage=Coverage(
            books_matched=(books_before + books_after) or -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
    _attach_disappearing_view(result, rows, year=year, top=top)
    return result


@tool(
    name="words_appearing_after",
    category="words",
    description=(
        "W-12 (2026-05-23) — слова резко вошедшие в употребление после "
        "заданного года. Зеркальная пара к words_disappearing_after; "
        "ранжирует по rise_ratio = post_per_million / pre_per_million. "
        "Используй для «слова, ставшие чаще», «emerging vocabulary», "
        "«trending words after 1900»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "cutoff year, default 1920"},
            "top":  {"type": "integer", "description": "default 25"},
        },
        "required": [],
    },
    requires=[],
    cost="heavy",
    cacheable=True,
    wrapper_version="v1-w12-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.words_appearing_after",
             schema=V1WordsAppearingAfter)
def words_appearing_after(year: int = 1920, top: int = 25) -> ToolResult:
    from scripts.rag_tools import words_appearing_after as _v1
    raw = _v1(year=year, top=top)
    query = {"year": year, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="words_appearing_after",
            err_type=("not_found" if "not enough" in err.lower() or "no books" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    warnings: list[ToolWarning] = []
    books_before = 0
    books_after = 0
    if isinstance(raw, dict):
        pre_b = raw.get("pre_bucket") or {}
        post_b = raw.get("post_bucket") or {}
        if isinstance(pre_b, dict):
            books_before = int(pre_b.get("books") or 0)
        if isinstance(post_b, dict):
            books_after = int(post_b.get("books") or 0)
    if books_before or books_after:
        warnings.append(ToolWarning(
            code="coverage",
            message=f"{books_before} books before {year}, "
                    f"{books_after} after",
        ))
    result = ToolResult.success(
        tool="words_appearing_after", data=raw,
        coverage=Coverage(
            books_matched=(books_before + books_after) or -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
    _attach_appearing_view(result, rows, year=year, top=top)
    return result


# =====================================================================
# v5 Phase 2.5 — view emission helpers
# =====================================================================


def _parse_period(period) -> tuple[int | None, int | None]:
    """Parse a v1 period string ("1825-1849") into (start, end).

    Returns (None, None) when shape doesn't match. Stricter than the
    earlier `partition("-")` parse so multi-hyphen / partial-numeric
    values never half-assign (used to set bucket_start while leaving
    bucket_end None — yielded "1825–?" in renders).
    """
    if not isinstance(period, str):
        return None, None
    m = _PERIOD_RE.match(period)
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None, None


def _normalize_timeline_rows(buckets) -> tuple[list[dict], int]:
    """Normalize v1 timeline rows to the canonical wrapper shape.

    Single source of truth for both the LLM render payload
    (`r.data["timeline"]`) and the typed view's `series`. Each output
    row preserves v1's documented row keys (period, books, total_tokens,
    occurrences, per_million) AND surfaces renderer-friendly fields:

      * period            — display-ready label with en-dash («1825–1849»);
                             ASCII-hyphen v1 shape is replaced so the LLM
                             never has a reason to invent a different one.
      * bucket_start/end  — int | None, parsed from the v1 period string
                             via the strict `_PERIOD_RE`. None when the
                             string fails the shape — those rows count
                             toward `malformed`.
      * freq_per_million  — RECOMPUTED from occurrences/total_tokens × 1e6
                             so the «Частота на 1M слов» column is robust
                             against v1 dropping the field; identical to
                             v1's per_million when v1 supplies it. Falls
                             back to v1's per_million when counts are
                             missing / total_tokens is zero.
      * count             — alias for occurrences («Вхождений» header).

    Rows are sorted ascending by bucket_start so a v1 returning out-of-
    order buckets never poisons the leading edge of the chart. Returns
    `(rows, malformed_count)`; the caller surfaces malformed as a
    ToolWarning so the prod path can't quietly emit «?–?» rows.

    W-2 (2026-05-24): railway/telegraph/steam produced «None–None» in
    every period cell and an empty per-million column in prod. Two
    structural causes covered here — (a) the wrapper used to pass v1's
    `per_million` straight through, so a missing/null v1 value left the
    column blank; (b) the LLM render payload kept the v1 ASCII-hyphen
    `period`, leaving room for the renderer to invent bucket_start /
    bucket_end column names with None values. Both close here.
    """
    rows: list[dict] = []
    malformed = 0
    for b in (buckets or []):
        if not isinstance(b, dict):
            malformed += 1
            continue
        bucket_start, bucket_end = _parse_period(b.get("period"))
        if bucket_start is None or bucket_end is None:
            malformed += 1
        occurrences = b.get("occurrences")
        total_tokens = b.get("total_tokens")
        # Recompute per-million from counts. round(2) matches v1's
        # rag_tools.py:1253 formula so a sweep diff stays zero on
        # well-shaped v1 rows; a v1 row that dropped per_million now
        # still gets one — the canonical fix for the empty «Частота»
        # column. Falls through to v1's per_million when counts can't
        # support recomputation (total_tokens missing / zero).
        if (isinstance(occurrences, (int, float))
                and isinstance(total_tokens, (int, float))
                and total_tokens > 0):
            freq_per_million = round(
                1_000_000.0 * float(occurrences) / float(total_tokens), 2,
            )
        else:
            v1_pm = b.get("per_million")
            freq_per_million = (
                float(v1_pm) if isinstance(v1_pm, (int, float)) else None
            )
        if bucket_start is not None and bucket_end is not None:
            period_label = f"{bucket_start}–{bucket_end}"
        else:
            period_label = "?–?"
        rows.append({
            # v1 row keys — preserved for the V1WordFreqTimeline
            # __row_keys__ contract and any downstream cache rows that
            # still address columns by v1's names.
            "period": period_label,           # display-ready (was "1825-1849")
            "books": b.get("books"),
            "total_tokens": total_tokens,
            "occurrences": occurrences,
            "per_million": freq_per_million,  # recomputed, consistent with the alias
            # Renderer-friendly aliases — both the LLM (which sees this
            # dict in r.data) and the typed renderer's series builder
            # read these directly.
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "freq_per_million": freq_per_million,
            "count": occurrences,
        })
    # Ascending by bucket_start; None starts go last so an unparseable
    # row never lands at the leading edge.
    rows.sort(key=lambda r: (r["bucket_start"] is None,
                              r["bucket_start"] or 0))
    return rows, malformed


def _build_timeline_series(rows) -> list[dict]:
    """Project normalized timeline rows to the typed-view series shape.

    Kept separate from `_normalize_timeline_rows` so the view payload
    only carries the keys `build_timeline_chart` documents — adding
    new keys here would broaden the typed-renderer contract silently.
    """
    return [{
        "bucket_start": r.get("bucket_start"),
        "bucket_end": r.get("bucket_end"),
        "freq_per_million": r.get("freq_per_million"),
        "count": r.get("count"),
    } for r in (rows or [])]


def _attach_timeline_view(result, rows, *, word: str, basis: str) -> None:
    """Build and attach the TIMELINE_CHART view from normalized rows.

    `rows` come from `_normalize_timeline_rows` — caller has already
    counted malformed entries and emitted the `period_unparseable`
    warning (so this function is purely a view projection now and
    can't double-count). Errors during view emission stay confined
    to the typed-renderer path; the LLM render path keeps working
    because r.data.timeline is already normalized.
    """
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        series = _build_timeline_series(rows)
        view = vb.build_timeline_chart(
            word=word,
            series=series,
            basis=basis,
            language="ru",
        )
        validity = DataValidity.OK if series else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.timeline").exception(
            "word_freq_timeline view emission failed"
        )


def _attach_appearing_view(result, rows, *, year: int, top: int) -> None:
    """W-12 mirror of `_attach_disappearing_view` — same shape but the
    headline + column label highlight the RISE direction."""
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        headline = f"Слова, появившиеся (или ставшие чаще) после {year}"
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "rise_factor"],
                headline=headline,
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru=f"Не нашлось слов с резким ростом после {year}.",
                empty_message_en="No appearing words.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            rise = r.get("rise_ratio")
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
                "rise_factor": (f"{rise:.1f}×" if isinstance(rise, (int, float))
                                else (rise or "—")),
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "rise_factor"],
            headline=headline,
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.timeline").exception(
            "words_appearing_after view emission failed"
        )


def _attach_disappearing_view(result, rows, *, year: int, top: int) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        headline = f"Слова, исчезнувшие после {year}"
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "drop_factor"],
                headline=headline,
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru=f"Не нашлось слов с резким падением после {year}.",
                empty_message_en="No disappearing words.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # Phase 2 — V1WordsDisappearingAfter declares row.drop_ratio
            # (rag_tools.py:1370). No phantom fallbacks.
            drop = r.get("drop_ratio")
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
                "drop_factor": (f"{drop:.1f}×" if isinstance(drop, (int, float))
                                else (drop or "—")),
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "drop_factor"],
            headline=headline,
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.timeline").exception(
            "words_disappearing_after view emission failed"
        )
