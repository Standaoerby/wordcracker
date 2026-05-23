"""v2 hybrid_search — RRF merge of lexical (BM25/FTS5) and semantic (ChromaDB).

Reciprocal Rank Fusion is parameter-light, robust to score-scale mismatches,
and beats either single retriever on most evaluation suites. Formula:

    score(d) = sum over retrievers r of  1 / (k_rrf + rank_r(d))

where k_rrf=60 (the standard constant from the original RRF paper). Docs
appearing in both pools naturally bubble up.

For a query like «упоминания тумана у викторианцев», the semantic side
catches paraphrases ("dense haze", "fog-laden") while lexical pins exact
mentions of "fog". Together they cover both.
"""
from __future__ import annotations

import logging
from typing import Any

from scripts.v2.tool_registry import dispatch, tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.tools._normalize import match_id, search_snippet

log = logging.getLogger("wordcracker.v2.tools.search.hybrid")

K_RRF = 60


def _rank_map(items: list[dict[str, Any]], key: str = "pg_id") -> dict[str, int]:
    """Return {pg_id: rank (1-indexed)}. Dupes keep the best rank seen first."""
    out: dict[str, int] = {}
    for i, it in enumerate(items, 1):
        # `key` is the caller's preferred name (default "pg_id");
        # match_id falls back through the historical `id` alias.
        pg = it.get(key) if isinstance(it, dict) else None
        if not pg:
            pg = match_id(it)
        if not pg or pg in out:
            continue
        out[pg] = i
    return out


@tool(
    name="hybrid_search",
    category="search",
    description=(
        "Гибридный поиск: lexical (FTS5 BM25) + semantic (ChromaDB) "
        "+ Reciprocal Rank Fusion. Используй когда хочешь и точные упоминания "
        "слова, и параграфы по смыслу. Возвращает merged top-k. "
        "Optional rerank_with='bge_reranker' для cross-encoder reorder."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query":         {"type": "string"},
            "k":             {"type": "integer", "description": "final top-k (default 12)"},
            "per_retriever": {"type": "integer", "description": "k for each retriever before merge (default 50)"},
            "author_filter": {"type": "string", "description": "regex passed to semantic_search"},
            "rerank_with":   {"type": "string", "description": "plugin name from scoring.REGISTRY, e.g. 'bge_reranker' (slow but accurate)"},
            "lang":          {"type": "string", "description": "post-filter matches to books in this ISO-639-1 lang (e.g. 'en'). Stan Round 12 B4."},
        },
        "required": ["query"],
    },
    requires=["word"],
    cost="medium",
    cacheable=True,
    # Sprint 22+ Stan B4: `lang` filter changes the output set. Bump.
    # E22 — query-side lang normalization fix (twin of E21 book-side).
    # «english» → «en» via regex, not «eng» via [:3] truncation.
    # Invalidates entries written between E20 and E22 deploys.
    wrapper_version="v6-e22-lang-query-fix",
)
def hybrid_search(query: str, k: int = 12, per_retriever: int = 50,
                  author_filter: str | None = None,
                  rerank_with: str | None = None,
                  lang: str | None = None) -> ToolResult:
    warnings: list[ToolWarning] = []

    # Lexical via v2 lexical_search. T5 (2026-05-23): unified `dispatch`
    # chokepoint — same call shape as the semantic_search hop below, no
    # second alias for the legacy path.
    lex = dispatch("lexical_search", {"query": query, "k": per_retriever})
    if not lex.ok:
        warnings.append(ToolWarning(
            code="lexical_failed",
            message=f"lexical_search: {lex.error.message if lex.error else 'unknown'}",
        ))
        lex_matches: list[dict] = []
    else:
        lex_matches = lex.data.get("matches", []) if isinstance(lex.data, dict) else []
        # Propagate any warnings (e.g. fts_unavailable).
        for w in lex.warnings:
            warnings.append(w)

    # Semantic via legacy semantic_search (still v1-routed for now).
    # T5: `dispatch` falls through to scripts.rag_query.TOOL_DISPATCH when
    # the name is not in v2 REGISTRY, with the same timeout/budget guard.
    sem_args = {"query": query, "k": per_retriever}
    if author_filter:
        sem_args["author_filter"] = author_filter
    sem = dispatch("semantic_search", sem_args)
    if not sem.ok:
        warnings.append(ToolWarning(
            code="semantic_failed",
            message=f"semantic_search: {sem.error.message if sem.error else 'unknown'}",
        ))
        sem_matches: list[dict] = []
    else:
        sem_matches = sem.data.get("results", []) if isinstance(sem.data, dict) else []
        # Semantic results use `metadata.pg_id` shape; normalize.
        for m in sem_matches:
            md = m.get("metadata") or {}
            if "pg_id" not in m and "pg_id" in md:
                m["pg_id"] = md["pg_id"]

    if not lex_matches and not sem_matches:
        return ToolResult(
            ok=False, tool="hybrid_search",
            query={"query": query, "k": k, "per_retriever": per_retriever},
            data={"matches": [], "lexical_n": 0, "semantic_n": 0},
            warnings=warnings or [ToolWarning("empty", "both retrievers returned nothing")],
            coverage=Coverage(books_matched=0, books_total=-1),
            error=None,
        )

    lex_ranks = _rank_map(lex_matches)
    sem_ranks = _rank_map(sem_matches)
    all_ids = set(lex_ranks) | set(sem_ranks)

    fused: list[tuple[str, float, dict, dict]] = []
    by_id_lex = {match_id(m): m for m in lex_matches if m}
    by_id_sem = {match_id(m): m for m in sem_matches if m}
    for pg in all_ids:
        score = (
            (1 / (K_RRF + lex_ranks[pg]) if pg in lex_ranks else 0.0)
            + (1 / (K_RRF + sem_ranks[pg]) if pg in sem_ranks else 0.0)
        )
        fused.append((pg, score, by_id_lex.get(pg, {}), by_id_sem.get(pg, {})))

    fused.sort(key=lambda t: t[1], reverse=True)
    # When reranking, keep a wider pool (2k items) so the reranker has
    # room to reorder. Otherwise the cross-encoder is just confirming RRF.
    pool_size = min(len(fused), max(k * 2, 30)) if rerank_with else k
    top = fused[:pool_size]

    # Sprint 21+ B100 v2 — lazy v1-metadata lookup helper for ultimate
    # title fallback. Used when neither lex nor sem carries title (e.g.
    # cached lexical results from before alpha3, or an exotic edge case).
    # Import here so the test path that mocks dispatch doesn't pay the
    # v1 metadata-load cost on every hybrid call.
    _meta_cache: dict = {}
    def _meta_lookup(pid: str) -> dict:
        if not pid:
            return {}
        if pid in _meta_cache:
            return _meta_cache[pid]
        try:
            from scripts.v2.tools.search.lexical import _title_lookup
            lookup = _title_lookup()
            entry = lookup.get(str(pid), {}) or {}
        except Exception:
            entry = {}
        _meta_cache[pid] = entry
        return entry

    out_matches = []
    for pg, score, lm, sm in top:
        # Sprint 21+ B100 hotfix v2 — title/author come from THREE places,
        # in priority order:
        #   1) semantic_search v1 puts title/author at TOP LEVEL of each
        #      result (scripts/rag_tools.py lines 419-426). NOT nested
        #      under metadata. My earlier alpha3 fix looked in the wrong
        #      key, which is why prod still showed PG ids only.
        #   2) lexical_search (post-alpha3 patch) puts title/author at
        #      top level after v1 metadata lookup.
        #   3) Ultimate fallback: do the v1 metadata lookup HERE at merge
        #      time. Catches old cached results that pre-date the
        #      lexical_search title-attach patch.
        title = (
            sm.get("title")
            or (sm.get("metadata") or {}).get("title")    # defensive
            or lm.get("title")
            or _meta_lookup(pg).get("title")
        )
        author = (
            sm.get("author")
            or (sm.get("metadata") or {}).get("author")
            or lm.get("author")
            or _meta_lookup(pg).get("author")
        )
        out_matches.append({
            "pg_id": pg,
            "rrf_score": round(score, 6),
            "lexical_rank": lex_ranks.get(pg),
            "semantic_rank": sem_ranks.get(pg),
            "snippet": search_snippet(lm, sm),
            "title": title,
            "author": author,
        })

    # Sprint 22+ Stan B4 — lang post-filter. Round 12 Q5: «английская
    # классика» surfaced Finnish/Hungarian/Italian books because no
    # filter applied anywhere. Now: when `lang` is set, drop matches
    # whose book metadata.language doesn't match. Falls back to lookup
    # via _meta_lookup helper (same v1 metadata cache used for titles).
    lang_dropped = 0
    if lang:
        # E22 (2026-05-22) — twin of E21 _title_lookup fix. Old computation
        #   want = lang.lower().strip().split("-")[0][:3]
        # also truncated to 3 chars: for entity-extracted lang_hint
        # "english" (from «английская литература»), want became "eng"
        # — but `_title_lookup` (post-E21) stores book_lang as "en".
        # «"eng" in "en"» is False → drop all matches → empty answer.
        # Stan prod 2026-05-22: «примеры использования слова "ajar" в
        # английской литературе» still failed after E21 because E21
        # only fixed the BOOK side; the QUERY side still ate the 3-char
        # truncation. Use the same robust extraction: try a 2-3 letter
        # ISO-639 token first; fall back to a 2-char slice for full
        # names («english» → «en», «russian» → «ru»).
        import re as _re_q
        _raw_lang = str(lang).lower().strip()
        _m_iso = _re_q.match(r"^([a-z]{2,3})\b", _raw_lang)
        if _m_iso:
            want = _m_iso.group(1)
        else:
            want = _raw_lang[:2]
        filtered: list[dict] = []
        for m in out_matches:
            meta = _meta_lookup(m["pg_id"])
            book_lang = (meta.get("language") or "").lower().strip()
            # E20 (2026-05-22) — PG metadata stores `language` as a
            # stringified Python list, e.g. "['en']" / "['en', 'fr']" /
            # "['fr']", NOT plain "en". Old equality check
            # (`book_lang == want`) was therefore always False — every
            # match got dropped, giving the false «не встретилось в
            # корпус» for ALL English-lit queries that supplied
            # lang_hint. Substring containment handles both raw "en"
            # and stringified-list shapes; bracketed multi-lang lists
            # match when the wanted code is one of the entries.
            # Books with no language metadata are kept (better than
            # over-aggressive filter).
            if not book_lang or want in book_lang:
                filtered.append(m)
            else:
                lang_dropped += 1
        out_matches = filtered
    if lang_dropped:
        warnings.append(ToolWarning(
            code="lang_filtered",
            message=f"dropped {lang_dropped} matches not in lang={lang!r}",
        ))

    # Optional cross-encoder rerank — bi-encoder pool → cross-encoder top-k
    rerank_applied: str | None = None
    if rerank_with and out_matches:
        try:
            from scripts.v2.scoring import REGISTRY as _SCORING, ScoringQuery
            plugin = _SCORING.get(rerank_with)
            if plugin is None:
                warnings.append(ToolWarning(
                    code="rerank_unknown",
                    message=f"plugin {rerank_with!r} not in REGISTRY",
                ))
            elif "retrieval_rerank" not in getattr(plugin, "kinds", ()):
                warnings.append(ToolWarning(
                    code="rerank_kind_mismatch",
                    message=f"plugin {rerank_with!r} doesn't support retrieval_rerank",
                ))
            else:
                rr = plugin.compute(ScoringQuery(
                    kind="retrieval_rerank",
                    target=query,
                    candidates=out_matches,
                ))
                if rr:
                    rerank_scores = {item.id: item.score for item in rr}
                    out_matches = [m for m in out_matches if m["pg_id"] in rerank_scores]
                    out_matches.sort(
                        key=lambda m: rerank_scores.get(m["pg_id"], -1e9),
                        reverse=True,
                    )
                    for m in out_matches:
                        m["rerank_score"] = round(rerank_scores.get(m["pg_id"], 0.0), 6)
                    rerank_applied = rerank_with
        except Exception as e:
            warnings.append(ToolWarning(
                code="rerank_failed", message=f"{rerank_with}: {e}",
            ))

    # After optional rerank, trim down to user-requested k.
    out_matches = out_matches[:k]

    data = {
        "query": query,
        "matches": out_matches,
        "lexical_n": len(lex_matches),
        "semantic_n": len(sem_matches),
        "k_rrf": K_RRF,
    }
    if rerank_applied:
        data["reranked_by"] = rerank_applied

    result = ToolResult.success(
        tool="hybrid_search",
        data=data,
        coverage=Coverage(books_matched=len(out_matches), books_total=-1),
        warnings=warnings,
        query={"query": query, "k": k, "per_retriever": per_retriever,
               "author_filter": author_filter, "rerank_with": rerank_with},
    )

    # E16 (2026-05-22) — emit WORD_CONTEXTS view so the renderer can
    # surface examples. Without this hybrid_search has snippets in
    # `data["matches"]` but no typed view, so select_primary_view ignores
    # it. For «примеры использования слова X» the plan dispatches
    # hybrid_search + enrich_word; previously only enrich_word emitted
    # a view (ETYMOLOGY_BUNDLE) which the renderer picked — examples
    # were invisible.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        contexts = []
        for m in out_matches[:10]:
            if not isinstance(m, dict):
                continue
            snip = m.get("snippet")
            if not snip or not str(snip).strip():
                continue
            contexts.append({
                "snippet": str(snip).strip(),
                "pg_id": match_id(m),
                "title": m.get("title") or "",
                "author": m.get("author") or "",
            })
        # E42 (2026-05-22) — scope_label «весь корпус (FTS5+semantic RRF)»
        # leaked internal index implementation в headline rendered to user.
        # Stan flagged it as «неправильный» in persona prod. Replace with
        # plain user-facing phrase.
        view = vb.build_word_contexts(
            word=query,
            contexts=contexts,
            scope_label="во всём корпусе",
            language="ru",
        )
        validity = DataValidity.OK if contexts else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.search.hybrid").exception(
            "hybrid_search view emission failed"
        )
    return result
