# wordcracker v2.6 — hybrid intent + Stan demon round 2 fixes

Stan's 2026-05-18 round 2 demon test hit **10 out of 20 free-form
queries** as `clarify` — 50% pass rate. The problem isn't logic;
it's that **rule-based intent matching can't keep up with the
breadth of natural Russian phrasing**. Adding more regex variations
buys 1-2% at a time and breeds maintenance debt.

v2.6 makes the architecture **hybrid**: rules win the 50% of clean
phrasings (free, deterministic, instant), and a local LLM fallback
catches the rest.

## The big change: LLM intent fallback

New module `scripts/v2/planner/llm_intent.py`. When rules return
`clarify` AND `history.infer_followup_intent` also returns `None`,
we ask `wordcracker:v2` (already loaded) to pick ONE of the 35
intent labels. The model isn't doing tool calling — just
classification, with a tight 1 KB system prompt that lists every
intent + a one-line use case.

```
user → rule classifier (50% hit, 0 ms)
     → history followup (~5% more)
     → LLM fallback (the rest, ~1-2 s on local 3090)
     → plan builder (unchanged)
```

What this closes from Stan's round 2:
- Q1 «ну привет, ты кто вообще?» → `introduction`
- Q2 «слушай а сколько у тебя книжек?» → `corpus_meta`
- Q4 «а у пушкина?» (follow-up with no trigger word) → with prior
  history threading + LLM → `author_vocab`
- Q8 «найди-ка преступление и наказание» → `book_lookup`
- Q11 «есть ли у тебя гарри поттер?» → `book_lookup` → KNOWN_BOOKS
  → copyright OOS
- Q15 «а во Frankenstein?» → context-aware → `book_archaic`

Defensive properties:
- **Cache**: per-process LRU on `text_lower[:200]` → label.
  Repeated phrasings = no second LLM call.
- **Timeout**: 8 s default (env `WC_LLM_INTENT_TIMEOUT_S`); on
  failure falls back to `clarify`, never crashes.
- **Toggle**: `WC_LLM_INTENT_ENABLED=0` disables entirely, leaves
  pure rules — useful for auditing regex coverage.
- **Telemetry**: every LLM call logged with `matched_pattern=
  llm-fallback` so Stan can see which phrasings needed it.
- **Taxonomy-sync test**: unit test verifies every intent in
  `INTENTS` has a hint and vice-versa, so adding a new intent
  without a hint throws.

## Stan round 2 confusion-bug fixes

These six all classified correctly but produced confused output
even with v2.5's `_render_note`. v2.6 hardens each.

### Q3 — Wilde affinity returned «wilde» itself

«фирменные слова Уайльда» topped with `wilde, tetrarch, cardew,
daubeny, phipps, simone, symonds, yeats, topazes`. Two problems:
the author's own surname leaked, and Wilde-specific character names
(Salome's Tetrarch, Importance of Being Earnest's Cardew, Lord
Goring's Phipps) weren't in v2.4's literary-PROPN blacklist.

Fix:
- `_drop_author_self_name()` — strip the surname extracted from
  `^Surname,` regex. An author's own name is not their style.
- Extended `_LITERARY_PROPN_BLACKLIST` with the Wilde set + Russian
  translit character names from Pushkin / Tolstoy demon round 1.

### Q9 — Learning_words returned Lambton, Shire as B1 vocab

Pride & Prejudice locations leaked into the study list. New
`_LITERARY_LOCATION_BLACKLIST` in the v2 `learning_words` wrapper
covers P&P, S&S, Wuthering Heights, Frankenstein, Dracula,
Treasure Island, Moby Dick, Hound of the Baskervilles, mythology.

### Q10 — author_closest returned «Various» (3634 books) on top

«На кого похож Conan Doyle» ranked the multi-author Various
placeholder #1. New `_is_collection_bucket()` filter drops
Various / Anonymous / Unknown / N/A / Encyclopedia / Catholic
Church / Multiple / Collection / Compilation buckets before render.

### Q12 — Poe came back as 1809–1964 *again*

v2.5 supposedly drops `year_of_death_max` when implied life span
> 120 yrs. The filter never fired because the v1 returns
`numpy.int64`, and `isinstance(yob, int)` is **False** for numpy
ints. Coerce both values via `int(yob_raw)` so the filter
actually runs.

### Period parsing — «у викторианцев»

`_VICTORIAN` regex required the `-нск-` infix, missing the
plural-genitive «викторианцев». Extended to `виктори[аяь]н(?:ск\w*
|ц\w*|к\w*)`. Also added `_EDWARDIAN` (1901-1914) and `_NINETEENTH`
(«19 век», «XIX century», «девятнадцатый век»).

### Biographical queries — «годы жизни эдгара по?»

Rule only knew «когда родился», not «годы жизни». Added
`(годы|даты) жизни`, `birth and death`, `life (years|dates|span)`,
plus `биография / biograph*` and «что ты знаешь о X».

### Emotion profile — «эмоциональный профиль Dracula»

`book_emotion` had no rule at all and **wasn't in `PRIORITY` dict**
(defaulted to 0, lost to every other match). Added the rule with
priority 115 and the explicit pattern «эмоциональный профиль X»,
«emotional profile X», «sentiment of X».

### Bigrams — «топ-15 биграмм у Конан Дойла»

Documented as a good query in README + clickable chips, but had no
rule. Added pattern catching `биграмм / триграмм / bigram /
trigram` → `author_top_words`. Plan extended to detect bigram /
trigram triggers and pass `n=2` or `n=3` to `top_ngrams_by_author`
(default stays `n=1` for «самое частотное слово»).

## Tests

- Unit: **224/224** (+17 new tests for `llm_intent`: parse label
  cases, taxonomy sync, cache, network error, disabled mode,
  history threading)
- 20-question dry-run on Stan's round 2 queries: **14/20 rules-only
  pass** (was 10/20 on v2.5.1) — the other 6 are exactly the
  free-form opener / follow-up class that LLM fallback handles.

## Sprint 13 backlog (deferred)

- Wikidata-based author birth/death enrichment as fallback when
  Gutendex CSV has the publication-year bug
- Full-vocab tf-idf cosine in compare_authors as a second number
- Hallucinated signature words still emitted by the LLM in some
  cases (Q13 Lovecraft → «amended/editorials/journalism»); critic
  catches it but the raw output is still wrong. Needs renderer
  prompt tweak.
- POS-tag accuracy on isolated lemmas in learning_words (20%
  errors per Stan: `Elopement` tagged adj, `Imprudent` tagged noun)

Co-developed with Claude Opus 4.7 (1M context).
