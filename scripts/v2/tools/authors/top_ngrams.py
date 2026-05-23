"""v2 top_ngrams_by_author + lexical_diversity."""
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
    V1TopNgramsByAuthor, V1LexicalDiversity,
)


@tool(
    name="top_ngrams_by_author",
    category="authors",
    description=(
        "Топ N-грамм у автора. n=1 unigrams, n=2 bigrams, n=3 trigrams. "
        "Используй для «фирменные обороты», «частые связки слов», «биграммы X». "
        "Передай author_regex='.*' если фильтруешь только по period/country. "
        "Параметр semantic_class фильтрует результат по closed-list лексикону "
        "(сейчас поддерживается 'motion' для глаголов движения)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex":   {"type": "string"},
            "n":              {"type": "integer", "description": "1, 2 или 3"},
            "top":             {"type": "integer", "description": "default 20"},
            "pos_filter":      {"type": "array", "items": {"type": "string"}},
            "year_from":       {"type": "integer"},
            "year_to":         {"type": "integer"},
            "country":         {"type": "string"},
            "semantic_class":  {"type": "string",
                                 "description": "filter result by lexicon: "
                                 "'motion' (motion verbs). Optional."},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="heavy",
    cacheable=True,
    # Phase 5: per-tool timeout override removed (was 120s). Effective cap =
    # min(DEFAULT_TOOL_TIMEOUT_S, request_budget.remaining) via chokepoint.
    # E18 (2026-05-22) — E15 now reads v1's «top» key first (was reading
    # phantom «top_ngrams» only → empty view). Bump to invalidate stale.
    # Phase 3 W-9 (2026-05-22) — added direct-scan fallback for
    # semantic_class=motion. Bump invalidates cache that returned
    # empty for «глаголы движения у Диккенса» whenever the affinity-
    # ranked top-N intersected MOTION_VERBS at 0.
    wrapper_version="v5-phase3-motion-fallback",
)
@v1_contract(v1_fn="scripts.rag_tools.top_ngrams_by_author",
             schema=V1TopNgramsByAuthor)
def top_ngrams_by_author(author_regex: str, n: int = 2, top: int = 20,
                         pos_filter=None, year_from=None, year_to=None,
                         country=None, semantic_class: str | None = None) -> ToolResult:
    from scripts.rag_tools import top_ngrams_by_author as _v1

    # E2 (R-22 P2): when semantic_class is set, pull a wider top from v1
    # (lexicon filter will narrow). Without this, top=25 might contain
    # 0 motion verbs (Dickens' top verbs are «said», «replied», «cried»).
    raw_top = top
    if semantic_class:
        raw_top = max(top * 8, 200)  # pull enough to find motion verbs

    raw = _v1(author_regex=author_regex, n=n, top=raw_top, pos_filter=pos_filter,
              year_from=year_from, year_to=year_to, country=country)
    query = {"author_regex": author_regex, "n": n, "top": top,
             "pos_filter": pos_filter, "year_from": year_from,
             "year_to": year_to, "country": country,
             "semantic_class": semantic_class}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="top_ngrams_by_author", err_type="not_found",
                               message=str(raw["error"]), query=query)
    # Phase 2 — V1TopNgramsByAuthor canonical key is `top` (rag_tools.py
    # line ~597). Phantom `top_ngrams` fallback removed per R3.
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []

    # E2: apply semantic-class lexicon filter
    semantic_fallback_used = False
    if semantic_class and semantic_class.lower() == "motion":
        from scripts.v2.tools.authors._motion_verbs import (
            count_motion_verbs_in_author,
            filter_motion_verbs,
        )
        before = len(rows)
        rows = filter_motion_verbs(rows, word_key="ngram")[:top] if rows else []
        # Phase 3 W-9 (Stan 2026-05-22) — when affinity-ranked top-N
        # has zero motion-verb intersection (Dickens' top verbs are
        # dialogue tags «said»/«replied», not motion), fall back to a
        # direct corpus scan: count MOTION_VERBS occurrences in the
        # author's books and rank by count. Guarantees a non-empty
        # answer for «глаголы движения у X» whenever the author exists
        # in the corpus.
        if not rows and n == 1:
            scan_rows = count_motion_verbs_in_author(
                author_regex=author_regex,
                year_from=year_from, year_to=year_to,
                country=country, top=top,
            )
            if scan_rows:
                rows = scan_rows
                semantic_fallback_used = True
        if isinstance(raw, dict):
            raw["top"] = rows
            raw["_semantic_filter"] = {
                "class": "motion",
                "before": before,
                "after": len(rows),
                "fallback_direct_scan": semantic_fallback_used,
            }
    # Other classes can be added later (emotion / cognition / speech)

    warnings = []
    if not rows:
        if semantic_class:
            warnings.append(ToolWarning(
                "no_lexicon_matches",
                f"no {semantic_class}-class words found for this author — "
                f"checked top-{raw_top} affinity + direct corpus scan",
            ))
        else:
            warnings.append(ToolWarning("empty_top",
                                         "no ngrams matched filters"))
    elif semantic_fallback_used:
        # W-9 disclosure: surface that we switched ranking strategy. The
        # user asked for «глаголы движения у X»; we returned a list
        # ranked by raw count over the author's books, not by affinity.
        warnings.append(ToolWarning(
            "semantic_fallback_used",
            f"semantic_class={semantic_class!r}: ranking by raw count "
            f"over author's books (no affinity intersect with lexicon)",
        ))

    result = ToolResult.success(
        tool="top_ngrams_by_author", data=raw,
        coverage=Coverage(books_matched=raw.get("books_used", -1)
                                       if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=warnings,
        query=query,
    )
    _attach_top_ngrams_view(result, rows, author_regex, n, top)
    return result


@tool(
    name="lexical_diversity",
    category="authors",
    description=(
        "Лексическая разнообразность: TTR + per-book averages. "
        "Используй для «какая лексическая плотность у X», «насколько разнообразный словарь»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {"type": "object",
                      "description": "{'book': PGid} | {'author': regex} | 'all_corpus'"},
        },
        "required": ["scope"],
    },
    requires=["scope"],
    cost="medium",
    cacheable=True,
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.lexical_diversity",
             schema=V1LexicalDiversity)
def lexical_diversity(scope) -> ToolResult:
    from scripts.rag_tools import lexical_diversity as _v1
    raw = _v1(scope=scope)
    query = {"scope": scope}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="lexical_diversity",
            err_type=("invalid_args" if "scope" in err.lower() else "not_found"),
            message=err, query=query,
        )
    # V1LexicalDiversity exposes books_used (per-author scope) — no
    # generic books_total. Per-book / all_corpus scopes don't report
    # a book count.
    n_books = (raw.get("books_used", -1)
                if isinstance(raw, dict) else -1)
    result = ToolResult.success(
        tool="lexical_diversity", data=raw,
        coverage=Coverage(books_matched=n_books, books_total=-1),
        query=query,
    )
    _attach_lexical_diversity_view(result, raw, scope)
    return result


# =====================================================================
# v5 Phase 2.5 — view emission helpers
# =====================================================================


def _attach_top_ngrams_view(result, rows, author_regex: str,
                              n: int, top: int) -> None:
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        author_name = author_regex.lstrip("^").rstrip(",").strip()
        ngram_kind = {1: "Униграммы", 2: "Биграммы", 3: "Триграммы"}.get(n, f"{n}-граммы")
        headline = f"{ngram_kind} — {author_name}"
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "ngram", "count"],
                headline=headline,
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=f"Для {author_name} не нашлось {ngram_kind.lower()} при текущих фильтрах.",
                empty_message_en="No ngrams matched filters.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1TopNgramsByAuthor row_keys: ngram, count.
            view_rows.append({
                "rank": i,
                "ngram": r.get("ngram") or "—",
                "count": r.get("count") or "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "ngram", "count"],
            headline=headline,
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.top_ngrams").exception(
            "top_ngrams_by_author view emission failed"
        )


def _attach_lexical_diversity_view(result, raw, scope) -> None:
    """lexical_diversity emits a TOP_N_TABLE of per-book TTRs +
    aggregate metric — repurposing TOP_N_TABLE since there's no
    dedicated LEXICAL_DIVERSITY view_type (could add in 3.5)."""
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return
        # V1LexicalDiversity is scope-polymorphic (rag_tools.py:891/907/930):
        #   * `{"book": pg}` / `"all_corpus"` → returns `ttr`
        #   * `{"author": rx}`              → returns `ttr_aggregate`
        # Pick by scope shape so the wrapper reads exactly one declared
        # key per branch — R3 compliant, no .get()-or fallback.
        is_author_scope = (isinstance(scope, dict)
                           and bool(scope.get("author")))
        ttr = (raw.get("ttr_aggregate") if is_author_scope
               else raw.get("ttr"))
        per_book = raw.get("top_5_most_varied") or []
        from scripts.v2.tools._normalize import scope_book_id
        _book = scope_book_id(scope) if isinstance(scope, dict) else None
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {_book}" if _book
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "весь корпус")
        if not per_book and ttr is None:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "book", "ttr"],
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru=f"Для {scope_str} нет данных лексической разнообразности.",
                empty_message_en="No lexical diversity data.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return
        view_rows = []
        for i, b in enumerate(per_book[:30], start=1):
            if not isinstance(b, dict):
                continue
            # V1LexicalDiversity per-book rows: pg_id, tokens, types, ttr.
            view_rows.append({
                "rank": i,
                "book": b.get("pg_id") or "—",
                "ttr": (f"{b.get('ttr'):.3f}" if isinstance(b.get("ttr"), (int, float)) else "—"),
            })
        caveats = []
        if ttr is not None:
            caveats.append(f"Aggregate TTR: {ttr:.3f}" if isinstance(ttr, (int, float))
                            else f"Aggregate TTR: {ttr}")
        view = vb.build_top_n_table(
            rows=view_rows or [{"rank": 1, "book": scope_str, "ttr": f"{ttr}"}],
            columns=["rank", "book", "ttr"],
            headline=f"Лексическая разнообразность — {scope_str}",
            caveats=caveats,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.top_ngrams").exception(
            "lexical_diversity view emission failed"
        )
