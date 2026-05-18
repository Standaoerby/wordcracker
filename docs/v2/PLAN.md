# wordcracker v2 — план разработки

> Long-lived branch: `v2-refactor`. Merge в `main` целиком по достижении critical-acceptance.
> Базовый источник требований: `wordcracker/wordcracker_v2_roadmap.md` в Obsidian vault.
> Baseline: v1.1.7 (32 tools, 19/20 «Примеры», 13/20 functional pass).

---

## Главная цель

Поднять качество ответов и устойчивость **без переписывания корпусной аналитики**, обвязав её:

```
planner → router → tool result → renderer
```

Не «новая система». Рефакторинг поверх работающего ядра (Ollama / ChromaDB / SPGC / 32 tools / nginx+CF).

---

## Acceptance criteria для merge v2 → main

- [x] **Sprint 2 acceptance** — Query Planner покрывает ≥80% «Примеры запросов чату» — **39/40 = 98%** реально (retest 2026-05-17).
- [x] **Sprint 2 acceptance** — Q02/Q03/Q05 v1 timeouts устранены: Q02 pass 14s, Q03 pass 14s, Q05 partial из-за corpus data (не код).
- [x] **Sprint 2 acceptance** — Tool router логирует план в `tool_trace` — `/api/chat/stream` отдаёт `intent`/`plan`/`tool_call`/`tool_result` события.
- [x] **Sprint 1 acceptance** — каркас `ToolResult` + Registry + FilterSpec + Metadata adapter работают (5 v2 tools + 27 v1 через `legacy_dispatch`).
- [ ] Все 32 tools мигрированы в native v2 (5/32 на момент v2.0-alpha1).
- [ ] FTS5 lexical index построен и подключён в `semantic_search` через hybrid merge — **Sprint 3**.
- [ ] `BookProfile` / `AuthorProfile` доступны как ленивые caches — **Sprint 4**.
- [ ] Cache layer для etymology / enrich_word / profiles на disk + in-process — **Sprint 1.7** (deferred).
- [x] Sprint 2-test reports: `tests/v2/run_functional_40.py` зелёный (98%).

---

## Sprint 1 — Foundations (параллельно с Sprint 2)

**Цель:** дать всем tools единый контракт, не ломая поведения. Старый `TOOL_DISPATCH` = адаптер поверх нового registry.

### 1.1 ToolResult envelope
- `scripts/v2/_types.py`: dataclasses `ToolResult`, `ToolWarning`, `Coverage`, `SourceInfo`. (Renamed from `types.py` early on to avoid shadowing Python's stdlib `types` module — broke ImportError in `build_fts_index` until the leading underscore went in.)
- `to_dict()` сериализатор → совместим с текущим `messages[].content = json.dumps(result)`.
- `from_legacy(dict)` — wraps old `{...data..., "error": ...}` returns в новую форму. Используется для tools, ещё не отрефакторенных.

### 1.2 Tool Registry
- `scripts/v2/tool_registry.py`: `@tool(name, category, inputs, requires, cost)` декоратор.
- Глобальный `REGISTRY: dict[str, ToolSpec]`.
- `build_tools_spec()` → OpenAI/Ollama schema для current iteration.
- `dispatch(name, args) -> ToolResult` — auto-wraps + timing + try/except.

### 1.3 FilterSpec
- `scripts/v2/filters.py`: dataclass `FilterSpec(author_regex, lang, country, year_from, year_to, min_corpus_count, exclude_proper_nouns, pos_filter)`.
- `apply_to_metadata_df()` — заменяет `_select_books()` логику в rag_tools.
- Backward-compat: `FilterSpec.from_legacy_kwargs(**kwargs)` для постепенной миграции.

### 1.4 Metadata Resolver
- `scripts/v2/metadata.py`: `resolve_book(query) -> CanonicalBook` (PG id / U id, canonical title, author, year, lang, available_tools_coverage).
- Использует существующий `find_book` под капотом, но возвращает обогащённый record + coverage flags (`has_tokens`, `has_counts`, `has_pub_year`, `has_nationality`).
- Cache в memory с TTL.

### 1.5 Corpus Versioning
- `scripts/v2/corpus_version.py`: читает `/workspace/spgc/derived/corpus_meta.json` + counts dirs → возвращает `CorpusVersion`.
- Сохраняется в каждом `ToolResult.source_info`.
- `/api/corpus/version` endpoint для UI и tests.

### 1.6 Pilot migration
- Перевести в новый формат **4 simple tools** для проверки контракта:
  1. `corpus_overview` (нет args)
  2. `find_book` (string lookup)
  3. `author_metadata` (single regex)
  4. `top_authors_by` (single enum metric)
- Старый dispatch вызывает adapter → new tool. Тесты A3 (количество tools) обновляются.

**Deliverables Sprint 1:**
- `scripts/v2/{types,tool_registry,filters,metadata,corpus_version}.py`
- `scripts/v2/tools/{corpus,books,authors}/*.py` (pilot tools)
- `tests/v2/test_tool_contract.py` — schema validation для всех migrated tools
- `tests/v2/test_filters.py` — FilterSpec.apply correctness
- ADR: `docs/v2/adr/001-tool-result.md`, `002-filter-spec.md`, `003-metadata-resolver.md`

---

## Sprint 2 — Planner + Router (параллельно с Sprint 1)

**Цель:** убрать failure modes из v1.1.7 теста (Q02/Q03/Q05 timeouts, Q11 plan-without-tool).

### 2.1 Intent Classifier
- `scripts/v2/planner/intent.py`: rules-based first (regex/keyword → intent label), LLM fallback для ambiguous.
- Intent taxonomy:
  - `author_vocab` (фирменные слова автора)
  - `book_vocab` (фирменные слова книги)
  - `author_compare`
  - `word_contexts` / `word_collocates` / `word_timeline` / `word_pos` / `word_etymology`
  - `book_readability` / `book_archaic`
  - `corpus_meta` (сколько книг / прогресс)
  - `learning` (lookup для изучения)
  - `emotion_analysis`
  - `attribution` / `influences`
  - `out_of_scope` (написать рассказ, личная история)
- Confidence scoring: HIGH (rules-only match), MEDIUM (LLM-assisted), LOW (clarify).

### 2.2 Entity Extractor
- `scripts/v2/planner/entities.py`: вытаскивает {author, book, word, year_from, year_to, country, level}.
- Источники: regex (явный PG id, явный «^Wodehouse,»), known-name dictionary, LLM fallback.
- Резолвинг авторов через canonical list (`metadata_df()` unique authors → fuzzy match).

### 2.3 Plan Builder
- `scripts/v2/planner/plan.py`: intent + entities → `QueryPlan(steps: list[PlanStep], fallback, expected_cost)`.
- Известные chains:
  - `author_vocab` → `[find_book if title given] → affinity_by_author(pos_filter=..., min_corpus_count=auto)`
  - `book_vocab` → `find_book → affinity_by_book`
  - `emotion_analysis` → `[find_book?] → emotion_collocates(emotion, scope)`
- Cost estimation: `cheap` (<3s), `medium` (<15s), `heavy` (>15s).
- Heavy planов с unverified entities → ASK clarifications до запуска.

### 2.4 Tool Router
- `scripts/v2/planner/router.py`: executes `QueryPlan` step-by-step, populates `ToolResult` chain.
- Принудительно вызывает `find_book` перед book-dependent tools (anti-hallucination rule 6 теперь enforced кодом, не prompt-ом).
- Ограничивает iterations (max 3 для simple, 7 для heavy).
- Возвращает structured trace.

### 2.5 LLM Adapter
- `scripts/v2/planner/llm_adapter.py`: тонкая обёртка над ollama /api/chat, **используется только для clarify / renderer / out-of-scope detection**, не для tool selection. Tool calls приходят от router, не от LLM.
- Сохраняет совместимость с current `rag_query.ask_stream()` API: всё та же SSE event sequence.

**Deliverables Sprint 2:**
- `scripts/v2/planner/{intent,entities,plan,router,llm_adapter}.py`
- `scripts/v2/rag_v2.py` — новый `ask()` entry с feature flag (`?engine=v2`)
- `tests/v2/test_intent.py` — 40 примеров → intent label
- `tests/v2/test_plan.py` — для каждого intent ожидаемый chain
- `tests/v2/test_routing.py` — end-to-end Q01-Q20 через v2 engine
- ADR: `004-planner-architecture.md`, `005-router-vs-llm.md`

---

## Sprint 3 — Hybrid Retrieval (после Sprint 1)

**Цель:** улучшить `semantic_search` за счёт lexical index + reranker.

### 3.1 FTS5 build
- `scripts/v2/build_fts_index.py`: SQLite FTS5 поверх `/workspace/raw_text/*.txt` (BM25).
- Ожидаемый размер: ~2-3 GB. Время билда: ~30 мин.
- Сохраняет offsets для citation lookup.

### 3.2 Hybrid Search
- `scripts/v2/tools/search/hybrid_search.py`: semantic (top-200) + lexical (top-200) → score merge (RRF) → top-20.
- Опция `mode=lexical|semantic|hybrid`, default `hybrid`.

### 3.3 Reranker
- `scripts/v2/reranker.py`: cross-encoder (`BAAI/bge-reranker-base` или `cross-encoder/ms-marco-MiniLM-L-6-v2`).
- Inference на GPU, batch 32.
- Опциональный (ENV `WC_RERANKER=on`).

**Deliverables Sprint 3:**
- `scripts/v2/build_fts_index.py`, `scripts/v2/tools/search/hybrid_search.py`, `scripts/v2/reranker.py`
- `/data/fts5_index.db`
- ADR: `006-fts5.md`, `007-hybrid-rrf.md`

---

## Sprint 4 — Profiles (после Sprint 1)

**Цель:** единые таблицы Lemma/Author/Book, ленивые builds, переиспользование в planner.

### 4.1 LemmaProfile
- `scripts/v2/profiles/lemma.py`: `LemmaProfile(lemma, pos, global_count, book_count, author_count, rarity_score, difficulty)`.
- Build: scan corpus_counts.csv + per-book counts → CSV.
- Tool: `lemma_profile(word) -> LemmaProfile`.

### 4.2 AuthorProfile
- `scripts/v2/profiles/author.py`: dataclass `AuthorProfile(books, tokens, vocab_size, lexical_diversity, signature, closest_authors, country, active_period)`.
- Wraps `author_profile()` v1.1.7 + caches result в SQLite.
- Tool: `author_profile_v2(author_regex)` (postфикс _v2 до миграции).

### 4.3 BookProfile
- `scripts/v2/profiles/book.py`: `BookProfile(pg_id, title, author, lang, year, readability, cefr, signature_words, archaic_words)`.
- Build on-demand, cache.
- Tool: `book_profile(pg_id)`.

**Deliverables Sprint 4:**
- `scripts/v2/profiles/{lemma,author,book}.py`
- `/data/profiles/{authors,books,lemmas}.sqlite`
- `tests/v2/test_profiles.py`

---

## Sprint 5 — Learning v2 (после Sprint 4)

**Цель:** «слова второго уровня» с учётом профилей.

### 5.1 Learning Priority Score
```
learning_score = author_affinity * 0.30
               + book_frequency * 0.20
               + rarity * 0.20
               + CEFR_relevance * 0.15
               + context_quality * 0.10
               + non_proper_noun_confidence * 0.05
```

### 5.2 Improved CEFR/Difficulty
- Use LemmaProfile.rarity + corpus_count percentile + length + Anglo-Saxon vs Latin origin heuristic.

### 5.3 Better Export
- Anki Deck (`.apkg`, не CSV) via `genanki`.
- Markdown deck в Obsidian-compatible формате.

**Deliverables Sprint 5:**
- `scripts/v2/tools/learning/{score,cefr,export}.py`
- `tests/v2/test_learning_score.py`

---

## Cross-cutting

### Cache Layer (накопительно через все sprints)
- `scripts/v2/cache.py`: file-based + LRU in-memory.
- Cacheable: etymology, enrich_word, plans, profiles, heavy tool results, collocations, timelines.
- Cache key: `(tool_name, sorted(args))` + corpus_version.

### Observability
- `scripts/v2/observability.py`: structured logs (JSONL) в `/data/logs/v2/`.
- Fields: timestamp, request_id, intent, plan_steps, tool_name, runtime_ms, rows_returned, warnings, fallback_used, llm_tokens, answer_confidence.
- Dashboard tab в `status_server.py`: slow tools, cache hit rate, intent distribution.

### Tests
- `tests/v2/` mirrors `scripts/v2/` layout.
- Levels:
  - `unit_*.py` — deterministic, no LLM, fast (<10s total).
  - `integration_*.py` — hit real chroma + sqlite (no LLM), <60s total.
  - `llm_behavior_*.py` — actual qwen3, slow (>5min total), gated on `RUN_LLM_TESTS=1`.
- Регрессионный suite v1: оставляем `tests/run_tests.py` нетронутым до merge.

### Migration policy
- Каждый перенесённый tool оставляет shim в `rag_tools.py` (deprecated alias).
- Удаляем shims одним коммитом после merge v2 → main.
- Старый `rag_query.py::ask` остаётся как `?engine=v1` пока v2 не валидирован 1 неделю.

---

## Timeline (приблизительный)

| Week | Sprint | Status |
|---|---|---|
| 1 | S1 foundations + S2 intent classifier prototype | начинаем сейчас |
| 2 | S1 pilot migration + S2 plan builder + router | |
| 3 | S2 e2e wire-up + functional retest v2 | first internal demo |
| 4 | S3 FTS5 + hybrid | |
| 5 | S4 profiles + S5 learning score | |
| 6 | Validation, cleanup, ADR finalization, merge | |

---

## Принцип «не делать»

Согласовано с roadmap §4.2 и §15:
- Не переписывать ChromaDB / Ollama / nginx / CF.
- Не вводить web framework (стек stdlib HTTP пока работает).
- Не делать user accounts / SRS / mobile (вне scope, см. backlog «НЕ планируется»).
- Не плодить новые tools без необходимости — лучше унифицировать существующие.
