# wordcracker v2.4 — demon-round fixes (Stan's 2026-05-18 free-form probe)

Stan ran a 12-question free-form session on production
(<https://slovoeb.net>) and caught **6 fails out of 12** on phrasings
that the canned functional 40 missed. Real-world Russian isn't the
sentinel set. This release closes all 6.

## The pattern

Fails were not in logic — they were in **entity extraction + intent
triggers**. Regexes shaped around one specific phrasing don't carry
over to free-form Russian. So this release widens triggers (with
explicit negative tests so we don't drag in false positives) rather
than adding new logic.

## Fixes by question

| Q | Original symptom | Root cause | Fix |
|---|---|---|---|
| **Q2** «найди книгу Преступление и наказание» → clarify | KNOWN_BOOKS had the entry, but no intent rule fired for `найди книгу X` — fell through to clarify | New `book_lookup` intent (priority 122) → `_plan_book_lookup` → `find_book` directly. Even unresolved titles get a real tool call instead of a clarify. |
| **Q4** «словом fog у викторианцев» → clarify (fog not extracted) | `_WORD_AFTER_KEY` regex only matched «слово X», not the instrumental case «словом X» | Regex widened to all 5 cases: `слов(о\|а\|у\|ом\|е) X`. Plus English `word X / the word X`. |
| **Q5** «топ-5 британских по скачиваниям» sorted by books | No `top_metric` in entities; planner always passed `metric=books` even when downloads column was present | New `Entities.top_metric` field with `_find_top_metric()` — triggers «по скачиваниям», «по токенам», «по количеству книг». Planner threads it into `top_authors_by(_country)` args. |
| **Q6a** «этимология слова sword» → clarify (no quotes around sword) | Same root as Q4 + we never accepted bare token after the verb «этимология» | New `_WORD_AFTER_VERB` regex (distinct so we can tighten triggers separately): `(этимология\|происхождение\|соседствует\|collocates of\|etymology of) X`. |
| **Q7** «20 слов уровня intermediate из "P&P"» → clarify | Doc-example phrasing not matched by the `learning` rule, which expected explicit B1/B2 markers | New rule `\d+ слов* уровня X из/для` → learning (priority 0.95). Phrase exactly as the README's «good queries» list shows it. |
| **Q8** «процитируй полностью роман 1984 Оруэлла» → clarify | The structured copyright OOS only fired through the planner's copyright decorator — `clarify` path skipped it entirely | New OOS pattern catching `процитируй полностью / дай полный текст / give me the full text / quote verbatim`. Routes to `out_of_scope` (priority 200) regardless of which book is named — verbatim full-text reproduction is never something we ship through chat, copyright or not. |
| **Q9** «на кого по стилю похож X» → clarify | `author_closest` had `похожи на стиль X` rule but not `по стилю похож X` — Russian word order tripped it | New rule `(на кого\|кто) по стилю похож\* / по стилю похож\* на / стилистически (близок\|похож)`. |
| **Q3** «характерные прилагательные Уайльда» — ernest/caliban/nazarene/parnassus tagged as ADJ | spaCy systematically mis-tags isolated lowercased proper nouns; corpus-diff heuristic doesn't catch literary character names that bleed across many editions | Hard-coded `_LITERARY_PROPN_BLACKLIST` in the v2 `affinity_by_author` wrapper — Wilde character names (ernest, algernon, bunbury, cecily…), Tempest characters (caliban, prospero…), classical place names (parnassus, olympus, elysian…), Wodehouse-specific (wrykyn, threepwood, blandings). Extend pragmatically. |

## Side effect

While fixing Q3 I noticed v1 `affinity_by_author` returns `top_words`
in the newer code path but `top` in some legacy CSVs. The wrapper now
reads both. This also closes the «warning: affinity returned no words»
that appeared **alongside a 20-row table** (Stan's Q3) — the warning
fired against `top_words` while the renderer used `top`.

## What's out of scope for v2.4 (deferred)

| Q | Why deferred |
|---|---|
| Q1 «100% покрытие» vs «не вошло 24206» in `corpus_overview` answer | LLM rendering inconsistency — needs a separate prompt tweak on the corpus_overview tool, not an entity/intent fix |
| Q12 «По 1809–1964» — that's the publications-in-corpus range, not birth/death | Tool data layer issue in `author_metadata` — the field needs renaming, separate PR against the v1 tool |
| Q11 stats footer not live-updating | Cosmetic — footer polls /api/stats every 30s; Stan's session was probably shorter |
| Cosine = 0.0 between two weird-fiction authors | True insufficiency of the metric (top-N affinity vectors barely overlap by design); needs full-vocab tf-idf cosine as a second number — separate Sprint 12 item |

## Tests

- Unit: **207/207** (+1 — 9 new sub-tests in `test_stan_demon_round_2026_05_18`)
- 12 demon probes — all 12 classify correctly post-fix (verified locally)
- Existing 28 adversarial probes still pass (no positive-test regression)

## Coming next

Q1 / Q12 are pure rendering / data-label issues — fixing them needs
LLM prompt tweaks and a small change in `author_metadata` payload
shape. Will batch with Sprint 12 build_country_affinity + etymology
caches when those come up.

Co-developed with Claude Opus 4.7 (1M context).
