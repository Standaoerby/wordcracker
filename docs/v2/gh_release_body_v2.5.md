# wordcracker v2.5 — demo polish

Stan's heading toward showing wordcracker to a real first-time user
tomorrow. This release is the «нестыдно показать» pass: closing the
3 remaining quality bugs from his demon round (Q1, Q12, cosine=0),
giving the empty-chat screen actual suggestions instead of a tiny
gray hint, and warming the cache so the first user query doesn't
take 60 seconds cold.

## Demo UX

### Clickable suggestion chips

Pre-first-message state used to be just:

```
Примеры: «дай статистику по Wodehouse», «топ-15 биграмм Достоевского»…
```

— small gray text under the log. First-time users either ignored
it or typed something only loosely similar.

v2.5 replaces it with **12 clickable chips in 4 categories**:

| Стиль автора | Книги | Слова | Корпус |
|---|---|---|---|
| фирменные слова Уайльда | уровень сложности Pride and Prejudice | этимология слова sword | сколько книг в базе |
| сравни По и Лавкрафта | архаизмы в Dracula | соседствует со словом fog | что у тебя с копирайтом |
| на кого по стилю похож Doyle | прилагательные в "Dorian Gray" | примеры слова "ajar" | топ-5 британских авторов |

Click → fills textarea → submits. Chips hide as soon as the conversation
starts; reappear on «clear». Each query is one the system actually
handles well — the chips double as a self-test surface.

### Dispatch warm-up at restart

The existing `_warmup()` only touched ChromaDB. v2.5 also pre-runs the
v2 dispatch on:

- `corpus_overview` (typical first-time-user opener)
- `top_authors_by(metric=books)` (typical second query)
- `top_authors_by(metric=downloads)` — separate cache key
- `top_authors_by(metric=tokens)` — uses the Sprint 11.2 JSON cache
- `author_metadata("^Doyle,")` — warms the parquet read + geo lookup

Total warm-up time adds ~3-4 s to systemd start. First user query
that hits any of these is now a cache hit instead of a cold dispatch.

## Q1, Q12, cosine=0 — render-layer fixes

These three Stan caught during demon-round all rendered confusing
output even though the underlying tool data was correct. Each is
fixed by adding a `_render_note` field to the tool result that
instructs the LLM exactly how to present the numbers, without
needing changes to the Modelfile SYSTEM prompt.

### Q1: «100% покрытие индекса» vs «не вошло 24 206 книг»

`corpus_overview` returns:

- `raw_books_available` ≈ 55 101 (всё что есть на диске)
- `chromadb_chunks` ≈ 3.86M (semantic index, EN only)
- `index_gap_approx` (= raw - chunks/125, ≈ 25 000)
- (separately) FTS5 index covers all 55 094 books

The LLM was conflating semantic-index gap (25k) with FTS5 coverage
(100%) and printing both in the same paragraph as contradictory facts.

Fix: explicit `semantic_index_books_approx` + `semantic_index_coverage_pct`
fields, plus an `_render_note` explicitly telling the LLM «ChromaDB
covers ~50% EN-only, FTS5 covers all 55k — don't merge them into
one percent».

### Q12: «По 1809–1964» (Poe died in 1849)

`author_metadata` reads `year_of_death_max` from Gutendex CSV. For
Poe some edition records have `authoryearofdeath` set to the
publication year of that edition (1964), not the author's actual
death year. v2 wrapper now:

- Drops `year_of_death_max` if the implied life span exceeds 120 years
  (Gutendex confused death with edition publication)
- Exposes the dropped value as `year_of_death_max_unreliable` so the
  LLM can warn the user
- Adds `_render_note` telling the LLM these are biographical (not
  corpus publication range) and to caveat if unreliable

### Cosine = 0.0 between two weird-fiction authors

`compare_authors` cosine on top-N affinity vectors is structurally
near-zero because the top-N for distinct authors almost never
overlaps by construction. Stan reported this looking «suspicious».

Fix:

- `cosine_is_structural_zero` boolean when cosine < 0.05
- `shared_top_words_count` integer the LLM can quote directly
- `_render_note` instructing «don't say «authors are completely
  different» because of cosine=0 — talk about
  `shared_high_affinity` and `top_unique_a/b` instead»

The `cosine_note` field already existed in v1 but the LLM was
under-using it; the structured boolean + integer + explicit note
makes it impossible to miss.

## Other fixes

- **affinity_by_author** wrapper now reads both `top_words` (newer
  v1 path) and `top` (legacy CSV path). Fixes Stan's «warning:
  affinity returned no words» appearing alongside a 20-row table
  in Q3 — the warning was checking `top_words` while the renderer
  used `top`.
- **footer live update** — already polls /api/stats every 30 s
  (existing code path), verified working post-deploy.

## Tests

- Unit: **207/207** (no behavioral changes, just additional fields
  in tool results and UI HTML)
- 12 demon probes — all 12 classify + plan correctly
- 28 adversarial probes — still pass

## Coming next (Sprint 12 backlog)

- `build_country_affinity.py` — full per-corpus lemma diff for Q40
- Etymology family caches (Wiktionary top-50 per family)
- Reranker (BGE-reranker-base) for hybrid_search top-30→10
- Full-vocab tf-idf cosine in compare_authors as second number
- Rate limiting per IP (nginx layer)

Co-developed with Claude Opus 4.7 (1M context).
