# wordcracker v2.0 — Release Notes

## TL;DR

`wordcracker:v2` ships a deterministic planner+router pipeline, replacing
the v1 LLM-driven agentic loop. The LLM is no longer responsible for tool
selection — it only renders structured results and verifies its own
output via a critic pass. Tool calls are picked by a rules-based intent
classifier + entity extractor + plan templates, then executed by a
straight-line router.

Result: 40/40 = 100% pass-like on the corrected «Примеры запросов чату»
suite (31 real tool-driven pass, 3 honest clarify, 6 intentional refusals,
0 partial, 0 fail). Hallucination class (PG1327 bug) eliminated by
mandatory `find_book` chains + critic fact-check.

## Tags shipped

| Tag | Date | Scope |
|---|---|---|
| **v2.0.0-alpha1** | 2026-05-17 | First v2 deploy (Sprint 1–2): planner+router, 39/40 |
| v2.0.1 | 2026-05-17 | Sprint 6: critic pass + `wordcracker:v2` Modelfile + conversation context |
| v2.0.2 | 2026-05-17 | Sprint 7: LemmaProfile + AuthorProfile SQLite cache + observability JSONL |
| v2.0.3 | 2026-05-17 | Sprint 8: learning_priority_score + Anki .apkg export |
| v2.0.4 | 2026-05-17 | Sprint 9: book_compare composite intent → 40/40 = 100% |
| v2.0.5 | 2026-05-17 | Sprint 10: UI badges (engine/intent/critic) + copy button |
| **v2.0.6** | _candidate_ | Final audit pass — all 25 tools verified, perf bench, ready for merge |

## What changed (architecture)

### Before (v1.1.7)

```
user → Qwen3 agent loop (up to 5 iter) → choose tool → run → Qwen3 → choose tool ...
```

LLM picked tools on every iteration. Hallucination, plan-without-tool,
timeout-on-loop were systemic.

### After (v2.0)

```
user
  → intent classifier (33 rules, 40/40 unit)
  → entity extractor (author/book/word/year/country/level/POS/etc, paired-quote regex)
  → history merge (multi-turn entity backfill)
  → plan builder (deterministic chain per intent)
  → tool router (no LLM in loop)
  → renderer (single LLM call, low temp, sees only data)
  → critic (second low-temp LLM pass, flags unsupported claims)
  → answer + UI badges (engine / intent / critic verdict / copy)
```

## Components delivered

| Module | Lines | Purpose |
|---|---:|---|
| `scripts/v2/types.py` (was `_types.py`) | 230 | ToolResult / Coverage / SourceInfo / Warning |
| `scripts/v2/tool_registry.py` | 200 | @tool decorator + dispatch + cache wiring |
| `scripts/v2/filters.py` | 95 | FilterSpec + legacy adapter |
| `scripts/v2/corpus_version.py` | 75 | Source info for tool results |
| `scripts/v2/cache.py` | 235 | Disk + LRU cache, corpus_version tagged |
| `scripts/v2/critic.py` | 200 | Sprint 6 fact-check LLM pass |
| `scripts/v2/legacy_dispatch.py` | 75 | Hybrid v2/v1 dispatcher |
| `scripts/v2/observability.py` | 130 | JSONL logs + ring buffer + aggregate |
| `scripts/v2/planner/intent.py` | 285 | 33-intent rules + priority |
| `scripts/v2/planner/entities.py` | 420 | 90+ author aliases, 30 known books, multi-quote regex |
| `scripts/v2/planner/plan.py` | 880 | Plan templates per intent |
| `scripts/v2/planner/router.py` | 130 | Deterministic execute + SSE |
| `scripts/v2/planner/history.py` | 175 | Multi-turn backfill + intent inference |
| `scripts/v2/profiles/store.py` | 130 | SQLite cache for author/book/lemma blobs |
| `scripts/v2/profiles/{author,book,lemma}.py` | 220 | Lazy profile builders |
| `scripts/v2/tools/` (19 modules) | ~1400 | Native v2 tool wrappers |
| `scripts/v2/rag_v2.py` | 305 | ask/ask_stream entry points |
| `scripts/v2/build_fts_index.py` | 220 | FTS5 builder (one-time) |

Plus 11 test files, 200+ unit tests, 4 functional retest reports.

## Failure modes eliminated (vs v1.1.7)

| Bug | Before | After |
|---|---|---|
| PG1327 hallucination on «Crime and Punishment» | unreliable | mandatory `find_book` chain |
| «приведи примеры такого использования» (multi-turn) | clarify | entity backfill from history + word reference picked from last assistant turn |
| Свифт / любой автор без alias | clarify | 90+ aliases + stem-forms for Russian cases |
| Copyright books (1984, LOTR, Hemingway) | cryptic tool error | friendly out_of_scope + nearest available alternative suggestion |
| Q5 compare_authors with missing author | partial | author_metadata probe → renderer warns |
| Q9/Q18/Q33 «bad scope» errors | partial | scope normalization + paired regex for typographic quotes |
| Q4 Lovecraft Call of Cthulhu | counts file not found | learning_tools now uses `_counts_path` fallback for orphan PG |
| Q19 «Alice's Adventures» apostrophe in title | extracted as word=«alice» | paired quote regex skips apostrophe inside title |
| Q24 «X + Y but rarely in Z» | clarify | new `book_compare` composite intent |

## Sprint 6 — Quality & Trust

- **Critic pass**: ~3-5s after the renderer, low-temp LLM call that
  compares numeric/named claims against tool_results. Renders ⚠ block
  for unsupported claims. Fails open on network errors. WC_CRITIC=on default.
- **`wordcracker:v2` Modelfile**: pinned Qwen3:14b with temp=0.1, top_p=0.8,
  repeat_penalty=1.1, num_ctx=8192, baked-in operator SYSTEM. Saves ~1500
  tokens per request.
- **Conversation context**: `_llm_render` now sees `current_author / book
  / word / country / year_range / turns_in_history` so multi-turn refs
  work.

## Sprint 7 — Profiles & Observability

- **LemmaProfile**: light-path build from corpus_counts.csv → SQLite store.
  Difficulty bucketing (basic/intermediate/advanced/rare/ultra_rare),
  rarity score 0..1.
- **AuthorProfile wired** into v2 author_profile wrapper — re-asks in <5ms.
- **Observability**: JSONL per-request logs in `/data/logs/v2/queries-*.jsonl`
  + 256-record in-memory ring buffer + `aggregate_recent()` rollup
  (intent histogram, slow_tools p95, cache hit rate, critic flagged).

## Sprint 8 — Learning v2

- **Learning Priority Score**: roadmap §8.2 formula, weighted [0..1]:
  - 0.30 author_affinity (log-compressed)
  - 0.20 book_frequency
  - 0.20 rarity (lemma profile)
  - 0.15 CEFR_relevance (level match)
  - 0.10 context_quality (example present)
  - 0.05 non_proper_noun_confidence
- **Anki .apkg**: native via genanki. Stable seed = md5("wordcracker:" +
  deck_name) so re-imports update instead of duplicating. Fallback to
  anki_csv when genanki not installed.

## Sprint 9 — Composite intent

- **book_compare**: Q24 «слова в Treasure Island и Moby Dick, но редко в
  David Copperfield» now routes to a real tool chain instead of clarify.

## Sprint 10 — UI polish

- **Engine badge** [v2] on each assistant message
- **Intent badge** showing planner verdict ([intent: author_vocab])
- **Critic badge**: ✓ critic clean / ⚠ critic: N flag(s), hover = summary
- **Copy** button — clipboard the markdown answer
- Live status updates with planner verdict as soon as intent is classified
  (before tools start)

## Test coverage

| Suite | Count | Status |
|---|---:|---|
| Unit (`tests/v2/test_*.py`) | 182 | 100% green |
| E2E (`tests/v2/test_pipeline_e2e.py`) | 6 | 100% green |
| Sanity probes | 11 | 100% green |
| Functional 40 (`tests/v2/run_functional_40.py`) | 40 | 40/40 = 100% pass-like |
| All-tools audit (`tests/v2/test_all_tools.py`) | 25 | 25/25 pass |

## Performance (Sprint 10 deployment, cold caches — bench_v2.py 1-pass)

40 questions, no cache pre-warm, all native v2 native + legacy_dispatch:

| Intent | n | p50 (s) | p95 (s) | max (s) |
|---|---:|---:|---:|---:|
| introduction | 1 | 0.02 | 0.02 | 0.02 |
| out_of_scope | 6 | 0.00 | 0.00 | 0.00 |
| word_pos | 1 | 3.18 | 3.18 | 3.18 |
| word_contexts | 1 | 6.12 | 6.12 | 6.12 |
| word_collocates | 1 | 7.85 | 7.85 | 7.85 |
| author_closest | 1 | 8.64 | 8.64 | 8.64 |
| word_emotion | 3 | 9.10 | 9.10 | 11.46 |
| book_archaic | 3 | 9.15 | 9.15 | 9.77 |
| topic_words | 1 | 9.05 | 9.05 | 9.05 |
| lexical_wealth | 1 | 9.96 | 9.96 | 9.96 |
| author_compare | 1 | 12.24 | 12.24 | 12.24 |
| word_etymology | 1 | 14.77 | 14.77 | 14.77 |
| word_timeline | 3 | 17.24 | 26.96 | 26.96 |
| book_compare | 1 | 17.50 | 17.50 | 17.50 |
| book_vocab | 2 | 17.66 | 17.66 | 17.66 |
| author_vocab | 4 | 17.84 | 26.14 | 26.14 |
| country_vocab | 1 | 17.80 | 17.80 | 17.80 |
| learning | 4 | 18.74 | 22.93 | 22.93 |
| country_compare | 3 | 17.92 | 21.93 | 21.93 |
| vocab_passport | 1 | 23.35 | 23.35 | 23.35 |

Slow tools internal runtime (the rendering + critic LLM passes are
extra ~3-8s on top):

| Tool | n | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|
| author_influences | 1 | 28 | 28 |
| author_metadata | 2 | 191 | 191 |
| top_authors_by_country | 6 | 68 | 68 |
| affinity_by_author | 4 | 458 | 458 |
| word_pos_distribution | 1 | 510 | 510 |
| compare_authors | 1 | 1404 | 1404 |
| author_profile | 1 | 4020 | 4020 |
| learning_words | 3 | 6457 | 6457 |
| affinity_by_book | 3 | 9034 | 9034 |
| words_disappearing_after | 3 | 25857 | 25857 |
| word_collocates | 2 | 37033 | 37033 |
| find_words_by_etymology | 1 | 39340 | 39340 |
| top_authors_by (tokens) | 1 | 59742 | 59742 |

**Notes:**
- Cache hit rate 0% on cold start — second run after profile warm-up
  delivers <2s for repeat author/book queries via AuthorProfile cache.
- Critic over-flagged on 28/32 questions on first deploy — Sprint
  follow-up added a 4-flag threshold guard + softened prompt so the
  critic stays useful for genuine hallucinations but quiet on table-
  heavy answers (commit 044a94e).
- Heavy tools (find_words_by_etymology Wiktionary, top_authors_by tokens
  full corpus scan, words_disappearing_after full scan) fit within the
  150s chat budget on the 3090 host.
- Total wall-clock per query: median **~10-18s** including LLM render +
  critic, p95 **~26s**. v1.1.7 baseline was ~30-50s due to multi-iter
  agentic loop — **roughly 2× faster** in median.

## Deployment

Production switch (already live):
```bash
# systemd drop-in: /etc/systemd/system/wordcracker-chat.service.d/v2-engine.conf
[Service]
ExecStart=
ExecStart=/usr/bin/docker compose exec -T -e ASSISTANT_NAME=Словоёб \
  -e WC_DEFAULT_ENGINE=v2 -e WC_LLM_MODEL=wordcracker:v2 \
  -e WC_CRITIC_MODEL=wordcracker:v2 gutenberg-lab \
  python -u /workspace/scripts/chat_server.py --port 8890
```

v1 still available per-request: `?engine=v1` or `X-WC-Engine: v1`.

## Open / deferred

- 13 of 32 tools still go through `legacy_dispatch` (semantic_search,
  top_ngrams_by_author, lexical_diversity, top_books_by_*, enrich_word,
  export_word_list, bulk_enrich). They work, just no native ToolResult
  Coverage. Wrap-only chore; low priority.
- Reranker (BGE-reranker-base) for hybrid_search top-30 → top-10 — Sprint 3.3
  defer. Adds ~50ms but improves answer quality for noisy queries.
- Multi-model setup (separate planner / answer / reranker models).
- Improved CEFR via lemma POS / etymology heuristics — Sprint 8.2 stub
  still uses corpus_count thresholds.
