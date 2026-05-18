# wordcracker v2.0

Major rewrite — LLM no longer drives tool selection.

## Highlights

- **Deterministic planner pipeline** replaces v1's agentic LLM loop: intent classifier → entity extractor → plan builder → tool router → renderer → critic.
- **40/40 = 100%** on the 40-question functional suite (31 real tool-driven pass, 3 honest clarify, 6 intentional refusals, 0 partial, 0 fail).
- **2× faster** median wall-clock: ~10-18s (was ~30-50s in v1.1.7).
- **All 35 tools** wrapped in native v2 `ToolResult` envelope (Coverage, Warnings, source_info, runtime_ms).
- **Critic pass** flags unsupported numeric/named claims via second low-temp LLM call. Over-flag guard suppresses table-row false positives.
- **FTS5 lexical index** (55094 docs, 27GB) + **hybrid_search** with RRF merge against ChromaDB semantic index.
- **`wordcracker:v2`** custom Ollama model (Qwen3:14b base, baked-in SYSTEM, temp 0.1, num_ctx 8192).
- **Multi-turn history**: «приведи примеры такого использования» now correctly threads author/book/word from prior turn.
- **Graceful copyright refusals**: 1984/LOTR/Hemingway return friendly out_of_scope with suggested public-domain alternatives.

## Eliminated failure modes from v1.1.7

| Bug | Resolution |
|---|---|
| PG1327 hallucination on «Crime and Punishment» | Mandatory `find_book` chain in plan templates |
| Multi-turn refs ignored | `history.py` entity backfill + intent inference |
| Authors without alias (Свифт) | 90+ alias dict with Russian case stems |
| Copyright books → cryptic errors | `_copyright_refusal_if_book_under_copyright` helper, KNOWN_BOOKS placeholder entries |
| Compare with missing 2nd author | `author_metadata` probe before chain |
| Bad scope errors (Q9/Q18/Q33) | `_scope_dict_or_clarify` widens to `{author:'.*', filters...}` |
| Orphan PG counts not found (Q4) | `learning_tools` switched to `_counts_path` fallback |
| Quoted title with apostrophe (Q19 Alice's) | Paired-quote regex per quote pair |
| Composite intent (Q24) | New `book_compare` intent + plan template |

## Tests

- 184 unit tests, all green
- 25/25 all-tools audit
- 40/40 functional suite (3 separate verification runs)
- Perf bench reproducible via `tests/v2/bench_v2.py`

## Deployment

```bash
# Already live in production via systemd drop-in:
cat systemd/wordcracker-chat.service.d/v2-engine.conf
# Set WC_DEFAULT_ENGINE=v2, WC_LLM_MODEL=wordcracker:v2,
# WC_CRITIC_MODEL=wordcracker:v2 → restart wordcracker-chat.service.

# v1 still per-request opt-out:
curl -X POST .../api/chat -H 'X-WC-Engine: v1' -d ...
```

## Architecture

See [docs/v2/RELEASE_NOTES.md](docs/v2/RELEASE_NOTES.md) — full architecture write-up, component inventory, performance breakdown, and deferred-items list.

## Sprint cadence

Six sprints of incremental shipping, each tagged + functional-retest verified:

| Tag | Sprint |
|---|---|
| v2.0.1 | Critic pass + Modelfile + conversation context |
| v2.0.2 | LemmaProfile + AuthorProfile cache + observability JSONL |
| v2.0.3 | Learning priority score + Anki .apkg |
| v2.0.4 | book_compare composite intent (Q24 closed → 40/40) |
| v2.0.5 | UI badges (engine/intent/critic/copy button) |
| v2.0.6 | Critic over-flag guard + RELEASE_NOTES |
| **v2.0** | v2-refactor → main fast-forward, full native migration, README |

Co-developed with Claude Opus 4.7 (1M context) over a single-day refactor session.
