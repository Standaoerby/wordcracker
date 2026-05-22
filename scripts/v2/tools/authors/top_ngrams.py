"""v2 top_ngrams_by_author + lexical_diversity."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


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
    timeout_s=120,
    # wrapper_version bumped because semantic_class param changes cache key
    wrapper_version="v2-sem-class",
)
def top_ngrams_by_author(author_regex: str, n: int = 2, top: int = 20,
                         pos_filter=None, year_from=None, year_to=None,
                         country=None, semantic_class: str | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import top_ngrams_by_author as _v1
    except ImportError as e:
        return ToolResult.fail(tool="top_ngrams_by_author", err_type="internal",
                               message=f"v1 unavailable: {e}")

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
    rows = (raw.get("top_ngrams") if isinstance(raw, dict) else None) or []

    # E2: apply semantic-class lexicon filter
    if semantic_class and rows:
        if semantic_class.lower() == "motion":
            from scripts.v2.tools.authors._motion_verbs import filter_motion_verbs
            before = len(rows)
            rows = filter_motion_verbs(rows, word_key="ngram")[:top]
            if isinstance(raw, dict):
                raw["top_ngrams"] = rows
                raw["_semantic_filter"] = {
                    "class": "motion",
                    "before": before,
                    "after": len(rows),
                }
        # Other classes can be added later (emotion / cognition / speech)

    warnings = []
    if not rows:
        if semantic_class:
            warnings.append(ToolWarning(
                "no_lexicon_matches",
                f"no {semantic_class}-class words found in top "
                f"{raw_top} ngrams for this author",
            ))
        else:
            warnings.append(ToolWarning("empty_top",
                                         "no ngrams matched filters"))

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
)
def lexical_diversity(scope) -> ToolResult:
    try:
        from scripts.rag_tools import lexical_diversity as _v1
    except ImportError as e:
        return ToolResult.fail(tool="lexical_diversity", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope)
    query = {"scope": scope}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="lexical_diversity",
            err_type=("invalid_args" if "scope" in err.lower() else "not_found"),
            message=err, query=query,
        )
    n_books = raw.get("books_total", -1) if isinstance(raw, dict) else -1
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
            view_rows.append({
                "rank": i,
                "ngram": r.get("ngram") or r.get("words") or r.get("phrase") or "—",
                "count": r.get("count") or r.get("freq") or "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "ngram", "count"],
            headline=headline,
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.top_ngrams").warning(
            "top_ngrams_by_author view emission failed: %s", e,
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
        ttr = raw.get("ttr") or raw.get("aggregate_ttr") or raw.get("value")
        per_book = raw.get("per_book") or raw.get("books") or []
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {scope.get('book') or scope.get('pg_id')}"
                     if scope.get("book") or scope.get("pg_id")
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
            view_rows.append({
                "rank": i,
                "book": b.get("title") or b.get("pg_id") or "—",
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
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.authors.top_ngrams").warning(
            "lexical_diversity view emission failed: %s", e,
        )
