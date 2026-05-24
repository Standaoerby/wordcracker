# wordcracker

NLP-аналитический сервис над корпусом Project Gutenberg (~55 000 книг).
Веб-чат с LLM-агентом, частотная аналитика, стилометрия, контексты, vocabulary
learning, hybrid retrieval (FTS5 + semantic). Хост — RTX 3090 / Ubuntu 24.04
в Docker.

**Live:** <https://slovoeb.net> (chat), <https://status.slovoeb.net> (health
dashboard), <https://admin.slovoeb.net> (upload новых книг).

---

## v2.3.1 (2026-05-18) — текущая версия

Архитектура переписана на детерминированный планировщик. LLM больше не
выбирает tools на каждой итерации — это делают rules-based intent classifier
(80+ правил) + entity extractor + plan templates. LLM остаётся только для
финального рендера и второго critic-прохода. Sprint 11 принёс speed wins
(60s→21ms на tokens aggregates) и Q40 composite_compare. v2.3 — adversarial
hardening + 5 routing fixes. v2.3.1 — Pushkin character-name leak +
re-rank followup + structured copyright OOS.

```
user → input caps (64KB / 4K chars / 50 turns) + control-char strip
     → intent classifier (35 intents, 80+ rules + injection guards)
     → entity extractor (90+ aliases, KNOWN_BOOKS, paired-quote regex)
     → history.merge (multi-turn entity backfill + intent inference,
                      now handles re-rank followups)
     → plan builder (33 templates, @_with_copyright_check decorator)
     → tool router (no LLM in loop; supports pg_id/scope/author_regex
                    inject for fan-out chains)
     → renderer (single LLM call, low temp, sees structured data only)
     → critic (second low-temp LLM pass; MAX_FLAGS=2 + per-intent skip)
     → answer + UI badges (engine / intent / critic / copy) + retry-with-scope
     → sticky stats footer (queries / avg / cache hit rate / critic flags)
```

### Что изменилось vs v1.1.7

| Аспект | v1.1.7 | v2.3.1 |
|---|---|---|
| Tool selection | LLM выбирает на каждой итерации (до 5 раз) | Детерминированный planner |
| Hallucination | Иногда выдумывал PG id (см. v1.0e issue) | Mandatory `find_book` chain + critic verify |
| Multi-turn | История терялась | `history.py` backfill + re-rank inheritance |
| Copyright books | Тула фейлила | Structured OOS: metadata-only + analog |
| Median latency | 30-50s (agentic loop) | **10-18s** (single render call) |
| Functional 40-question | 13 pass / 3 partial / 3 fail | **40/40 pass-like, 33 tool-driven** |
| Tools native v2 | — | **35/35** (full Coverage/Warnings envelope) |
| Tests | 57 functional | **206 unit + 18 caught-bug probes** |
| Heavy aggregate latency | `top_authors_by(tokens)` 60s+ | **21ms via pre-built JSON cache** |
| Adversarial input | unbounded | 64KB payload cap, 10× prompt-injection guards |

### Components

- **scripts/v2/_types.py** — `ToolResult` envelope (ok, data, coverage,
  warnings, source_info, runtime_ms)
- **scripts/v2/tool_registry.py** — `@tool` декоратор, `dispatch()`,
  cache wiring
- **scripts/v2/planner/intent.py** — 35-intent taxonomy, 80+ rules with
  priority/confidence + 10 prompt-injection guards
- **scripts/v2/planner/entities.py** — 90+ author aliases, KNOWN_BOOKS,
  paired-quote regex, `_NAME_AFTER_KEY` for «имени Анна»-style probes
- **scripts/v2/planner/plan.py** — 33 plan templates, `_with_copyright_check`
  decorator, `_HIGH_TRANSLIT_AUTHORS` allowlist for Russian PROPN floor
- **scripts/v2/planner/router.py** — детерминированный executor +
  `_inject` modes (pg_id / scope / author_regex)
- **scripts/v2/planner/history.py** — multi-turn entity backfill,
  intent inference, re-rank inheritance
- **scripts/v2/critic.py** — second-LLM fact-check pass (MAX_FLAGS=2,
  per-intent skip list for table-heavy intents)
- **scripts/v2/cache.py** — disk + LRU cache, corpus_version-tagged
- **scripts/v2/observability.py** — JSONL request logs + aggregate rollup
- **scripts/v2/profiles/{author,book,lemma}.py** — SQLite profile cache
- **scripts/v2/tools/** — 35 native tool wrappers
- **scripts/v2/rag_v2.py** — ask() + ask_stream() entry
- **scripts/v2/build_fts_index.py** — one-time FTS5 builder
- **scripts/v2/build_author_tokens.py** — pre-built JSON cache builder
  for `top_authors_by(metric=tokens)` (60s → 21ms)
- **scripts/chat_server.py** — HTTP entry, input caps, `/api/stats`,
  stats footer, retry-with-scope button
- **Modelfile.v2** — pinned Qwen3:14b с baked-in SYSTEM
- **systemd/wordcracker-chat.service** — production unit with
  `ExecStartPost` wait-for-/health (60s cap) so chained tooling
  doesn't fire requests during the ~12s ChromaDB warmup

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

## Pre-deploy gate (W-18)

Две проверки, гоняющиеся ПЕРЕД каждым деплоем — обе блокирующие.
Источник текстов проб и PASS/FAIL-критериев — внешний файл
`docs/test_external_claude_2026-05-22_error_taxonomy_probe_suite.md`.

1. **Mandatory version-bump** — `scripts/v2/__version__.ANALYTICS_VERSION`
   обязан отличаться от той версии, что висит в baseline / в предыдущем
   коммите. Закрывает класс «деплой состоялся, но строка версии не
   сдвинулась — пришлось проверять прогоном» (наблюдалось 2026-05-24).
2. **12-probe error-taxonomy suite** — по одной пробе на класс ошибки
   E1–E12 против живого `/api/chat`. Деплой блокируется при любом
   переходе **PASS→FAIL** относительно предыдущего baseline.

### Одна команда на деплой-хосте

```bash
# Полный гейт (bump + 12 проб vs baseline) — POSIX wrapper:
./scripts/predeploy_gate.sh

# Локальный таргет (контейнер на 127.0.0.1):
WC_PROBE_BASE_URL=http://127.0.0.1:8890 ./scripts/predeploy_gate.sh

# Только bump-проверка, без живых проб (CI или smoke):
./scripts/predeploy_gate.sh --no-probes

# Запечь текущий результат как новый baseline (wrapper передаёт флаг
# вниз; baseline записывается только если прогон чистый: 12/12 PASS,
# регрессий нет, версия бампнута):
./scripts/predeploy_gate.sh --update-baseline
```

### Отдельные шаги (для отладки / CI)

```bash
# (1) Bump-чекер — stdlib-only, не требует endpoint'а:
python scripts/check_version_bump.py --against baseline    # vs predeploy_baseline.json
python scripts/check_version_bump.py --against git --git-ref origin/main  # vs PR base
python scripts/check_version_bump.py --against file --file /tmp/prev_version.py

# (2) 12-probe runner — стучится в /api/chat:
python scripts/predeploy_probe_suite.py                                # прод
python scripts/predeploy_probe_suite.py --base-url http://127.0.0.1:8890
python scripts/predeploy_probe_suite.py --probes P12                   # subset
python scripts/predeploy_probe_suite.py --update-baseline              # запечь baseline
```

Сначала залей тексты проб и PASS/FAIL-условия из источника в
`scripts/predeploy_probes.json` (поля `question` со значением
`__FILL_FROM_SOURCE__` и пустые `pass_when` — это слоты для заполнения;
универсальные гарды в `universal_pass_when` уже работают).

### Exit codes (общий контракт двух инструментов)

| Код | Значение | Деплой |
|---:|---|---|
| 0 | все пробы PASS, версия бампнута, регрессий нет | OK |
| 1 | ошибка конфигурации (нет файла, кривой JSON, git недоступен) | блок |
| 2 | хотя бы одна регрессия PASS→FAIL vs baseline | **блок** |
| 3 | `ANALYTICS_VERSION` не сдвинулся (требование W-18) | **блок** |
| 4 | `/health` не поднялся за 60 с — таргет недоступен | **блок** |
| 5 | в `predeploy_probes.json` есть `__FILL_FROM_SOURCE__`-слоты | блок |

`scripts/predeploy_gate.sh` пробрасывает exit-код первого упавшего шага
наверх, так что вызывающий деплой-скрипт может матчить кодом, не разбирая
вывод.

### CI

`.github/workflows/predeploy.yml` запускает на каждом PR в `main`:

- **version-bump** — `check_version_bump.py --against git --git-ref origin/<base>`;
  PR без бампа `ANALYTICS_VERSION` фейлит CI.
- **probe-config sanity** — юнит-тесты раннера + конфиг 12 проб должен
  быть валидным (12 проб, без дубликатов P-ID, `repeat=3` на P12).
- **test-collect** — `pytest tests/v2 --collect-only` должен пройти без
  ошибок коллекции (R10).

Live 12-probe прогон — на деплой-хосте через `predeploy_gate.sh`: у CI
runner'а нет доступа к чат-API.

### Baseline

`scripts/predeploy_baseline.json` появляется после первого зелёного
прогона с `--update-baseline`. Структура:

```json
{
  "version": "2.6.13",
  "recorded_at": "2026-05-24T12:00:00+00:00",
  "verdicts": {"P1": "PASS", "P2": "PASS", "...": "..."}
}
```

Дальнейшие прогоны сверяются с этим snapshot'ом — pre-deploy ловит
ровно момент, когда что-то, работавшее на прошлой версии, сломалось на
этой. Без baseline'а первый деплой пропускается с предупреждением.

## Roadmap

См. [wordcracker/wordcracker_v2_roadmap_next.md](https://github.com/Standaoerby/wordcracker) (Obsidian).

Текущий статус: **v2.6.11 stable** (W-11/W-12 follow-up: POS+period intents + words_appearing acceptance, 2026-05-24).
Open items:
- `build_country_affinity.py` — настоящий per-corpus lemma diff для Q40-стиля (сейчас approximation через top-leader-per-country)
- `find_words_by_etymology` family caches (Wiktionary top-50 words per family)
- Cache warm-up на restart (pre-load top-10 author profiles)
- Reranker (BGE-reranker-base) для hybrid_search top-30 → top-10
- Multi-model setup (separate planner / answer / reranker) — ROI unclear
- Rate limiting per-IP + cost throttling на heavy tools (nginx layer)

---

## История

| Tag | Дата | Что |
|---|---|---|
| **v2.3.1** | **2026-05-18** | Stan round 2: Pushkin PROPN floor 1500, «отсортируй» re-rank followup, structured copyright OOS |
| v2.3 | 2026-05-18 | Adversarial hardening (input caps, 10× injection guards) + 5 routing fixes (Q10/Q15/Q21/Q30/meta-copyright) + decorator refactor |
| v2.2.2 | 2026-05-18 | systemd ExecStartPost wait-for-/health + runner self-wait (race fix) |
| v2.2.1 | 2026-05-18 | `_NAME_AFTER_KEY` hotfix (re.IGNORECASE disabled the proper-noun guard) |
| v2.2 | 2026-05-18 | Sprint 11.4 composite_compare (Q40) + 11.5 stats footer + retry-with-scope + 2 caught bugs |
| v2.1 | 2026-05-17 | Sprint 11.1+11.2+11.3 — critic precision (23→9 flags), author_tokens cache (60s→21ms), multi-author parallel |
| v2.0 → v2.0.7 | 2026-05-17 | Production v2 release + Sprint 6-10 + 3 caught bugs |
| v1.0 → v1.1.7 | 2026-05-15..17 | (legacy) v1 agentic loop releases |

---

**License:** MIT.
**Контакт:** [github.com/Standaoerby/wordcracker](https://github.com/Standaoerby/wordcracker).
