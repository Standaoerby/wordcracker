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

from scripts.v2.legacy_dispatch import dispatch_any
from scripts.v2.tool_registry import dispatch as v2_dispatch, tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.search.hybrid")

K_RRF = 60


def _rank_map(items: list[dict[str, Any]], key: str = "pg_id") -> dict[str, int]:
    """Return {pg_id: rank (1-indexed)}. Dupes keep the best rank seen first."""
    out: dict[str, int] = {}
    for i, it in enumerate(items, 1):
        pg = it.get(key) or it.get("id")
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
            "per_retriever": {"type": "integer", "description": "k for each retriever before merge (default 30)"},
            "author_filter": {"type": "string", "description": "regex passed to semantic_search"},
            "rerank_with":   {"type": "string", "description": "plugin name from scoring.REGISTRY, e.g. 'bge_reranker' (slow but accurate)"},
        },
        "required": ["query"],
    },
    requires=["word"],
    cost="medium",
    cacheable=True,
)
def hybrid_search(query: str, k: int = 12, per_retriever: int = 30,
                  author_filter: str | None = None,
                  rerank_with: str | None = None) -> ToolResult:
    warnings: list[ToolWarning] = []

    # Lexical via v2 lexical_search
    lex = v2_dispatch("lexical_search", {"query": query, "k": per_retriever})
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
    sem_args = {"query": query, "k": per_retriever}
    if author_filter:
        sem_args["author_filter"] = author_filter
    sem = dispatch_any("semantic_search", sem_args)
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
    by_id_lex = {m.get("pg_id") or m.get("id"): m for m in lex_matches if m}
    by_id_sem = {m.get("pg_id") or m.get("id"): m for m in sem_matches if m}
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

    out_matches = []
    for pg, score, lm, sm in top:
        out_matches.append({
            "pg_id": pg,
            "rrf_score": round(score, 6),
            "lexical_rank": lex_ranks.get(pg),
            "semantic_rank": sem_ranks.get(pg),
            "snippet": lm.get("snippet") or sm.get("text") or sm.get("snippet"),
            "title": sm.get("metadata", {}).get("title") if isinstance(sm.get("metadata"), dict) else None,
            "author": sm.get("metadata", {}).get("author") if isinstance(sm.get("metadata"), dict) else None,
        })

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

    return ToolResult.success(
        tool="hybrid_search",
        data=data,
        coverage=Coverage(books_matched=len(out_matches), books_total=-1),
        warnings=warnings,
        query={"query": query, "k": k, "per_retriever": per_retriever,
               "author_filter": author_filter, "rerank_with": rerank_with},
    )
