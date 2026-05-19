"""ScoringPlugin registry — pluggable similarity / relevance metrics.

Sprint 16 Phase B3 (v3.0): one Protocol unifies author-similarity AND
retrieval-reranking. Same pattern for both — future BGE reranker (B4)
is just another plugin, not a separate stack.

See docs/v2/PLUGIN.md §2 for usage / extension contract.

Usage
-----
```python
from scripts.v2.scoring import REGISTRY, ScoringQuery

# Author similarity
plugin = REGISTRY["burrows_delta"]
result = plugin.compute(ScoringQuery(
    kind="author_similarity",
    target="^Doyle,",
    candidates=["^Wells,", "^Stevenson,", "^Wilde,"],
))
# result = [ScoredItem(id="^Wells,", score=0.42, ...), ...]

# Retrieval rerank (Phase B4)
plugin = REGISTRY["bge_reranker"]
result = plugin.compute(ScoringQuery(
    kind="retrieval_rerank",
    target="ghosts in victorian fiction",
    candidates=[chunk1, chunk2, ...],  # tuples (id, text)
))
```

Adding a new plugin
-------------------
1. New module in scripts/v2/scoring/ implementing ScoringPlugin
2. Register in REGISTRY dict below
3. test_scoring_plugins.py picks it up automatically via REGISTRY
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


ScoringKind = Literal["author_similarity", "retrieval_rerank", "word_pair"]


@dataclass
class ScoringQuery:
    """Input for a ScoringPlugin.compute() call.

    `target` is the reference (author regex / query text / target word).
    `candidates` is the list to score against the target. Shape depends
    on `kind`:
      - author_similarity: candidates are author regexes (`^Surname,`)
      - retrieval_rerank: candidates are list[tuple[id, text]]
      - word_pair: candidates are list of (target_word, neighbor_word)
        pairs — for collocate scoring (Phase C will use this)
    """
    kind: ScoringKind
    target: Any
    candidates: list[Any] = field(default_factory=list)
    options: dict = field(default_factory=dict)


@dataclass
class ScoredItem:
    """One result from a ScoringPlugin.compute() call.

    `score` semantics depend on plugin:
      - Burrows Delta: lower = more similar (it's a distance)
      - BGE Reranker: higher = more relevant (it's a probability)
      - PMI: higher = stronger association

    Use the `direction` attribute to know which interpretation applies
    so caller can sort consistently. Plugin sets it; caller reads it.
    """
    id: Any
    score: float
    direction: Literal["lower_better", "higher_better"] = "higher_better"
    extra: dict = field(default_factory=dict)


@runtime_checkable
class ScoringPlugin(Protocol):
    """Common interface for all scoring metrics.

    Implementations register themselves in REGISTRY below. The Protocol
    is `runtime_checkable` so `test_scoring_plugins.py` can verify
    every registered plugin matches the shape.
    """
    name: str
    kinds: tuple[ScoringKind, ...]
    cost: Literal["cheap", "medium", "heavy"]

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        ...

    def explain(self, scored: ScoredItem) -> str:
        ...


# ---------- Built-in plugins ----------

class BurrowsDelta:
    """Burrows Delta — distance between z-scored function-word vectors.

    The metric the v1 `author_influences` tool was always using. This
    plugin is a thin pass-through to v1, kept as default for backward
    compatibility (and as one ensemble member).
    """
    name = "burrows_delta"
    kinds = ("author_similarity",)
    cost = "medium"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "author_similarity":
            return []
        try:
            from scripts.rag_tools import author_influences as _v1
        except ImportError:
            return []
        top = query.options.get("top", 20)
        raw = _v1(author_regex=query.target, top=top)
        if not isinstance(raw, dict) or raw.get("error"):
            return []
        rows = (raw.get("closest") or raw.get("neighbours")
                or raw.get("top") or raw.get("authors") or [])
        out: list[ScoredItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            author = r.get("author") or r.get("name")
            d = r.get("delta") or r.get("distance") or r.get("score")
            if author is None or not isinstance(d, (int, float)):
                continue
            out.append(ScoredItem(
                id=author, score=float(d), direction="lower_better",
                extra={"books": r.get("books"),
                       "n_books": r.get("n_books")},
            ))
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"Burrows Delta {scored.score:.3f} (нижний = ближе по "
                f"стилю; функциональные слова в z-score нормировке)")


class JaccardTop200:
    """Simple ML-free baseline — Jaccard overlap of top-200 author
    signature words. Useful as ensemble member and as sanity check for
    Burrows."""
    name = "jaccard_top200"
    kinds = ("author_similarity",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "author_similarity":
            return []
        try:
            from scripts.rag_tools import affinity_by_author as _aff
        except ImportError:
            return []
        a_words = _top_words_set(_aff, query.target, top=200)
        if not a_words:
            return []
        out: list[ScoredItem] = []
        for cand in query.candidates:
            b_words = _top_words_set(_aff, cand, top=200)
            if not b_words:
                continue
            intersection = a_words & b_words
            union = a_words | b_words
            jaccard = len(intersection) / len(union) if union else 0.0
            out.append(ScoredItem(
                id=cand, score=jaccard, direction="higher_better",
                extra={"shared_words": len(intersection)},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"Jaccard top-200 = {scored.score:.3f} "
                f"({scored.extra.get('shared_words', 0)} общих "
                f"signature слов из 400 уникальных)")


def _top_words_set(affinity_fn, author_regex: str, top: int) -> set[str]:
    """Helper for Jaccard — pull top-N signature words as a set."""
    try:
        raw = affinity_fn(author_regex=author_regex, top=top,
                          min_corpus_count=500)
    except Exception:
        return set()
    if not isinstance(raw, dict) or raw.get("error"):
        return set()
    rows = raw.get("top_words") or raw.get("top") or []
    return {(r.get("word") or "").lower() for r in rows
            if isinstance(r, dict) and r.get("word")}


class Ensemble:
    """Vote across multiple plugins — used as the v3.0 default for
    `author_influences`. Member plugins compute their own scoring on
    the same candidate set; final rank is by Borda count (sum of
    per-metric ranks).

    Heavy plugins (BGE reranker) are excluded — ensemble is meant to
    be cheap-medium combos for everyday queries.
    """
    name = "ensemble"
    kinds = ("author_similarity",)
    cost = "medium"

    def __init__(self, members: list[str] | None = None):
        self.members = members or ["burrows_delta", "jaccard_top200"]

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        per_metric: list[list[ScoredItem]] = []
        for name in self.members:
            plugin = REGISTRY.get(name)
            if plugin is None:
                continue
            results = plugin.compute(query)
            per_metric.append(results)
        if not per_metric:
            return []
        # Borda count: for each id, sum the rank position across metrics.
        # Lower total = consistently higher across metrics.
        borda: dict[Any, list[int]] = {}
        for results in per_metric:
            for rank, item in enumerate(results):
                borda.setdefault(item.id, []).append(rank)
        out: list[ScoredItem] = []
        for id_, ranks in borda.items():
            # Average rank — punishes items present in fewer metrics
            avg_rank = sum(ranks) / len(ranks) if ranks else 999
            penalty = (len(per_metric) - len(ranks)) * 5
            score = avg_rank + penalty
            out.append(ScoredItem(
                id=id_, score=score, direction="lower_better",
                extra={"metrics_voted": len(ranks),
                       "ranks_per_metric": ranks},
            ))
        out.sort(key=lambda s: s.score)
        return out

    def explain(self, scored: ScoredItem) -> str:
        m = scored.extra.get("metrics_voted", 0)
        return (f"Ensemble avg-rank {scored.score:.1f} "
                f"(voted by {m}/{len(self.members)} metrics)")


# ---------- Registry ----------

REGISTRY: dict[str, ScoringPlugin] = {
    "burrows_delta":  BurrowsDelta(),
    "jaccard_top200": JaccardTop200(),
    "ensemble":       Ensemble(),
    # B4 (next commit): BGEReranker — lazy-loaded, kind="retrieval_rerank"
}


def get(name: str) -> ScoringPlugin | None:
    """Convenience accessor — returns None when plugin not registered."""
    return REGISTRY.get(name)


def list_plugins() -> list[dict]:
    """Introspection — for status dashboard / docs / debugging."""
    return [
        {"name": p.name, "kinds": list(p.kinds), "cost": p.cost}
        for p in REGISTRY.values()
    ]
