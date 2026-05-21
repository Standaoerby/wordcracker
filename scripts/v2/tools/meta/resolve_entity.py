"""Entity resolvers — v4 (Phase 1.5 migration to v5 backend).

Two @tool wrappers that the LLM planner composes into plans:

    resolve_author_name(query: str) → {author_regex, canonical, confidence, candidates}
    resolve_book_title(query: str)  → {pg_id, title, author, confidence, candidates}

They turn ambiguous, possibly mistyped, possibly Cyrillic user phrasings
into the canonical inputs that downstream tools (`affinity_by_author`,
`book_readability`, etc.) expect — *without* requiring the input to
already be in AUTHOR_ALIASES or KNOWN_BOOKS.

v5 Phase 1.5 ([[architecture_refactor_v5_plan]] §P3):
When env `WC_V5_RESOLVER=on`, both tools delegate to
`scripts.v2.entity_resolver` — single canonical path with:
  - NFKC + homoglyph fold (closes B-R14-4 «Доcтоевский» with Latin c)
  - RU genitive lemmatization (closes B-R14-9 «Толстого» → Толстой)
  - RU→EN title alias map (closes B-R14-9 part 2 «Братьев Карамазовых»)
  - prominence ranking by downloads (closes B-R14-13 «Hugo» → Victor Hugo)

Legacy fields preserved (author_regex, canonical, confidence,
candidates) — downstream callers unchanged. New v5 fields added:
  - normalization_trace: list of normalization steps (for trace/debug)
  - data.view: typed RenderableView for the answer (NOT_FOUND / CLARIFY
    when applicable). Phase 2 tools will read this; Phase 0 ignores.

When the flag is off, the original layered logic runs unchanged. The
flag will be flipped on as a final Phase 6 cleanup, after the regression
suite stays green for one full external Claude test round.

Layered resolution (when flag OFF)
==================================
For both tools:
    1. **Curated alias dict** (existing entities.py tables) — ≤1 ms.
    2. **Fuzzy match** against the SPGC metadata authors / titles using
       rapidfuzz if available, else a simple substring scan.
    3. **Tool fallback** — when fuzzy is ambiguous, return all
       candidates and let the renderer ask the user to disambiguate.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.meta.resolve_entity")


# ---------- shared helpers ----------


def _norm_query(q: str) -> str:
    return (q or "").strip().lower().strip("\"'«»“”")


def _try_rapidfuzz() -> Optional[object]:
    """Lazy-import rapidfuzz if available. None on miss; caller falls
    back to a slower substring scan."""
    try:
        import rapidfuzz  # noqa: F401
        from rapidfuzz import fuzz, process
        return process, fuzz
    except ImportError:
        return None


# ---------- resolve_author_name ----------


@tool(
    name="resolve_author_name",
    category="search",
    description=(
        "Резолвит свободную формулировку имени автора («Конан Дойль», "
        "«Doyle», «у Достоевского», «john milton») в канонический "
        "author_regex для downstream tools (affinity_by_author, "
        "compare_authors). Layered: curated aliases → fuzzy match по "
        "SPGC metadata. v4 planner вызывает это перед author-tools."
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
    # v5 Phase 1.5 — delegate to entity_resolver when feature flag on.
    # Always emits a typed view alongside legacy fields for forward compat.
    if _v5_enabled():
        return _v5_resolve_author(query)

    q = _norm_query(query)
    if not q:
        return ToolResult.fail(
            tool="resolve_author_name", err_type="invalid_args",
            message="query is required",
        )

    # Layer 1: curated aliases (fast path, also matches v3 rules).
    # Try the full query, then progressively narrower fragments — last
    # word (surname-only), first+last word, every individual word.
    # Catches «Conan Doyle» / «Arthur Conan Doyle» / «J.R.R. Tolkien».
    try:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
    except ImportError:
        AUTHOR_ALIASES = {}

    keys_to_try: list[str] = []
    keys_to_try.append(q)
    keys_to_try.append(" ".join(q.split()))
    parts = q.split()
    if len(parts) >= 2:
        keys_to_try.append(parts[-1])           # surname only
        keys_to_try.append(parts[0])             # forename only (rare hits)
        keys_to_try.append(f"{parts[0]} {parts[-1]}")  # first + last
    seen: set[str] = set()
    for k in keys_to_try:
        k = k.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        if k in AUTHOR_ALIASES:
            regex = AUTHOR_ALIASES[k]
            return ToolResult.success(
                tool="resolve_author_name",
                data={
                    "author_regex": regex,
                    "canonical": _regex_to_canonical(regex),
                    "confidence": 1.0 if k == q else 0.92,
                    "source": "alias_curated",
                    "matched_key": k,
                    "candidates": [{"regex": regex, "score": 100}],
                    "query": query,
                },
                coverage=Coverage(books_matched=-1, books_total=-1),
                query={"query": query},
            )

    # Layer 2: fuzzy match against authors in SPGC metadata
    candidates = _fuzzy_author_candidates(q, limit=5)
    if candidates:
        best = candidates[0]
        # If we have a clear winner (>= 5 point gap over second), high
        # confidence; otherwise medium (renderer should disambiguate).
        gap = (best["score"] - candidates[1]["score"]) if len(candidates) > 1 else 100
        confidence = 0.88 if gap >= 5 else 0.55
        return ToolResult.success(
            tool="resolve_author_name",
            data={
                "author_regex": best["regex"],
                "canonical": best["canonical"],
                "confidence": confidence,
                "source": "fuzzy_metadata",
                "candidates": candidates,
                "query": query,
            },
            warnings=([ToolWarning(
                code="ambiguous",
                message=f"top-2 within {5 - gap if gap < 5 else 0} points, "
                          f"renderer should ask which one",
            )] if gap < 5 else []),
            coverage=Coverage(books_matched=-1, books_total=-1),
            query={"query": query},
        )

    return ToolResult.fail(
        tool="resolve_author_name", err_type="not_found",
        message=f"no author matched query {query!r}",
        details={"query": query},
    )


def _regex_to_canonical(regex: str) -> str:
    """`^Doyle, Arthur` → `Doyle, Arthur`. `^Wodehouse,` → `Wodehouse`."""
    if not regex:
        return ""
    s = regex.lstrip("^").rstrip(",").strip()
    return s


def _fuzzy_author_candidates(q: str, limit: int = 5) -> list[dict]:
    """Score `q` against SPGC author column. Returns list of
    `{regex, canonical, score}` sorted by score desc."""
    try:
        from scripts.rag_tools import _metadata_df
        df = _metadata_df()
    except Exception:
        return []

    if df is None or "author" not in df.columns:
        return []

    # Build unique author list once (mtime-cached on _metadata_df side).
    authors_series = df["author"].dropna().astype(str)
    if authors_series.empty:
        return []
    authors = authors_series.unique().tolist()

    # Prefer rapidfuzz if installed; fall back to a cheap substring scan.
    rf = _try_rapidfuzz()
    if rf is not None:
        process, fuzz = rf
        # extract returns [(choice, score, idx), ...]
        try:
            matches = process.extract(q, authors, scorer=fuzz.WRatio,
                                       limit=limit)
        except Exception:
            matches = []
        out = []
        for choice, score, _idx in matches:
            if score < 60:
                continue
            surname = choice.split(",", 1)[0].strip()
            if not surname:
                continue
            out.append({
                "regex": f"^{surname},",
                "canonical": choice,
                "score": int(score),
            })
        return out

    # No rapidfuzz — substring scan with a basic score
    q_lc = q.lower()
    raw: list[tuple[int, str]] = []
    for a in authors:
        a_lc = a.lower()
        if q_lc in a_lc:
            score = int(100 * len(q_lc) / max(len(a_lc), 1))
            raw.append((score, a))
        elif a_lc.startswith(q_lc.split()[0]) if q_lc.split() else False:
            raw.append((60, a))
    raw.sort(reverse=True)
    out = []
    for score, choice in raw[:limit]:
        surname = choice.split(",", 1)[0].strip()
        if not surname:
            continue
        out.append({
            "regex": f"^{surname},",
            "canonical": choice,
            "score": score,
        })
    return out


# ---------- resolve_book_title ----------


@tool(
    name="resolve_book_title",
    category="search",
    description=(
        "Резолвит свободную формулировку названия книги («Beowulf», "
        "«Pride and Prejudice», «Преступление и наказание») в "
        "канонический PG id. Layered: KNOWN_BOOKS → fuzzy на SPGC "
        "metadata + user uploads. v4 planner вызывает это перед "
        "book-tools (book_readability, book_emotion_profile, …)."
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
    # v5 Phase 1.5 — delegate to entity_resolver when feature flag on.
    if _v5_enabled():
        return _v5_resolve_book(query, author_hint=author)

    q = _norm_query(query)
    if not q:
        return ToolResult.fail(
            tool="resolve_book_title", err_type="invalid_args",
            message="query is required",
        )

    # Layer 1: curated KNOWN_BOOKS exact match
    try:
        from scripts.v2.planner.entities import KNOWN_BOOKS
    except ImportError:
        KNOWN_BOOKS = {}

    for k in {q, " ".join(q.split())}:
        if k in KNOWN_BOOKS:
            pg, canonical = KNOWN_BOOKS[k]
            return ToolResult.success(
                tool="resolve_book_title",
                data={
                    "pg_id": pg or None,
                    "title": canonical,
                    "confidence": 1.0 if pg else 0.8,
                    "source": "known_books",
                    "candidates": [{"pg_id": pg or "", "title": canonical,
                                      "score": 100}],
                    "query": query,
                },
                coverage=Coverage(books_matched=1, books_total=1),
                warnings=([] if pg else [ToolWarning(
                    code="copyright",
                    message=f"{canonical} is in KNOWN_BOOKS but not in SPGC "
                            f"(copyright); pg_id is None",
                )]),
                query={"query": query, "author": author or None},
            )

    # Layer 2: delegate to v1 find_book and pick the best match.
    try:
        from scripts.rag_tools import find_book as _v1_find_book
    except ImportError as e:
        return ToolResult.fail(
            tool="resolve_book_title", err_type="internal",
            message=f"v1 unavailable: {e}",
            query={"query": query},
        )

    raw = _v1_find_book(title=query, author=author or "", top=5, lang="en")
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="resolve_book_title", err_type="internal",
            message=str(raw["error"]),
            query={"query": query, "author": author or None},
        )
    matches = (raw.get("matches") if isinstance(raw, dict) else None) or []
    if not matches:
        return ToolResult.fail(
            tool="resolve_book_title", err_type="not_found",
            message=f"no book matched {query!r}",
            details={"query": query, "author": author or None},
        )

    # Score: top match wins outright if there's a clear gap on downloads,
    # otherwise we report ambiguity.
    best = matches[0]
    second = matches[1] if len(matches) > 1 else None
    best_dl = int(best.get("downloads") or 0)
    second_dl = int((second.get("downloads") or 0)) if second else 0
    gap_ratio = (best_dl / second_dl) if second_dl else 999.0
    confidence = 0.85 if gap_ratio >= 1.5 or not second else 0.55

    candidates = [
        {"pg_id": str(m.get("id") or ""), "title": m.get("title") or "",
          "author": m.get("author") or "", "downloads": m.get("downloads"),
          "score": int(min(100, 50 + m.get("downloads", 0) // 1000))}
        for m in matches
    ]

    return ToolResult.success(
        tool="resolve_book_title",
        data={
            "pg_id": str(best.get("id") or ""),
            "title": best.get("title") or query,
            "author": best.get("author") or "",
            "confidence": confidence,
            "source": "find_book_fuzzy",
            "candidates": candidates,
            "query": query,
        },
        warnings=([ToolWarning(
            code="ambiguous",
            message=f"top-2 candidates within 1.5x downloads ratio; "
                      f"renderer should ask user to confirm",
        )] if confidence < 0.7 else []),
        coverage=Coverage(books_matched=len(matches), books_total=len(matches)),
        query={"query": query, "author": author or None},
    )


# =====================================================================
# v5 Phase 1.5 — entity_resolver delegation
# =====================================================================

def _v5_enabled() -> bool:
    """Phase 1.5 — gate v5 path behind env var. Default off until
    behavioural goldens (test_golden_v5.py Tier 2) pass on prod.
    Test code can set this via WC_V5_RESOLVER=on.
    """
    import os
    return os.environ.get("WC_V5_RESOLVER", "off") == "on"


def _v5_resolve_author(query: str) -> ToolResult:
    """Delegate to entity_resolver.resolve_author. Emits both legacy
    fields (for downstream tools that read `data.author_regex`) AND a
    typed view (for Phase 2 view-aware renderer).
    """
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
        # Even on fail, attach a view so Phase 2 callers can show
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
            "source": f"v5/{resolved.get('source', 'unknown')}",
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
    # Resolved view goes alongside legacy data fields — used by Phase 2
    # composite views; standalone callers can ignore.
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


def _v5_resolve_book(query: str, *, author_hint: str = "") -> ToolResult:
    """Delegate to entity_resolver.resolve_book. Same shape as
    resolve_author — legacy fields preserved, typed view added."""
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
