# wordcracker

NLP-аналитический сервис над корпусом Project Gutenberg (~55 000 книг).
Веб-чат с LLM-агентом, частотная аналитика, стилометрия, контексты, vocabulary
learning, hybrid retrieval (FTS5 + semantic). Хост — RTX 3090 / Ubuntu 24.04
в Docker.

**Live:** <https://slovoeb.net> (chat), <https://status.slovoeb.net> (health
dashboard), <https://admin.slovoeb.net> (upload новых книг).

---

## v2.0 (2026-05-17) — текущая версия

Архитектура переписана на детерминированный планировщик. LLM больше не
выбирает tools на каждой итерации — это делают rules-based intent classifier
+ entity extractor + plan templates. LLM остаётся только для финального
рендера и второго critic-прохода.

```
user → intent classifier (33 правила)
     → entity extractor (90+ aliases, 30 KNOWN_BOOKS, paired-quote regex)
     → history.merge (multi-turn entity backfill + intent inference)
     → plan builder (deterministic chain per intent)
     → tool router (no LLM in loop)
     → renderer (single LLM call, low temp, sees structured data only)
     → critic (second low-temp LLM pass, flags unsupported claims)
     → answer + UI badges (engine / intent / critic / copy)
```

### Что изменилось vs v1.1.7

| Аспект | v1.1.7 | v2.0 |
|---|---|---|
| Tool selection | LLM выбирает на каждой итерации (до 5 раз) | Детерминированный planner |
| Hallucination | Иногда выдумывал PG id (см. v1.0e issue) | Mandatory `find_book` chain + critic verify |
| Multi-turn | История терялась | `history.py` backfill last author/book/word |
| Copyright books | Тула фейлила | Friendly out_of_scope с альтернативой |
| Median latency | 30-50s (agentic loop) | **10-18s** (single render call) |
| Functional 40/40 | 13 pass / 3 partial / 3 fail | **31 pass / 3 clarify / 6 OOS / 0 fail** |
| Tools native v2 | — | **35/35** (full Coverage/Warnings envelope) |
| Tests | 57 functional | 184 unit + 6 e2e + 25 all-tools audit |

### Components

- **scripts/v2/types.py** — `ToolResult` envelope (ok, data, coverage,
  warnings, source_info, runtime_ms)
- **scripts/v2/tool_registry.py** — `@tool` декоратор, `dispatch()`,
  cache wiring
- **scripts/v2/planner/intent.py** — 33-intent taxonomy, rules + priority
- **scripts/v2/planner/entities.py** — 90+ author aliases, 30 KNOWN_BOOKS,
  paired-quote regex для curly apostrophes
- **scripts/v2/planner/plan.py** — chain templates на каждый intent
- **scripts/v2/planner/router.py** — детерминированный executor
- **scripts/v2/planner/history.py** — multi-turn entity backfill + intent
  inference для follow-up phrases
- **scripts/v2/critic.py** — second-LLM fact-check pass
- **scripts/v2/cache.py** — disk + LRU cache, corpus_version-tagged
- **scripts/v2/observability.py** — JSONL request logs + aggregate rollup
- **scripts/v2/profiles/{author,book,lemma}.py** — SQLite profile cache
- **scripts/v2/tools/** — 35 native tool wrappers
- **scripts/v2/rag_v2.py** — ask() + ask_stream() entry
- **scripts/v2/build_fts_index.py** — one-time FTS5 builder
- **Modelfile.v2** — pinned Qwen3:14b с baked-in SYSTEM
- **systemd/wordcracker-chat.service.d/v2-engine.conf** — production
  deployment drop-in

### Live deployment

```bash
# Default engine = v2 через systemd drop-in:
sudo cat /etc/systemd/system/wordcracker-chat.service.d/v2-engine.conf
# [Service]
# ExecStart=
# ExecStart=/usr/bin/docker compose exec -T \
#   -e ASSISTANT_NAME=Словоёб \
#   -e WC_DEFAULT_ENGINE=v2 \
#   -e WC_LLM_MODEL=wordcracker:v2 \
#   -e WC_CRITIC_MODEL=wordcracker:v2 \
#   gutenberg-lab python -u /workspace/scripts/chat_server.py --port 8890

# v1 fallback per-request:
curl -X POST https://slovoeb.net/api/chat \
  -H 'X-WC-Engine: v1' \
  -d '{"question": "..."}'
```

### Полное описание

См. [docs/v2/RELEASE_NOTES.md](docs/v2/RELEASE_NOTES.md) и [docs/v2/PLAN.md](docs/v2/PLAN.md).

---

## Корпус

| Источник | Размер |
|---|---:|
| SPGC-2018-07-18 | 55 905 книг |
| Late-PD orphan additions (post-2018) | ~20k книг |
| User uploads | до 200 MB / upload |
| **Total raw text** | **55 101+ книг / 21+ GB** |
| ChromaDB index | 3.86M chunks (multilingual MiniLM-L12) |
| FTS5 lexical index | 55094 docs / 27 GB (BM25) |

## Стек

| Слой | Версия |
|---|---|
| Hardware | RTX 3090 24 GB on i9-13900H, 62 GB RAM |
| OS | Ubuntu 24.04 LTS, kernel 6.17 HWE |
| Docker | 29.5 + nvidia-container-toolkit 1.19 |
| Python | 3.11 (container) |
| PyTorch | 2.6 + cu124 |
| LLM | Ollama + custom `wordcracker:v2` (Qwen3:14b base, temp 0.1, num_ctx 8192) |
| Embedder | paraphrase-multilingual-MiniLM-L12-v2 (GPU) |
| ChromaDB | 1.5.9 |
| SQLite FTS5 | for lexical retrieval |

## Tests

```bash
# Unit + integration
python -m pytest tests/v2/

# Functional 40-question suite (live container)
python tests/v2/run_functional_40.py --engine v2 --base-url http://127.0.0.1:8890

# All-tools audit (one call per tool, 25 tools)
docker compose exec -T gutenberg-lab python -u /workspace/tests/v2/test_all_tools.py

# Performance bench
python tests/v2/bench_v2.py --runs 1 --out bench.md
```

## Roadmap

См. [wordcracker/wordcracker_v2_roadmap_next.md](https://github.com/Standaoerby/wordcracker) (Obsidian).

Текущий статус: **v2.0 stable**. Open items:
- Multi-model setup (separate planner / answer / reranker)
- Reranker (BGE-reranker-base) для hybrid_search top-30 → top-10
- Improved CEFR via POS/etymology heuristics

---

## История

| Tag | Дата | Что |
|---|---|---|
| **v2.0** | 2026-05-17 | Production release: planner+router pipeline, 40/40 functional |
| v2.0.6 | 2026-05-17 | Critic over-flag guard + RELEASE_NOTES |
| v2.0.5 | 2026-05-17 | Sprint 10 — UI badges (engine/intent/critic/copy) |
| v2.0.4 | 2026-05-17 | Sprint 9 — book_compare composite intent (Q24 closed, 40/40) |
| v2.0.3 | 2026-05-17 | Sprint 8 — learning_priority_score + Anki .apkg (genanki) |
| v2.0.2 | 2026-05-17 | Sprint 7 — LemmaProfile + AuthorProfile SQLite cache + observability |
| v2.0.1 | 2026-05-17 | Sprint 6 — critic pass + wordcracker:v2 Modelfile + conversation context |
| v1.1.7 | 2026-05-17 | (legacy) author_profile combo tool + bulk_enrich_propn |
| v1.0 | 2026-05-15 | Первый публичный релиз на slovoeb.net |

---

**License:** MIT.
**Контакт:** [github.com/Standaoerby/wordcracker](https://github.com/Standaoerby/wordcracker).
