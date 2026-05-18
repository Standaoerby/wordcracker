# wordcracker v2.2 — Sprint 11 polish round 2

Continuation of the Sprint 11 quality/speed pass. v2.1 closed the critic
noise and tokens-cache items; v2.2 picks up the two pieces that were
explicitly deferred — Q40 composite intent and chat UI polish.

## Highlights

### Sprint 11.4 — composite_compare intent (Q40)

Q40 («Возьми все произведения 1850-1920, раздели на британских и
американских, … покажи 200 слов B2-C1 которые отличают британскую прозу
от американской») used to land in `country_compare`, which only fetched
top authors per country. Useful as a starting point but didn't surface
the lexical contrast the question actually asks for.

New `composite_compare` intent (priority 145, above country_compare's
135). Plan is a 4-step fan-out:

1. `top_authors_by_country(GB, metric=tokens, top=10)` — ~50ms via the
   Sprint 11.2 `author_tokens.json` cache.
2. `top_authors_by_country(US, metric=tokens, top=10)` — same.
3. `affinity_by_author` for the leader of GB top (`depends_on=[0]`,
   `inject_result_as="author_regex"`, optional).
4. `affinity_by_author` for the leader of US top (same shape).

Router `_inject` learned a new injection mode `author_regex`: pulls
`top[0].author` from a `top_authors_by(_country)` result and reshapes
`"Surname, First"` → `"^Surname,"` (v1 regex convention) before threading
into the next step's args.

LLM/renderer now sees both country-leader signature lists side-by-side
with the top-author rankings — real word-level differential rather than
just "here are the top GB and US authors." Full B2-C1 per-period lemma
affinity across all 20k books in the window stays out of scope; that
needs a corpus-side pre-computation (`build_country_affinity.py` is a
v2.3 candidate).

### Sprint 11.5 — UI polish

Sticky footer at the bottom of the chat page shows live counters pulled
from the v2 observability ring buffer (last 256 requests):

```
queries: 47   avg: 12.4s   cache: 32% (15/47)   critic flags: 4/47
```

Polls `/api/stats` every 30 s. New `GET /api/stats` returns
`observability.aggregate_recent()` — same payload the status dashboard
card consumes. Critic-flags counter turns orange when >30% of recent
requests get flagged, so the drift back into noisy-critic territory is
visible at a glance.

Retry-with-scope button on clarify responses: `↻ уточнить и переспросить`
pre-fills the input with the original query so the missing scope
(author / book / period) can be appended in place instead of retyping a
long question from the vault.

### Two caught bugs

Stan caught two more issues while flexing v2.1, both fixed in the same
release window:

- **«похожи на ИИ» false-matched `author_closest`** — 4 rephrasings of
  «почему тексты Азимова так похожи на написанные ИИ» all landed in
  `author_closest` then clarified «нужен автор». The bare
  `похож\w*\s+на` rule was too permissive. Tightened to require an
  author/style anchor after «похож…на». Honest clarify on free-form
  «похож на правду / на сказку / на ИИ» queries now.
- **«имени Анна»** didn't surface as `e.word`. `_find_word` only knew
  `"X"` and «слово X». Added `им(я|ени|енем)\s+X` regex with a
  proper-noun guard (capital first letter), so name probes thread
  through `word_contexts` → `hybrid_search` → `_maybe_translate` and
  resolve Russian names to English mentions in the corpus.

## Tests

- Unit: 190/190 (+4 over v2.1: `composite_compare` plan structure,
  `author_regex` router injection, `pohozhi_na_AI` regression,
  `name_after_imya` extraction)
- Functional 40/40 verified after deploy — Q40 now routes correctly to
  `composite_compare` with the 4-step plan
- No regressions in the 39 other queries

## What's not in v2.2

- `build_country_affinity.py` (full per-corpus lemma diff for Q40) —
  v2.3
- `find_words_by_etymology` family caches — v2.3
- Cache warm-up on restart — v2.3
- BGE-reranker integration — backlog
- Multi-model setup (planner / answer / reranker split) — backlog,
  ROI unclear while one wordcracker:v2 keeps clearing 40/40

Co-developed with Claude Opus 4.7 (1M context).
