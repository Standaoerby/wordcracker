# wordcracker v2/v3 — extension points

How to add things without breaking the deterministic pipeline. Sprint 16
(v3.0) introduces three plugin surfaces — author aliases, scoring metrics,
and intent rules — each with its own contract and regression-test scaffold.

## Pipeline at a glance

```
user query
  → intent.classify()         — 80+ regex rules + LLM fallback (rule)
  → entities.extract()        — author / book / word / period (alias-driven)
  → history.merge_with_history — multi-turn entity backfill
  → plan.build()              — 33 plan templates (intent + entities → steps)
  → router.execute()          — tool dispatch (no LLM in loop)
  → renderer (1 LLM call)     — strict facts-only render
  → critic.review()           — fact-check pass, MAX_FLAGS guard
```

Three extension points let new functionality slot in **without changing
the core pipeline** — only changing data that the pipeline reads.

---

## 1. Adding an author alias

### Two layers

```
scripts/v2/data/aliases_generated.json   ← auto-built from corpus metadata
                                            (build_author_aliases.py;
                                             rebuilt on each corpus refresh)
scripts/v2/planner/entities.py           ← AUTHOR_ALIASES_CURATED dict
                                            (handcrafted overrides — Russian
                                             stems, ambiguity guards, etc.)
```

Runtime merge: `AUTHOR_ALIASES = {**generated, **curated}` — curated wins
on conflict.

### When to use which

**Generated** (auto):
- Every author present in `/workspace/spgc/metadata.csv`
- Standard transliterations (Tolstoy ↔ Толстой) via `iuliia` library
- Common case forms

**Curated** (manual):
- Russian stem variants («толсто» catches «Толстого / Толстому / Толстым»)
- Ambiguity guards (e.g. «по» preposition collision with Poe)
- Multi-word aliases («конан дойл», «эдгар аллан по»)
- Non-standard transliterations

### Regression test

Every curated entry is automatically tested:
- `tests/v2/test_aliases_regression.py` reads `AUTHOR_ALIASES_CURATED` and
  for each `(alias, regex)` pair asserts that `extract(f"слова у {alias}")`
  returns `author_regex == regex`.
- Run: `pytest tests/v2/test_aliases_regression.py`

### Adding a new curated alias

1. Edit `AUTHOR_ALIASES_CURATED` in `entities.py`
2. Run `pytest tests/v2/test_aliases_regression.py` — passes automatically
3. If you need extra forms (genitive, etc.), add multiple entries; the regression test will pick them up

---

## 2. Adding a scoring metric (Phase B foundation)

### Protocol

```python
# scripts/v2/scoring/_base.py
from typing import Protocol

class ScoringPlugin(Protocol):
    name: str
    cost: Literal["cheap", "medium", "heavy"]
    def compute(self, query: ScoringQuery) -> list[ScoredItem]: ...
    def explain(self, scored: ScoredItem) -> str: ...
```

Where `ScoringQuery` is e.g. `{"author": "^Doyle,", "candidates": [...]}` or
`{"query_text": "...", "candidate_passages": [...]}` — same protocol for
**both** author-similarity and retrieval-reranking use cases.

### Registry

```python
# scripts/v2/scoring/__init__.py
REGISTRY: dict[str, ScoringPlugin] = {
    "burrows_delta":  BurrowsDelta(),
    "jaccard_top200": JaccardTop200(),
    "jsd_unigram":    JSDUnigram(),
    "bge_reranker":   BGEReranker(),   # lazy-loaded
    "ensemble":       Ensemble(["burrows_delta", "jaccard_top200", "jsd_unigram"]),
}
```

### Where it's used

- `author_influences(metric="ensemble")` — default in v3.0; opt-in to single metric for forensic comparison
- `hybrid_search(rerank_with="bge_reranker")` — cross-encoder rerank after RRF merge
- `compare_authors(metric=...)` — same registry, same protocol

### Regression test

`tests/v2/test_scoring_plugins.py` checks:
1. Every plugin in `REGISTRY` matches the Protocol
2. `compute()` returns deterministic results given fixed input fixture
3. `explain()` returns non-empty string

Adding a new plugin = new module + registration + one fixture row in the test.

### Lazy-load heavy plugins

```python
class BGEReranker:
    name = "bge_reranker"
    cost = "heavy"
    _model = None
    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder("BAAI/bge-reranker-base")
    def compute(self, query):
        self._ensure_model()
        return self._model.predict(...)
```

Avoids paying load cost on container startup; only first-use latency.

---

## 3. Adding an intent rule

### Where

`scripts/v2/planner/intent.py`:
1. Add the intent string to the `INTENTS` frozenset
2. Assign a priority in the `PRIORITY` dict (60 = corpus_meta low, 200 = out_of_scope high)
3. Add 1+ regex patterns to the `RULES` list, each `(pattern, intent, confidence)`

Then add a plan builder in `scripts/v2/planner/plan.py`:
4. Define `_plan_<new_intent>(e: Entities) -> QueryPlan`
5. Register in `PLAN_BUILDERS` dict

### Regression test

Every intent listed in `INTENTS` must have a plan builder (`test_plan.py::
PlanCoversAllIntents`) — adding to `INTENTS` without a builder fails CI.

### Where the regex rule lives in the pipeline

Each rule is `(re.Pattern, intent_label, confidence)`. On `classify(text)`,
the planner runs **all** patterns against the text, collects matches, and
picks `(priority desc, confidence desc)`. So a less specific pattern with
high priority beats a more specific one with low priority — pick priority
carefully.

For tie-breaks between author-context and corpus-context intents (the
«Толстой routing» bug from rounds 1-5), use **negative lookahead** in the
broader rule:

```python
# corpus_meta — but NOT when an author is present after «у»
(_re(r"\bсколько\s+(книг|book)\b(?!\w*\s+(у|of)\s+[А-ЯA-ZЁ])"),
 "corpus_meta", 0.95),
```

---

## 4. Adding an LLM fallback intent / entity slot

The LLM fallback in `scripts/v2/planner/llm_intent.py` extracts a fixed
JSON schema:

```json
{"intent": "...", "author": "...", "book_title": "...",
 "word": "...", "year_from": ..., "year_to": ..., "country": "..."}
```

To add a new entity slot (e.g. `topic`, `genre`):
1. Add to `_build_full_prompt()` prompt template
2. Add to `_clean_*` helpers (`_clean_topic`, etc.)
3. Add to `_merge_llm_entities()` in `rag_v2.py` so it fills the new
   `Entities.<slot>` field

Stays 1 LLM call, no extra latency.

---

## 5. Failed-query telemetry → regex synthesis

(Phase H3 — deferred per Stan 2026-05-19 «не горит, делаем после первого
внешнего user'a в проде»)

The `/admin/failed` endpoint already aggregates repeated failures. A
future «Promote to rule» button would:
1. Show top-N failed phrases sorted by count
2. Stan selects one + picks target intent
3. Tool generates a regex candidate from common tokens with word
   boundaries, presents diff
4. Stan approves → patch committed against `RULES`

For now: read `top_failed_phrases` JSON, hand-write rules.

---

## Anti-patterns to avoid

- **Don't put intent classification logic into plan builders.** Plan
  builders see `intent + entities` and produce steps. They don't
  re-classify. Cleaner separation = easier testing.
- **Don't grow `RULES` to 200 entries.** When you hit ~10 patterns per
  intent, consider whether the LLM fallback should handle the long tail
  and rules should cover only the high-volume canonical phrasings.
- **Don't write intent rules that require entity extraction.** Entities
  are extracted **after** intent. If you need entity info to classify,
  it's an entity-extraction problem, not an intent problem.
- **Don't add a hardcoded list for every gap.** If you'd write a list of
  20 things (Russian author names, book titles, etc.) — that's a
  candidate for auto-generation from corpus metadata, not a curated list.

---

## Versioning

- Adding entries to `AUTHOR_ALIASES_CURATED` / `KNOWN_BOOKS` — patch bump
- Adding new intent / plan builder — minor bump
- Adding new ScoringPlugin — minor bump (registry key is opt-in)
- Changing the LLM fallback JSON schema — major bump (breaks downstream
  serializers, cache invalidation needed)
