# wordcracker v2/v3 — extension points

How to add things without breaking the deterministic pipeline. Sprint 16
(v3.0) introduces three plugin surfaces — author aliases, scoring metrics,
and intent rules — each with its own contract and regression-test scaffold.

## Pipeline at a glance

```
user query
  → intent.classify()          — 80+ regex rules + LLM fallback
  → entities.extract()         — author / book / word / period (alias-driven)
  → history.merge_with_history — multi-turn entity backfill
  → plan.build()               — 38 plan templates (intent + entities → steps)
  → router.execute()           — tool dispatch (no LLM in loop)
  → renderer (1 LLM call)      — strict facts-only render (rule 11 = counts)
  → critic.review()            — fact-check pass, MAX_FLAGS guard
  → numeric_audit.audit_numbers — programmatic count check (Phase D)
```

Four extension points let new functionality slot in **without changing
the core pipeline** — only changing data the pipeline reads or plugin
slots it dispatches to.

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

## 2. Adding a scoring metric (Phase B + C)

### Protocol

```python
# scripts/v2/scoring/__init__.py
from typing import Literal, Protocol, runtime_checkable

ScoringKind = Literal["author_similarity", "retrieval_rerank", "word_pair"]

@runtime_checkable
class ScoringPlugin(Protocol):
    name: str
    kinds: tuple[ScoringKind, ...]   # which use-cases the plugin supports
    cost: Literal["cheap", "medium", "heavy"]
    def compute(self, query: ScoringQuery) -> list[ScoredItem]: ...
    def explain(self, scored: ScoredItem) -> str: ...
```

`ScoringQuery` carries `(kind, target, candidates, options)`. Shape of
`candidates` depends on `kind`:

| kind | target | candidates | options |
|---|---|---|---|
| `author_similarity` | author regex `^Doyle,` | list of author regexes | `{"top": int}` |
| `retrieval_rerank` | query text | list of `(id, text)` tuples or dicts | — |
| `word_pair` | target word | list of `{"word", "c_pair", "c_neighbor"}` | `{"c_target", "N", "window"}` |

### Registry (7 plugins)

```python
# scripts/v2/scoring/__init__.py
REGISTRY: dict[str, ScoringPlugin] = {
    "burrows_delta":  BurrowsDelta(),    # author_similarity, medium
    "jaccard_top200": JaccardTop200(),   # author_similarity, cheap
    "ensemble":       Ensemble(),         # author_similarity, medium (Borda count)
    "bge_reranker":   BGEReranker(),     # retrieval_rerank, heavy (lazy-loaded)
    "pmi":            PMI(),              # word_pair, cheap
    "npmi":           NPMI(),             # word_pair, cheap (Bouma normalized)
    "dice":           Dice(),             # word_pair, cheap
}
```

### Where each kind is used

- `author_similarity` → `author_influences()` (default = `ensemble`)
- `retrieval_rerank` → `hybrid_search(rerank_with="bge_reranker")` and
  `find_book_by_topic(rerank_with="bge_reranker")` (cross-encoder reorder
  after RRF merge)
- `word_pair` → `word_collocates(metric="pmi" | "npmi" | "dice",
  min_cooccurrence=5)` — reranks raw window-cooccurrence counts using
  scope-level marginal frequencies

### Regression test

`tests/v2/test_scoring_plugins.py` auto-iterates `REGISTRY`:
1. Every plugin satisfies the runtime-checkable Protocol
2. `name` matches the registry key
3. `kinds` is a non-empty tuple, `cost` ∈ `{cheap, medium, heavy}`
4. `get(name)` and `list_plugins()` return the entry

Per-plugin behaviour tests live alongside (direction, bounds, soft-fail).

Adding a new plugin = new class + REGISTRY line; the contract suite
catches it automatically. Per-plugin math tests are optional but
recommended.

### Lazy-load heavy plugins

`BGEReranker` is the canonical example: model is `None` at import time,
loads `sentence-transformers` + `BAAI/bge-reranker-base` only on first
`compute()`. Container startup stays fast; only first-use pays the
~440 MB download.

```python
class BGEReranker:
    name = "bge_reranker"
    kinds = ("retrieval_rerank",)
    cost = "heavy"
    def __init__(self): self._model = None
    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder("BAAI/bge-reranker-base")
        return self._model
    def compute(self, query):
        if query.kind != "retrieval_rerank": return []
        ...
```

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

## 4. Tuning the numeric audit (Phase D)

After the critic LLM pass, `scripts/v2/numeric_audit.py` runs a
**programmatic** sweep over numbers in the rendered answer. Catches the
specific failure mode where the LLM writes a count that doesn't appear
in any tool result («у Doyle 47 → answer says 200»). The critic missed
this 30-40% of the time; the audit catches it deterministically.

### Knobs (env)

```
WC_NUMERIC_AUDIT=on       # default; off disables completely
WC_AUDIT_MIN=5            # ignore numbers below this (skip «top 2» noise)
WC_AUDIT_TOL=0.10         # ±10% match window; 0.05 = stricter
```

### Skip-list

`_INTENT_SKIP` is a small frozenset of intents whose answers don't need
numeric verification (`introduction`, `clarify`, `out_of_scope`). Add to
it when a new chatty intent ships and you see false-positives in the
admin log.

### Behaviour contract

- **Never crash the pipeline.** All errors → `AuditReport.trust()`.
- **Never modify the answer when no mismatches.** Footer attaches only
  when `report.has_issues()` is True.
- **Cap at 3 mismatches.** Beyond that, the renderer is broken
  wholesale, not tactically hallucinating.
- **Year-like numbers** (1500-2100) pass through unless context shows
  they were used as a count («1881 книг»). `_bio_source: hardcoded`
  data is trusted on the data side.

### Adding a new heuristic

If a class of false-positive shows up in `/admin/failed`:
1. Identify the pattern (e.g. percentages stripped of `%`, ISO dates)
2. Add a guard in `_is_year_like` or write `_is_<kind>_like(value, context)`
3. Call from `audit_numbers()` between min_value check and `_is_matched`
4. Add 2-3 cases in `tests/v2/test_numeric_audit.py::YearLike` style

---

## 5. Adding an LLM fallback intent / entity slot

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

## 6. Failed-query telemetry → regex synthesis

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
