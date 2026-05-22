"""v2 author_profile + author_influences + author_attribution — Burrows Delta + combo."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1AuthorProfile, V1AuthorInfluences, V1AuthorAttribution,
)


@tool(
    name="author_profile",
    category="authors",
    description="Combo: metadata + stats + signature + bigrams + diversity + influences + emotions. Параллельно.",
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "country":      {"type": "string", "description": "optional country filter"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="heavy",
    cacheable=True,
    # Phase 5: per-tool timeout removed (was 60s = default anyway).
    # Effective cap via chokepoint = min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining).
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.author_profile",
             schema=V1AuthorProfile)
def author_profile(author_regex: str, country: str | None = None) -> ToolResult:
    # Sprint 7.2: AuthorProfile SQLite store is the fast path. Re-asks for
    # the same author return in <5ms instead of paying the ~11s parallel
    # rebuild via v1.author_profile. The store is corpus_version-tagged so
    # stale entries get rebuilt automatically on corpus updates.
    try:
        from scripts.v2.profiles import author as profile_mod
        cached = profile_mod.get_or_build(author_regex, country=country)
    except Exception:
        cached = None
    query = {"author_regex": author_regex, "country": country}
    if cached is not None:
        md = cached.get("metadata") or {}
        result = ToolResult.success(
            tool="author_profile", data=cached,
            coverage=Coverage(
                books_matched=md.get("books_matched", -1),
                books_total=-1,
            ),
            query=query,
        )
        _attach_author_profile_view(result, cached, author_regex)
        return result
    # Cache miss + build failed → fall through to direct v1 call so we
    # at least return *something* useful instead of bubbling up an error.
    from scripts.rag_tools import author_profile as _v1
    raw = _v1(author_regex=author_regex, country=country)
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_profile", err_type="not_found",
                               message=str(raw["error"]), query=query)
    md = (raw.get("metadata") if isinstance(raw, dict) else None) or {}
    # V1AuthorMetadata canonical key is books_matched; metadata sub-dict
    # mirrors that shape.
    result = ToolResult.success(
        tool="author_profile", data=raw,
        coverage=Coverage(
            books_matched=md.get("books_matched", -1), books_total=-1,
        ),
        query=query,
    )
    _attach_author_profile_view(result, raw, author_regex)
    return result


@tool(
    name="author_influences",
    category="authors",
    description="Близкие по стилю авторы по Burrows Delta. Closest neighbours.",
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string"},
            "top":          {"type": "integer", "description": "default 10"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.author_influences",
             schema=V1AuthorInfluences)
def author_influences(author_regex: str, top: int = 10) -> ToolResult:
    from scripts.rag_tools import author_influences as _v1
    # Sprint 16 Phase B1: ask v1 for more candidates than `top` so that
    # after filtering aggregate buckets we still have `top` valid rows.
    # Stan round 6 R19: Doyle/Poe returned identical top — Burrows Delta
    # baseline noise. Pulling top*3 and filtering aggregate authors gives
    # the actual stylistic neighbours, not corpus-mean-similar ones.
    raw = _v1(author_regex=author_regex, top=top * 3)
    query = {"author_regex": author_regex, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_influences", err_type="not_found",
                               message=str(raw["error"]), query=query)
    # Phase 2 — V1AuthorInfluences canonical key is `top`. The phantom
    # `closest`/`neighbours`/`authors` aliases never existed in v1.
    if isinstance(raw, dict):
        lst = raw.get("top")
        if isinstance(lst, list):
            filtered = [r for r in lst if not _is_collection_bucket(r)]
            raw["top"] = filtered[:top]

        # Sprint 16 Phase B2: confidence floor. If the top-N distances
        # are all near corpus baseline (small range), the «closest authors»
        # are just whoever happens to sit closest to mean — not real
        # stylistic siblings. Better to say so than mislead.
        _annotate_confidence(raw, author_regex)
        # Sprint 20+ B2 — metric_explanations so renderer doesn't reverse
        # Burrows Delta direction (Stan Round 11 Q19: «чем выше delta,
        # тем сильнее влияние» — НЕВЕРНО). Stamp authoritative direction.
        raw.setdefault("metric_explanations", []).extend([
            {"metric": "burrows_delta",
              "direction": "LOWER = closer style (distance metric, NOT influence strength)",
              "scale": "typically 0.3-1.5; <0.5 stylistic kin, >0.8 distant",
              "interpret": "Hawthorne 0.4506 closer to Melville's style than Stevenson 0.4763 — the LOWER, the more similar"},
            {"metric": "jaccard_top200",
              "direction": "HIGHER = more shared signature words (similarity, not distance)",
              "scale": "0-1; intersection ÷ union of top-200 affinity words",
              "interpret": "0.15 jaccard = 30 shared words out of 200"},
            {"metric": "ensemble_score",
              "direction": "LOWER = closer (Borda rank average of Burrows + Jaccard, normalized as distance)",
              "scale": "0-1",
              "interpret": "ensemble combines two metrics — robust against single-metric outliers"},
        ])
    result = ToolResult.success(
        tool="author_influences", data=raw,
        coverage=Coverage(),
        query=query,
    )
    _attach_author_influences_view(result, raw, author_regex, top)
    return result


def _annotate_confidence(raw: dict, author_regex: str) -> None:
    """Inspect Burrows Delta distances in the result. Mark low-confidence
    when the spread is small (top N all clustered near baseline).

    Heuristic: take top-N distances. If `(max - min) / median < 0.05`
    OR if all top distances are within ±0.02 of the median, mark as
    baseline-overlap. LLM render sees `confidence: low` + note → tells
    user honestly «no clear stylistic match, this author sits near
    corpus mean»."""
    rows = raw.get("top") if isinstance(raw.get("top"), list) else None
    if not rows:
        return
    distances: list[float] = []
    for r in rows[:10]:
        # V1AuthorInfluences row keys: author, delta, books_in_training.
        d = r.get("delta")
        if isinstance(d, (int, float)):
            distances.append(float(d))
    if len(distances) < 3:
        return
    distances.sort()
    median = distances[len(distances) // 2]
    spread = distances[-1] - distances[0]
    if median <= 0:
        return
    relative_spread = spread / median if median > 0 else 0
    confidence = "high"
    if relative_spread < 0.05 or spread < 0.02:
        confidence = "low"
    raw["similarity_confidence"] = confidence
    if confidence == "low":
        raw["_render_note"] = (
            f"Стилистический профиль автора {author_regex} находится "
            f"близко к среднему по корпусу — расстояния до top-N "
            f"кандидатов слабо отличаются (spread={spread:.3f}, "
            f"median={median:.3f}). Это не «эти авторы похожи стилем», "
            f"а «никто чётко не похож, все близко к baseline». "
            f"Скажи пользователю честно: clear stylistic siblings "
            f"не нашлось. Попробуй сравнить с конкретным кандидатом "
            f"через compare_authors."
        )


_COLLECTION_BUCKETS = frozenset({
    "various", "anonymous", "unknown", "n/a", "encyclop",
    "catholic church", "multiple", "collection", "compilation",
})


def _is_collection_bucket(row) -> bool:
    """Multi-author aggregate placeholders that pollute «closest by
    style» rankings. They show up because they have thousands of books
    pooled together with mean stylistic profile near everyone's center."""
    if not isinstance(row, dict):
        return False
    name = (row.get("author") or row.get("name") or "").lower().strip()
    if not name:
        return False
    return any(b in name for b in _COLLECTION_BUCKETS)


@tool(
    name="author_attribution",
    category="authors",
    description="Burrows Delta attribution: дан текст, найти top-N candidate авторов.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "сам текст (>= 500 слов)"},
            "top":  {"type": "integer", "description": "default 5"},
        },
        "required": ["text"],
    },
    requires=[],
    cost="medium",
    cacheable=False,  # texts are unique, no point caching
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.author_attribution",
             schema=V1AuthorAttribution)
def author_attribution(text: str, top: int = 5) -> ToolResult:
    from scripts.rag_tools import author_attribution as _v1
    raw = _v1(text=text, top=top)
    query = {"text_chars": len(text), "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="author_attribution", err_type="invalid_args",
                               message=str(raw["error"]), query=query)
    result = ToolResult.success(tool="author_attribution", data=raw, query=query)
    _attach_author_attribution_view(result, raw)
    return result


# =====================================================================
# v5 Phase 2.5 — view emission helpers
# =====================================================================


def _attach_author_profile_view(result, raw, author_regex: str) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return
        # V1AuthorProfile shape: metadata dict (from author_metadata),
        # signature dict (from affinity_by_author), top_bigrams,
        # diversity, influences, dominant_emotions — each is a nested
        # dict containing the relevant tool result.
        md = raw.get("metadata") or {}
        am = md.get("authors_matched") or []
        author_canonical = (
            (am[0] if am and isinstance(am[0], str) else None)
            or author_regex.lstrip("^").rstrip(",").strip()
        )
        sig_block = raw.get("signature") or {}
        sig_rows = sig_block.get("top") if isinstance(sig_block, dict) else []
        sig_words = []
        if isinstance(sig_rows, list):
            sig_words = [s.get("word") if isinstance(s, dict) else str(s)
                         for s in sig_rows]
        infl_block = raw.get("influences") or {}
        infl_rows = infl_block.get("top") if isinstance(infl_block, dict) else []
        infl_names = []
        if isinstance(infl_rows, list):
            infl_names = [i.get("author") if isinstance(i, dict) else str(i)
                          for i in infl_rows[:10]]
        div_block = raw.get("diversity") or {}
        diversity = (div_block.get("ttr_aggregate")
                     or div_block.get("ttr")) if isinstance(div_block, dict) else None
        view = vb.build_top_n_table(  # use TOP_N as fallback since composite stub
            rows=[],
            columns=["x"],
            empty_reason=None,
            empty_message_ru=None,
            empty_message_en=None,
        ) if False else None    # placeholder — using AUTHOR_PROFILE below

        from scripts.v2.view_types import RenderableView, ViewType
        view = RenderableView(
            view_type=ViewType.AUTHOR_PROFILE,
            payload={
                "author_canonical": author_canonical,
                "metadata": {
                    "birth_year": md.get("year_of_birth_min"),
                    "death_year": md.get("year_of_death_max"),
                    "books_in_corpus": md.get("books_matched"),
                },
                "signature_words": sig_words,
                "lexical_diversity": diversity,
                "influences": infl_names,
            },
            headline=f"Профиль автора — {author_canonical}",
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.author_profile").exception(
            "author_profile view emission failed"
        )


def _attach_author_influences_view(result, raw, author_regex: str,
                                     top: int) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import (
            DataValidity, EmptyReason, RenderableView, ViewType,
        )
        if not isinstance(raw, dict):
            return
        rows = raw.get("top") if isinstance(raw.get("top"), list) else None
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "author", "delta"],
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru="Стилистические соседи не найдены.",
                empty_message_en="No stylistic neighbours.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return

        author_name = author_regex.lstrip("^").rstrip(",").strip()
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1AuthorInfluences rows: author, delta, books_in_training.
            d = r.get("delta")
            view_rows.append({
                "rank": i,
                "author": r.get("author") or "—",
                "delta": (f"{d:.4f}" if isinstance(d, (int, float)) else "—"),
            })
        confidence = raw.get("similarity_confidence")
        caveats = []
        if confidence == "low":
            caveats.append(
                "Уверенность: НИЗКАЯ — все top-N кандидатов плотно "
                "сидят у corpus baseline. Это не «эти авторы похожи», "
                "а «никто чётко не похож»."
            )
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "author", "delta"],
            headline=f"Стилистические соседи — {author_name}",
            requested_n=top,
            caveats=caveats,
            provenance=vb.make_provenance(
                requested={"author_regex": author_regex, "top": top},
                returned={"count": len(view_rows),
                          "similarity_confidence": confidence},
                sources=["Burrows Delta on top-200 function words"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.author_profile").exception(
            "author_influences view emission failed"
        )


def _attach_author_attribution_view(result, raw) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return
        # V1AuthorAttribution canonical key is `top` with rows
        # {author, delta, books_in_training} (rag_tools.py:1739).
        cands_raw = raw.get("top") or []
        cands = []
        for c in cands_raw[:10]:
            if not isinstance(c, dict):
                continue
            cands.append({
                "author": c.get("author") or "—",
                "score": c.get("delta"),
                "books_matched": c.get("books_in_training"),
            })
        view = vb.build_attribution_result(
            candidates=cands,
            primary_metric="Burrows Delta",
            primary_metric_explanation={
                "direction": "LOWER = closer match",
                "scale": "0..2 typically; <0.5 strong match",
            },
            headline="Атрибуция авторства (Burrows Delta)",
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.author_profile").exception(
            "author_attribution view emission failed"
        )
