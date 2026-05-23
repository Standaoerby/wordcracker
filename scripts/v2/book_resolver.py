"""Book resolver — v5 pipeline (no v6 book linker yet).

Moved out of `scripts.v2.entity_resolver` in T1 (D-P1-6) because v6
covers author resolution but book resolution still goes through the
KNOWN_BOOKS + RU title alias + v1 `find_book` pipeline. Splitting it
into its own module makes the "one resolver per entity type" structure
explicit — author resolution lives in `entity_resolver_v6`, book
resolution lives here.

Pipeline:
  normalize → RU title alias map (genitive case forms) → KNOWN_BOOKS
  (curated alias dict) → v1 `find_book` fuzzy with prominence ranking
  by downloads.

Public surface:
  - `resolve_book(query, *, author_hint="")` → ResolveResult
  - `resolve_ru_book_alias(q_lc)` → (pg_id, canonical_en, alias_trace) | None
"""
from __future__ import annotations

import logging

from scripts.v2.entity_resolver_v6.normalize import normalize_query
from scripts.v2.entity_resolver_v6.prominence import (
    _fuzz_band,
    confidence_from_gap,
)
from scripts.v2.entity_resolver_v6.types import Candidate, ResolveResult

log = logging.getLogger("wordcracker.v2.book_resolver")


# Confidence threshold below which we surface candidates instead of
# committing to a top-1.
_CLARIFY_CONFIDENCE_FLOOR = 0.65

# Score below which we treat as not_found.
_NOTFOUND_SCORE_FLOOR = 60


# RU book title aliases — genitive / various cases → nominative.
# Lookup of RU nominative goes through KNOWN_BOOKS (in planner.entities)
# OR through the local _RU_NOMINATIVE_TO_PG map below.
_RU_BOOK_TITLE_ALIASES: dict[str, str] = {
    # B-R14-9 part 2 — «Братьев Карамазовых» missing from KNOWN_BOOKS.
    "братьев карамазовых": "братья карамазовы",
    "братьями карамазовыми": "братья карамазовы",
    "братьям карамазовым": "братья карамазовы",
    "братья карамазовы": "братья карамазовы",
    "the brothers karamazov": "братья карамазовы",
    "brothers karamazov": "братья карамазовы",
    # Anna Karenina genitives — already partially covered, but ensure
    # canonicalization here too.
    "анны карениной": "анна каренина",
    "анной карениной": "анна каренина",
    "анну каренину": "анна каренина",
    "анна каренина": "анна каренина",
    "anna karenina": "анна каренина",
}

# Mapping of RU nominative titles → (pg_id, canonical EN title).
# Used when the resolved RU title isn't in KNOWN_BOOKS yet.
_RU_NOMINATIVE_TO_PG: dict[str, tuple[str, str]] = {
    "братья карамазовы": ("PG28054", "The Brothers Karamazov"),
    "анна каренина":     ("PG1399",  "Anna Karenina"),
}


def resolve_ru_book_alias(q_lc: str) -> tuple[str | None, str | None, str] | None:
    """If `q_lc` (already normalized lowercase) is a Russian title in
    any case form, return (pg_id, canonical_en, alias_used). None if not
    a known RU title.
    """
    nominative = _RU_BOOK_TITLE_ALIASES.get(q_lc)
    if nominative is None:
        return None
    mapped = _RU_NOMINATIVE_TO_PG.get(nominative)
    if mapped is None:
        return None
    pg, canon_en = mapped
    return pg, canon_en, f"RU title «{q_lc}» → «{nominative}»"


def resolve_book(query: str, *, author_hint: str = "") -> ResolveResult:
    """Resolve a book title to a single PG id.

    Pipeline: normalize → RU title alias map → KNOWN_BOOKS → v1 find_book
    fuzzy with prominence ranking by downloads.
    """
    raw = query or ""
    norm = normalize_query(raw)
    q_lc = norm.output
    trace = list(norm.steps)

    if not q_lc:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized="",
            confidence_reason="empty query",
        )

    # Step A: RU title alias map (genitive case forms of major works)
    ru_hit = resolve_ru_book_alias(q_lc)
    if ru_hit is not None:
        pg_id, canon_en, alias_trace = ru_hit
        trace.append(alias_trace)
        cand = Candidate(
            key=pg_id, display=canon_en, score=100,
            source="ru_title_alias",
        )
        return ResolveResult(
            decision="resolved",
            resolved={"pg_id": pg_id, "title": canon_en, "author": None,
                       "source": "ru_title_alias"},
            confidence=0.95,
            candidates=[cand],
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="RU title alias exact match",
        )

    # Step B: KNOWN_BOOKS exact / declension match
    try:
        from scripts.v2.planner.entities import KNOWN_BOOKS
    except ImportError:
        KNOWN_BOOKS = {}
    for k in {q_lc, " ".join(q_lc.split())}:
        if k in KNOWN_BOOKS:
            pg_id, canon = KNOWN_BOOKS[k]
            cand = Candidate(
                key=pg_id or "", display=canon, score=100,
                source="alias_curated",
            )
            conf = 1.0 if pg_id else 0.75
            reason = ("KNOWN_BOOKS exact" if pg_id
                      else "KNOWN_BOOKS hit but no PG id (copyright)")
            return ResolveResult(
                decision="resolved",
                resolved={"pg_id": pg_id or None, "title": canon,
                           "author": None, "source": "known_books"},
                confidence=conf,
                candidates=[cand],
                normalization_trace=trace,
                query_raw=raw, query_normalized=q_lc,
                confidence_reason=reason,
            )

    # Step C: delegate to v1 find_book (handles Cyrillic auto-translate)
    try:
        from scripts.rag_tools import find_book as _v1_find_book
    except ImportError:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="v1 find_book unavailable",
        )

    raw_res = _v1_find_book(title=query, author=author_hint or "", top=5,
                            lang="en")
    if isinstance(raw_res, dict) and raw_res.get("error"):
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=f"find_book error: {raw_res.get('error')}",
        )
    matches = ((raw_res.get("matches") if isinstance(raw_res, dict) else None)
               or [])
    if not matches:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="no fuzzy book match",
        )

    cands = [
        Candidate(
            key=str(m.get("id") or ""),
            display=m.get("title") or "",
            score=int(min(100, 60 + int((m.get("downloads") or 0) ** 0.5))),
            source="fuzzy",
            prominence=int(m.get("downloads") or 0),
            extra={"author": m.get("author") or ""},
        )
        for m in matches
    ]
    cands.sort(key=lambda c: (_fuzz_band(c.score), c.prominence), reverse=True)
    top = cands[0]
    runner_up = cands[1] if len(cands) > 1 else None
    conf, reason = confidence_from_gap(top, runner_up)

    if conf < _CLARIFY_CONFIDENCE_FLOOR:
        return ResolveResult(
            decision="clarify_needed",
            resolved=None, confidence=conf,
            candidates=cands[:5],
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=reason,
        )

    return ResolveResult(
        decision="resolved",
        resolved={
            "pg_id": top.key, "title": top.display,
            "author": top.extra.get("author") or None,
            "source": "find_book_fuzzy",
        },
        confidence=conf,
        candidates=cands[:5],
        normalization_trace=trace,
        query_raw=raw, query_normalized=q_lc,
        confidence_reason=reason,
    )


__all__ = [
    "resolve_book",
    "resolve_ru_book_alias",
]
