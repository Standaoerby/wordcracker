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

import math
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


class BGEReranker:
    """Cross-encoder reranker — BAAI/bge-reranker-base.

    For `retrieval_rerank` kind: given a query text and a list of
    candidate passages, score each (query, passage) pair with a
    cross-encoder for relevance. Cross-encoders are slower than
    bi-encoders (must run for every pair) but much more accurate —
    typical recipe: bi-encoder retrieves top-30, cross-encoder reranks
    to top-10.

    Lazy-loaded: model (~440 MB) only loads on first compute() call,
    not at import time. Caches model on instance after first load.

    Candidate shapes accepted (auto-normalized):
      - tuple/list: (id, text)
      - dict: {"id": ..., "text": ...} OR {"pg_id": ..., "snippet": ...}
        OR {"pg_id": ..., "text": ...}
    """
    name = "bge_reranker"
    kinds = ("retrieval_rerank",)
    cost = "heavy"
    model_name = "BAAI/bge-reranker-base"

    def __init__(self) -> None:
        self._model: Any = None  # CrossEncoder once loaded

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise RuntimeError(
                f"BGEReranker needs `sentence-transformers`: {e}") from e
        # CrossEncoder auto-downloads on first run, then caches in HF cache.
        self._model = CrossEncoder(self.model_name)
        return self._model

    @staticmethod
    def _normalize(cand: Any) -> tuple[Any, str] | None:
        """Return (id, text) or None if cand has no usable text."""
        if isinstance(cand, (tuple, list)) and len(cand) >= 2:
            return cand[0], str(cand[1])
        if isinstance(cand, dict):
            id_ = cand.get("id") or cand.get("pg_id")
            text = cand.get("text") or cand.get("snippet") or cand.get("body")
            if id_ is not None and text:
                return id_, str(text)
        return None

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "retrieval_rerank":
            return []
        target = query.target
        if not isinstance(target, str) or not target.strip():
            return []
        normalized: list[tuple[Any, str]] = []
        for cand in query.candidates:
            n = self._normalize(cand)
            if n is not None:
                normalized.append(n)
        if not normalized:
            return []
        try:
            model = self._load()
        except Exception:
            return []
        pairs = [[target, text] for _id, text in normalized]
        try:
            scores = model.predict(pairs)
        except Exception:
            return []
        out: list[ScoredItem] = []
        for (id_, _text), score in zip(normalized, scores):
            out.append(ScoredItem(
                id=id_, score=float(score), direction="higher_better",
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"BGE cross-encoder relevance {scored.score:.3f} "
                f"(выше = релевантнее запросу; full cross-attention "
                f"между query и кандидатом)")


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


# ---------- Word-pair scoring (collocations) ----------
#
# All three plugins below share the same input contract — the v2
# word_collocates wrapper does one I/O pass over counts files and
# packages everything the math needs into a single ScoringQuery:
#
#   target        = target word (e.g. "fog")
#   candidates    = [{"word": w, "c_pair": int, "c_neighbor": int}, ...]
#   options       = {"c_target": int, "N": int (total tokens), "window": int}
#
# Math notation:
#   N       = total tokens in scope
#   W       = 2 * window (size of each side-pooled window around target)
#   c(t)    = target word frequency in scope
#   c(w)    = neighbor word frequency in scope
#   c(t,w)  = observed co-occurrence count (w appears within ±window of t)
#   E(t,w)  = c(t) * W * c(w) / N    — expected co-occurrences if independent
#
# All metrics filter out pairs with c(t,w) < min_cooccurrence at the
# wrapper level (Plugin compute() doesn't filter — that's caller policy).


def _wordpair_unpack(query: ScoringQuery) -> tuple[float, int, int, int] | None:
    """Pull N, c_target, window, count of candidates from query.options.
    Return None when prerequisites missing — plugin should then return []."""
    opts = query.options or {}
    N = opts.get("N")
    c_target = opts.get("c_target")
    window = opts.get("window", 4)
    if not isinstance(N, (int, float)) or N <= 0:
        return None
    if not isinstance(c_target, (int, float)) or c_target <= 0:
        return None
    if not isinstance(window, (int, float)) or window <= 0:
        return None
    W = 2 * int(window)
    return float(N), int(c_target), int(W), len(query.candidates)


def _candidate_counts(cand: Any) -> tuple[str, int, int] | None:
    """Normalize a candidate row: returns (word, c_pair, c_neighbor) or None."""
    if not isinstance(cand, dict):
        return None
    word = cand.get("word") or cand.get("neighbor")
    c_pair = cand.get("c_pair") or cand.get("count")
    c_neighbor = cand.get("c_neighbor") or cand.get("scope_count")
    if not isinstance(word, str) or not word:
        return None
    if not isinstance(c_pair, (int, float)) or c_pair <= 0:
        return None
    if not isinstance(c_neighbor, (int, float)) or c_neighbor <= 0:
        return None
    return word, int(c_pair), int(c_neighbor)


class PMI:
    """Pointwise Mutual Information (log2). Standard collocation strength
    metric — high PMI = the pair is much more common than chance.

    Caveat: PMI overestimates rare pairs (a single co-occurrence of two
    1-count words gives huge PMI). Apply min_cooccurrence at the caller
    to suppress this; NPMI also normalizes it away.
    """
    name = "pmi"
    kinds = ("word_pair",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "word_pair":
            return []
        unpacked = _wordpair_unpack(query)
        if unpacked is None:
            return []
        N, c_target, W, _ = unpacked
        out: list[ScoredItem] = []
        for cand in query.candidates:
            nc = _candidate_counts(cand)
            if nc is None:
                continue
            word, c_pair, c_neighbor = nc
            expected = c_target * W * c_neighbor / N
            if expected <= 0:
                continue
            pmi = math.log2(c_pair / expected)
            out.append(ScoredItem(
                id=word, score=pmi, direction="higher_better",
                extra={"c_pair": c_pair, "c_neighbor": c_neighbor,
                       "expected": round(expected, 3)},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"PMI {scored.score:.2f} bit "
                f"(c_pair={scored.extra.get('c_pair')}, "
                f"E={scored.extra.get('expected')})")


class NPMI:
    """Normalized PMI ∈ [-1, 1]. NPMI = PMI / -log2(p(t,w)).

    -1 = never co-occur. 0 = independence. 1 = always co-occur.
    Bouma 2009. Much more stable for ranking than raw PMI.
    """
    name = "npmi"
    kinds = ("word_pair",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "word_pair":
            return []
        unpacked = _wordpair_unpack(query)
        if unpacked is None:
            return []
        N, c_target, W, _ = unpacked
        # Total #pairs in scope = N * W (each token has W neighbors).
        total_pairs = N * W
        out: list[ScoredItem] = []
        for cand in query.candidates:
            nc = _candidate_counts(cand)
            if nc is None:
                continue
            word, c_pair, c_neighbor = nc
            expected = c_target * W * c_neighbor / N
            if expected <= 0 or c_pair <= 0:
                continue
            p_pair = c_pair / total_pairs
            if p_pair <= 0 or p_pair >= 1:
                continue
            pmi = math.log2(c_pair / expected)
            denom = -math.log2(p_pair)
            if denom == 0:
                continue
            npmi = pmi / denom
            # Clamp into [-1, 1] for cosmetic robustness against
            # numerical edge cases when c_pair=1 and expected~p_pair.
            npmi = max(-1.0, min(1.0, npmi))
            out.append(ScoredItem(
                id=word, score=npmi, direction="higher_better",
                extra={"c_pair": c_pair, "c_neighbor": c_neighbor,
                       "pmi_bits": round(pmi, 3)},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"NPMI {scored.score:.3f} "
                f"(c_pair={scored.extra.get('c_pair')}, "
                f"PMI={scored.extra.get('pmi_bits')} bit)")


class Dice:
    """Dice coefficient = 2*c(t,w) / (c(t)*W + c(w)).

    Geometric similarity-style metric. Less sensitive than PMI to rare
    words: a 1/1/1 pair gets a low Dice score, unlike PMI. Good when
    PMI ranking is noisy.
    """
    name = "dice"
    kinds = ("word_pair",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "word_pair":
            return []
        unpacked = _wordpair_unpack(query)
        if unpacked is None:
            return []
        _N, c_target, W, _ = unpacked
        out: list[ScoredItem] = []
        for cand in query.candidates:
            nc = _candidate_counts(cand)
            if nc is None:
                continue
            word, c_pair, c_neighbor = nc
            denom = c_target * W + c_neighbor
            if denom <= 0:
                continue
            dice = 2 * c_pair / denom
            out.append(ScoredItem(
                id=word, score=dice, direction="higher_better",
                extra={"c_pair": c_pair, "c_neighbor": c_neighbor},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"Dice {scored.score:.4f} "
                f"(c_pair={scored.extra.get('c_pair')}, "
                f"c_neighbor={scored.extra.get('c_neighbor')})")


class LogLikelihood:
    """Dunning's log-likelihood G² on the 2×2 co-occurrence table.

    Measures statistical SIGNIFICANCE of a (target, neighbor) collocation
    — how surprised we are by the observed co-occurrence under the
    independence null. Unlike PMI/NPMI (association strength), G² scales
    with evidence: a pair seen 40× outranks a 3× pair even when the 3×
    pair has higher PMI. Rank by G² for "is this real", read PMI/logDice
    for "how tight".

    2×2 contingency (window-slot model, W = 2*window slots per token):
        Z   = N * W                      total window slots
        R1  = c_target * W               slots inside a target window
        C1  = c_neighbor * W             slots occupied by the neighbor
        O11 = c_pair                     neighbor inside target window
        O12 = R1 - O11   O21 = C1 - O11  O22 = Z - R1 - C1 + O11
        E11 = R1*C1/Z   (etc.)
        G²  = 2·Σ O·ln(O/E) over the 4 cells, skipping any cell with O≤0
              or E≤0 (the x·ln(x)→0 limit).

    NOTE: this is the FULL 4-cell table — distinct from the 2-cell
    keyness.log_likelihood_g2 (target-vs-reference word frequency). Same
    Dunning family, different contingency; not reusable across the two.
    """
    name = "loglikelihood"
    kinds = ("word_pair",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "word_pair":
            return []
        unpacked = _wordpair_unpack(query)
        if unpacked is None:
            return []
        N, c_target, W, _ = unpacked
        Z = N * W
        R1 = c_target * W
        out: list[ScoredItem] = []
        for cand in query.candidates:
            nc = _candidate_counts(cand)
            if nc is None:
                continue
            word, c_pair, c_neighbor = nc
            C1 = c_neighbor * W
            O11 = c_pair
            O12 = R1 - O11
            O21 = C1 - O11
            O22 = Z - R1 - C1 + O11
            E11 = R1 * C1 / Z if Z else 0.0
            E12 = R1 - E11
            E21 = C1 - E11
            E22 = Z - E11 - E12 - E21
            g2 = 0.0
            for o, e in ((O11, E11), (O12, E12), (O21, E21), (O22, E22)):
                if o > 0 and e > 0:
                    g2 += o * math.log(o / e)
            g2 *= 2.0
            out.append(ScoredItem(
                id=word, score=g2, direction="higher_better",
                extra={"c_pair": c_pair, "c_neighbor": c_neighbor,
                       "expected": round(E11, 3), "observed": c_pair,
                       "over": c_pair > E11},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        d = "над" if scored.extra.get("over") else "под"
        return (f"G² {scored.score:.2f} ({d}-представлено; "
                f"c_pair={scored.extra.get('c_pair')}, "
                f"E={scored.extra.get('expected')})")


class LogDice:
    """logDice (Rychlý 2008) — bounded, corpus-size-independent collocation
    strength. logDice = 14 + log2(2·c_pair / (c_target·W + c_neighbor)).

    Independent of N (unlike PMI), so values are comparable across scopes.
    Theoretical max 14 (every target window holds the neighbor and vice
    versa); typical strong collocations land 7–10.
    """
    name = "logdice"
    kinds = ("word_pair",)
    cost = "cheap"

    def compute(self, query: ScoringQuery) -> list[ScoredItem]:
        if query.kind != "word_pair":
            return []
        unpacked = _wordpair_unpack(query)
        if unpacked is None:
            return []
        _N, c_target, W, _ = unpacked
        out: list[ScoredItem] = []
        for cand in query.candidates:
            nc = _candidate_counts(cand)
            if nc is None:
                continue
            word, c_pair, c_neighbor = nc
            denom = c_target * W + c_neighbor
            if denom <= 0 or c_pair <= 0:
                continue
            logdice = 14.0 + math.log2(2.0 * c_pair / denom)
            out.append(ScoredItem(
                id=word, score=logdice, direction="higher_better",
                extra={"c_pair": c_pair, "c_neighbor": c_neighbor},
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    def explain(self, scored: ScoredItem) -> str:
        return (f"logDice {scored.score:.3f} "
                f"(c_pair={scored.extra.get('c_pair')}, "
                f"c_neighbor={scored.extra.get('c_neighbor')})")


# ---------- Registry ----------

REGISTRY: dict[str, ScoringPlugin] = {
    "burrows_delta":  BurrowsDelta(),
    "jaccard_top200": JaccardTop200(),
    "ensemble":       Ensemble(),
    "bge_reranker":   BGEReranker(),  # heavy, lazy-loads on first .compute()
    "pmi":            PMI(),
    "npmi":           NPMI(),
    "dice":           Dice(),
    "loglikelihood":  LogLikelihood(),
    "logdice":        LogDice(),
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
