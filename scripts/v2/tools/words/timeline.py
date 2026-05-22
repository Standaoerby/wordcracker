"""v2 word_freq_timeline + words_disappearing_after."""
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
    V1WordFreqTimeline, V1WordsDisappearingAfter,
)


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
    wrapper_version="v2-phase2-contract",
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
            buckets_auto = raw_auto.get("timeline") or []
            if len(buckets_auto) >= 3 or any(
                    b.get("occurrences", 0) > 0 for b in buckets_auto):
                raw = raw_auto
                buckets = buckets_auto
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


# =====================================================================
# v5 Phase 2.5 — view emission helpers
# =====================================================================


def _attach_timeline_view(result, buckets, *, word: str, basis: str) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        series = []
        # Phase 2 — V1WordFreqTimeline buckets carry: period, books,
        # total_tokens, occurrences, per_million. Derive bucket bounds
        # from `period` ("1800-1824"); freq from `per_million`.
        for b in (buckets or []):
            if not isinstance(b, dict):
                continue
            bucket_start = bucket_end = None
            period = b.get("period")
            if isinstance(period, str) and "-" in period:
                lo, _, hi = period.partition("-")
                try:
                    bucket_start = int(lo); bucket_end = int(hi)
                except ValueError:
                    pass
            series.append({
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "freq_per_million": b.get("per_million"),
                "count": b.get("occurrences"),
            })
        view = vb.build_timeline_chart(
            word=word,
            series=series,
            basis=basis,
            language="ru",
        )
        validity = DataValidity.OK if series else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.timeline").warning(
            "word_freq_timeline view emission failed: %s", e,
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
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.timeline").warning(
            "words_disappearing_after view emission failed: %s", e,
        )
