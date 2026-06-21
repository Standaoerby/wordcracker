"""v2 word_collocates.

Sprint 16 Phase C adds metric-based reranking. v1 returns raw window
co-occurrence counts; v2 wrapper can optionally:
  1) re-read .counts files for the same book set to get marginal
     frequencies (c(target), c(neighbor) per scope) + total tokens N
  2) hand the joined data to a scoring plugin (`pmi` / `npmi` / `dice`)
  3) filter pairs below min_cooccurrence and rerank by the chosen score

Math lives in scripts.v2.scoring.{PMI,NPMI,Dice} — pure functions,
unit-tested without I/O. This module is the data-fetch layer that
feeds them.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1WordCollocates
from scripts.keyness import MIN_LL_P0001  # χ²₁ p<0.0001 = 15.13 (single-source)


# W-15 (2026-05-24) — defense-in-depth at the v2 wrapper layer.
# v1 STOPWORDS (~70 tokens) doesn't include obvious high-rank fillers
# like off/under/there/all/one. v1 now also filters against
# _HIGH_FREQ_NEIGHBOR_DROP (rag_tools.py:2033), but the v2 wrapper
# repeats the check so that:
#   1) stale cache entries written before the v1 fix get re-filtered
#      on read (cache key is wrapper_version-scoped);
#   2) we never re-leak when v1 contract drifts.
# Import lazily to avoid circular and to pick up live v1 updates.
def _wrapper_stopword_set() -> frozenset[str]:
    try:
        from scripts.rag_tools import STOPWORDS, _HIGH_FREQ_NEIGHBOR_DROP
        return frozenset(STOPWORDS) | frozenset(_HIGH_FREQ_NEIGHBOR_DROP)
    except Exception:
        # Dev box without rag_tools — fallback to the minimal set that
        # covers the W-15 prod report (off/all/under/there/one).
        return frozenset({
            "the", "a", "an", "of", "and", "to", "in", "is", "it",
            "off", "all", "under", "there", "one", "also", "even",
            "very", "much", "many", "more", "most", "some", "any",
            "back", "way", "first", "last", "every", "another",
        })


# E40 follow-up: the COLLOCATES view stores the active metric's score in a
# single payload slot, but the rendered column header must NAME the metric
# the user actually got (G²/logDice ≠ NPMI). word_collocates passes the
# display label; emotion_collocates / count fall through to the "NPMI"
# default so their rendered output is unchanged.
_METRIC_COLUMN_LABEL = {
    "npmi": "NPMI", "pmi": "PMI", "dice": "Dice",
    "loglikelihood": "G²", "logdice": "logDice",
}


@tool(
    name="word_collocates",
    category="words",
    description=("Слова в окне ±N токенов вокруг target word в scope. "
                 "Опциональный metric=pmi|npmi|dice|loglikelihood|logdice для "
                 "рерэнка по силе ассоциации (npmi/pmi/dice) или статистической "
                 "значимости (loglikelihood=G², logdice) вместо сырого count."),
    input_schema={
        "type": "object",
        "properties": {
            "scope":              {"type": "object"},
            "word":               {"type": "string"},
            "window":             {"type": "integer", "description": "default 4"},
            "top":                {"type": "integer", "description": "default 20"},
            "exclude_stopwords":  {"type": "boolean", "description": "default true"},
            "max_books":          {"type": "integer", "description": "default 8000"},
            "metric":             {"type": "string",  "description": "count|pmi|npmi|dice|loglikelihood|logdice (default npmi — W-15)"},
            "min_cooccurrence":   {"type": "integer", "description": "filter pairs with c(t,w) below this (default 5)"},
            "min_ll":             {"type": "number",  "description": "G² floor for metric=loglikelihood (default 15.13 ≈ χ²₁ p<0.0001)"},
        },
        "required": ["scope", "word"],
    },
    requires=["word", "scope"],
    cost="medium",
    cacheable=True,
    # W-15 (2026-05-23) — default metric flipped count → npmi so the
    # rendered table sorts by association strength rather than raw counts
    # (which were dominated by stop-words like «the/of/and» even with
    # exclude_stopwords on). Bump wrapper_version so old npmi=None rows
    # in cache get recomputed.
    # W-15 polish (2026-05-24) — wrapper-level stopword filter added
    # (defense-in-depth over v1 _HIGH_FREQ_NEIGHBOR_DROP).
    # Collocation-significance (2026-06-21) — +loglikelihood (G²) / logdice
    # opt-in metrics + min_ll floor. Bump so cached top-lists recompute
    # under the new metric dispatch. Keep "w15" in the label: the W-15
    # stopword filter is still active above, and W15WrapperVersionBumped
    # locks that lineage (cache-invalidation provenance).
    wrapper_version="v5-w15-significance-metrics",
)
@v1_contract(v1_fn="scripts.rag_tools.word_collocates",
             schema=V1WordCollocates)
def word_collocates(scope, word: str, window: int = 4, top: int = 20,
                    exclude_stopwords: bool = True,
                    max_books: int = 8000,
                    metric: str = "npmi",
                    min_cooccurrence: int = 5,
                    min_ll: float = MIN_LL_P0001) -> ToolResult:
    # Late binding via fresh import so tests can `mock.patch
    # ("scripts.rag_tools.word_collocates")` without re-loading the
    # wrapper module. The top-level import above is kept solely to bind
    # the v1 ref into the contract registry at decoration time.
    from scripts.rag_tools import word_collocates as _v1

    # For metric ranking we ask v1 for a wider candidate pool so the
    # metric has room to reorder. The wrapper trims back to `top` after
    # filtering by min_cooccurrence.
    v1_top = max(top * 5, 100) if metric != "count" else top

    raw = _v1(scope=scope, word=word, window=window, top=v1_top,
              exclude_stopwords=exclude_stopwords, max_books=max_books)
    query = {"scope": scope, "word": word, "window": window, "top": top,
             "metric": metric, "min_cooccurrence": min_cooccurrence}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_collocates",
                               err_type="invalid_args" if "scope" in str(raw["error"]).lower()
                                                       else "not_found",
                               message=str(raw["error"]), query=query)

    # Phase 2 — V1WordCollocates canonical key is `top_collocates`
    # (rag_tools.py:1083). The pre-contract fallback to `top` is gone.
    rows = (raw.get("top_collocates") if isinstance(raw, dict) else None) or []

    # W-15 polish (2026-05-24) — wrapper-level stopword filter. Drops
    # rows whose `word` is in the v1 STOPWORDS ∪ _HIGH_FREQ_NEIGHBOR_DROP
    # union BEFORE NPMI scoring, so the scoring budget isn't spent on
    # tokens the user will never want to see (off/under/there/all/one)
    # and so the count-only fallback path (no marginals) also stays
    # clean. Skipped when exclude_stopwords=False (caller explicitly
    # asked for the raw window).
    if exclude_stopwords:
        _drop = _wrapper_stopword_set()
        target_lc = (word or "").strip().lower()
        rows = [r for r in rows
                if isinstance(r, dict)
                and (r.get("word") or "").strip().lower() not in _drop
                and (r.get("word") or "").strip().lower() != target_lc
                and len((r.get("word") or "").strip()) >= 2]
        if isinstance(raw, dict):
            raw["top_collocates"] = rows
    # Phase 2 — v1 word_collocates does NOT return books_capped /
    # books_total (only scope, word, window, total_occurrences,
    # books_with_hits, top_collocates per V1WordCollocates). Previous
    # warnings block read phantom keys; removed per R3.
    warnings: list[ToolWarning] = []

    # ---- Optional metric reranking ----
    metric_lc = (metric or "count").lower()
    if metric_lc != "count" and rows:
        try:
            from scripts.v2.scoring import REGISTRY as _SCORING, ScoringQuery
            plugin = _SCORING.get(metric_lc)
            if plugin is None or "word_pair" not in getattr(plugin, "kinds", ()):
                warnings.append(ToolWarning(
                    code="metric_unavailable",
                    message=f"metric {metric_lc!r} not in scoring REGISTRY",
                ))
            else:
                augmented = _augment_with_marginals(
                    scope=scope, word=word.strip().lower(),
                    candidate_rows=rows, max_books=max_books,
                )
                if augmented is None:
                    warnings.append(ToolWarning(
                        code="marginals_unavailable",
                        message="counts files not readable; falling back to raw count order",
                    ))
                else:
                    candidates_in, c_target, N, n_books_scanned = augmented
                    # Apply min_cooccurrence floor before scoring — saves
                    # a bit of arithmetic and suppresses noise floor pairs.
                    candidates_in = [c for c in candidates_in
                                      if c.get("c_pair", 0) >= min_cooccurrence]
                    scored = plugin.compute(ScoringQuery(
                        kind="word_pair", target=word,
                        candidates=candidates_in,
                        options={"c_target": c_target, "N": N,
                                 "window": window},
                    ))
                    # G² significance floor — only loglikelihood. Drop pairs
                    # below the χ²₁ critical value (default p<0.0001) BEFORE
                    # trimming to top, so the top-N is significance-clean.
                    if metric_lc == "loglikelihood":
                        scored = [s for s in scored if s.score >= min_ll]
                    if scored:
                        new_rows = []
                        for s in scored[:top]:
                            new_rows.append({
                                "word":   s.id,
                                "count":  s.extra.get("c_pair"),
                                "scope_count": s.extra.get("c_neighbor"),
                                metric_lc: round(s.score, 4),
                                **{k: v for k, v in s.extra.items()
                                   if k not in ("c_pair", "c_neighbor")},
                            })
                        # Mutate raw dict to surface the reranked list
                        # under the canonical v1 key.
                        if isinstance(raw, dict):
                            raw["top_collocates"] = new_rows
                            raw["metric"] = metric_lc
                            raw["min_cooccurrence"] = min_cooccurrence
                            if metric_lc == "loglikelihood":
                                raw["min_ll"] = min_ll
                            raw["scope_total_tokens"] = N
                            raw["scope_target_count"] = c_target
                            raw["scope_books_scanned"] = n_books_scanned
                    else:
                        warnings.append(ToolWarning(
                            code="metric_no_results",
                            message=f"{metric_lc} returned no scored pairs "
                                    "(min_cooccurrence too high or marginals zero)",
                        ))
        except Exception as e:
            warnings.append(ToolWarning(
                code="metric_failed",
                message=f"{metric_lc}: {type(e).__name__}: {e}",
            ))

    # books_with_hits is the canonical books_matched proxy in v1.
    result = ToolResult.success(
        tool="word_collocates", data=raw,
        coverage=Coverage(
            books_matched=(raw.get("books_with_hits", -1)
                            if isinstance(raw, dict) else -1),
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )

    # v5 Phase 2.5 — COLLOCATES view.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        rows_after_metric = (raw.get("top_collocates")
                              if isinstance(raw, dict) else None) or []
        from scripts.v2.tools._normalize import scope_book_id
        _book = scope_book_id(scope) if isinstance(scope, dict) else None
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {_book}" if _book
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        collocates = []
        # E40 (2026-05-22) — Stan prod «что соседствует со словом fog»:
        # NPMI column showed identical to count column (713.000 / 713)
        # because default metric="count" → metric_lc="count" → line
        # `c.get(metric_lc)` returned the count value as npmi. NPMI is
        # only meaningful when a real metric rerank ran (metric != "count").
        # When no rerank, leave npmi=None so template renders «—».
        for c in rows_after_metric[:top]:
            if not isinstance(c, dict):
                continue
            # Reranked rows (line ~128 above) stamp the score under the
            # active metric's name (`npmi`/`pmi`/`dice`/...). Read that
            # one key — the pre-Phase-2 npmi/score fallback chain masked
            # the case where metric != npmi.
            score_val = None
            if metric_lc != "count":
                score_val = c.get(metric_lc)
            # Rerank builder stamps `count` (= c_pair) onto every row;
            # raw v1 rows already carry `count`. One canonical key.
            collocates.append({
                "token": c.get("word") or "—",
                "npmi": score_val,
                "count": c.get("count"),
            })
        view = vb.build_collocates(
            word=word,
            collocates=collocates,
            window=window,
            scope_label=scope_str,
            metric_label=_METRIC_COLUMN_LABEL.get(metric_lc, "NPMI"),
            language="ru",
        )
        validity = DataValidity.OK if collocates else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.collocates").exception(
            "word_collocates view emission failed"
        )
    return result


def _augment_with_marginals(
    scope: Any, word: str, candidate_rows: list[dict],
    max_books: int,
) -> tuple[list[dict], int, int, int] | None:
    """Read counts files for scope's books, sum per-word frequencies.

    Returns (candidates_with_c_neighbor, c_target, N_total_tokens, n_books)
    or None if counts files not accessible (dev box without /workspace).

    Each input row is {"word": w, "count": c_pair} from v1; we annotate
    it with `c_neighbor` (total scope frequency of w) and pass through.
    """
    try:
        from scripts.rag_tools import _counts_path, _select_books
    except ImportError:
        return None
    # Resolve scope → book_ids
    book_ids: list[str] = []
    try:
        if isinstance(scope, dict) and scope.get("book"):
            pg = scope["book"].upper()
            if not (pg.startswith("PG") or pg.startswith("U")):
                pg = f"PG{pg}"
            book_ids = [pg]
        elif isinstance(scope, dict) and scope.get("author"):
            sel = _select_books(
                scope["author"],
                year_from=scope.get("year_from"),
                year_to=scope.get("year_to"),
                country=scope.get("country"),
            )
            if len(sel) == 0:
                return None
            try:
                import pandas as pd
                sel = sel.copy()
                sel["downloads"] = pd.to_numeric(
                    sel.get("downloads"), errors="coerce").fillna(0)
                sel_sorted = sel.sort_values("downloads", ascending=False)
                if len(sel_sorted) > max_books:
                    sel_sorted = sel_sorted.head(max_books)
                book_ids = list(sel_sorted["id"])
            except Exception:
                book_ids = list(sel["id"])[:max_books]
        else:
            return None
    except Exception:
        return None
    if not book_ids:
        return None

    # Build a set of neighbor words we care about — saves memory for
    # large vocab counts files since we only need ~100-500 candidates.
    want = {(r.get("word") or "").strip().lower()
             for r in candidate_rows if r.get("word")}
    if not want:
        return None

    c_target = 0
    c_neighbor: dict[str, int] = {w: 0 for w in want}
    N_total = 0
    n_books_scanned = 0
    for pg in book_ids:
        cf = _counts_path(pg)
        if not cf.exists():
            continue
        try:
            with open(cf, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c_str = parts[0].lower(), parts[1]
                    try:
                        c = int(c_str)
                    except ValueError:
                        continue
                    N_total += c
                    if w == word:
                        c_target += c
                    elif w in want:
                        c_neighbor[w] = c_neighbor.get(w, 0) + c
            n_books_scanned += 1
        except Exception:
            continue

    if n_books_scanned == 0 or N_total == 0 or c_target == 0:
        return None

    out_candidates: list[dict] = []
    for r in candidate_rows:
        w = (r.get("word") or "").strip().lower()
        if not w:
            continue
        cn = c_neighbor.get(w, 0)
        if cn <= 0:
            continue
        out_candidates.append({
            "word":       w,
            "c_pair":     r.get("count", 0),
            "c_neighbor": cn,
        })
    return out_candidates, c_target, N_total, n_books_scanned
