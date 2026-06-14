---
project: wordcracker
type: audit-result
date: 2026-06-14
status: review-pending (code-verified findings; awaits Stan go)
prod: 2.7.14 / fbb7992
audit_branch: audit/s1-s5-findings-2026-06-14 (off origin/main == fbb7992)
mode: safe-max (discovery + spec + xfail-pins; NO contract implementation)
source: AUDIT_2026-06-14_pipeline-systemic (S1–S5 hypotheses), NIGHT_RUN_2026-06-14
---

# S1–S5 — результат code-сверки (статика на клоне 2.7.14)

Ответ на «## Задание Code-сессии» из `AUDIT_2026-06-14_pipeline-systemic`.
Каждый структурный корень **подтверждён/опровергнут по живому коду, с file:line**.
Для подтверждённых — спека ремедиации (инвариант, файлы/функции, размер, blast-radius,
риск фикстур/рестампа) + **xfail-характеризующий тест-пин** (зелёная цель фикса).

> **Режим safe-max.** Контракты S1/S2/S3 НЕ имплементированы. Тесты — чистое ДОБАВЛЕНИЕ
> (`tests/v2/test_audit_s1_s5_characterization.py`): golden-фикстуры/манифест/корпус не тронуты,
> рестамп не требуется. PR ждёт ревью Стэна; мерж/деплой — не в этой сессии.

## R-CLEAN-START
- Клон `C:/dev/wordcracker`: рабочее дерево чистое, открытых PR нет → гейт пройден.
- Клон стоял на оставленной ветке `r29-wp45-glue` (`1f7a0c9`) — это **дубликат уже
  смерженного** PR #51 (та же правка «R-29 WP4+WP5», уехала в main как `fbb7992`),
  НЕ чужие незакоммиченные правки. Аудит отведён свежей веткой от `origin/main` (= prod 2.7.14),
  чтобы PR базировался чисто и не тащил дубликат.

## Сводка вердиктов

| Корень | Гипотеза | Вердикт | Главный пруф | Размер фикса | xfail-пин |
|---|---|---|---|---|---|
| **S1** | scope не первоклассен; book→author | **ПОДТВ.** (+2 фактические правки) | `_plan_author_top_words` игнорит `book_id` (author.py:162-189) | **S→M** (planner-only) / L (новый book-freq тул) | ✅ ×2 |
| **S2** | критик грундится ad-hoc; surface≠lemma | (a) **PARTIAL** / (b) **ПОДТВ.** | substring без леммы (critic.py:219-249) | **M** | ✅ (lemma-half) |
| **S3** | рендер «сочини, потом проверь» | **ПОДТВ.** | free-form LLM render, honesty post-hoc (rag_v2.py:741, 831-838, 1561-1601) | **L** (ре-арх.) | ⏸ deferred |
| **S4** | inter-step контракт строковый | (a) **ПОДТВ.** / (b) **PARTIAL** | `row.get("id")` без схемы (router.py:92-114) | **M** / M-L (dynamic fan-out) | ✅ |
| **S5** | дрейф = шум; dev↔prod skew | (a) **ПОДТВ.** / (b) **ПОДТВ.** (+правка) | epoch в фикстуре (export_word_list.json:4) | **S** (hygiene) | ✅ |

Все 5 пинов сейчас **xfailed** (падают на текущем коде, как и должны) — прогон ниже, §CI.

---

## S1 — Scope не первоклассен → **ПОДТВ.** (с двумя фактическими поправками гипотезы)

**Что подтверждено по коду.** Scope — НЕ типизированный резолвимый слот. Датакласс
`Entities` (`scripts/v2/planner/entities.py:565-613`) держит плоские `author_regex`,
`book_id`, `book_title`, `multi_*` — и **нет поля `scope`** (`book|author|corpus|period`).
Scope выводится неявно: (1) какой intent выбрал `classify()`, (2) поздняя ad-hoc деривация
`_scope_from` (`builders/_common.py:417`: `if e.book_id: return {"book": ...}` else
`{"author": ...}`). Системный промпт прямо просит LLM писать `scope` руками per-step
(`tool_catalog.py:169`) — то есть scope это LLM-авторский арг, не валидируемый слот.

**Трасса bug A («слова из [книга]» → author-агрегат), 2 ступени:**
- **Intent.** Частотные формулировки матчатся ТОЛЬКО в `author_top_words`:
  `intent.py:958` `сам[оы]е\s+част[оы]тн\w+\s+слов`, `:960` `most\s+frequent\s+words?`,
  n-gram `:965-969`. **Ни одного book-scoped частотного правила.**
- **Builder.** `_plan_author_top_words` (`builders/author.py:162-189`):
  - `:171-172` — `if not e.author_regex: return _need_author(e)` → книга **молча выброшена**,
    спрашивает автора.
  - `:183-186` — иначе `top_ngrams_by_author(author_regex=...)`, **`e.book_id` игнорируется** →
    author-агрегат по ~10 книгам под видом «частотных слов книги».
  - Контраст: сосед `_plan_author_vocab` (`author.py:200-201`) ИМЕЕТ ровно тот book-fallback,
    которого нет здесь: `if not e.author_regex and (e.book_id or e.book_title):
    return _plan_book_vocab(e)`. Асимметрия и есть шов bug A.

**Поправка №1 к гипотезе (REFUTED-часть).** «Book-scope прибит сбоку только для learning» —
**неверно**: существует полноценный book-scoped сигнатурный тул `affinity_by_book(pg_id, ...)`
(`scripts/v2/tools/books/affinity_book.py:27,62`, описание «Фирменные слова конкретной книги по
affinity»), доступный через intent `book_vocab`. Паритет с author-версией (toponym/PROPN-фильтры).

**Поправка №2 (точная формулировка дыры).** Чего реально НЕТ — book-scoped **raw-frequency /
n-gram** тула (`top_ngrams_by_book` отсутствует; `word_freq_timeline` это word+period,
`tools/words/timeline.py:50` `required:["word"]`). То есть «частотности книги нет» верно ТОЛЬКО
про сырую частоту/n-gram, не про affinity.

**Инвариант-убийца.** Scope — явный резолвимый слот; контракт «book НИКОГДА молча не → author».
Минимально: `_plan_author_top_words` становится book-aware (как сосед).

**Спека ремедиации:**
- **Файлы/функции:** `builders/author.py::_plan_author_top_words` (добавить book-ветку);
  `entities.py::Entities` (опц. `scope_kind`); `intent.py` (freq-правила/priority `:160-178`);
  `plan.py::PLAN_BUILDERS` (`:125`) + `builders/_common.py::_scope_from` (`:417`);
  `tool_catalog.py`/`llm_intent.py` (few-shots) — если добавляется тул.
- **Размер:**
  - **S** — только сделать `_plan_author_top_words` book-aware → роутить на `affinity_by_book`
    (как `_plan_author_vocab`). Planner-only, без корпуса. Гасит book→author-коллапс.
  - **M** — добавить честную book-raw-frequency (новый тул) для семантики «самые частотные слова
    книги».
  - **L** — сделать scope первоклассным слотом, протянутым через extractor→intent→builders.
- **Blast-radius:** S/M — planner-only (фикстуры не трогаются). Новый v1-bound тул → нужен
  contract-schema (`contracts/schemas.py`) + golden-фикстура (**re-record**) — это часть L и
  ТРОГАЕТ фикстуры (вне safe-max).
- **Риск фикстур/рестампа:** LOW для planner-only; MEDIUM/HIGH при новом v1-тулe.
- **Пины:** `test_s1_book_frequency_does_not_collapse_to_author_aggregate`,
  `test_s1_book_frequency_without_author_keeps_the_book`.

---

## S2 — Критик грундится ad-hoc → (a) **PARTIAL** / (b) **ПОДТВ.**

**(a) trusted-set per-intent vs единый — PARTIAL (дефект подтверждён, но механизм иной).**
Единого grounding-контракта нет. Trusted-set собирается инлайн в **ДВУХ дублирующихся местах**
(`rag_v2.py:1539-1556` — `ask()`; `rag_v2.py:2155-2169` — `ask_stream()`, держатся в синхроне
руками), каждое строит набор только из `r.results` текущего хода. Дальше доказательства
**пере-выводятся per-guard**: `filter_claims_with_data_evidence` (`critic.py:219`),
`_records_carry_name_filter_evidence` (`critic.py:509`), `_records_carry_etymology_evidence`
(`critic.py:764`), `_tool_data_blob` (`critic.py:860`), `numeric_audit.collect_data_numbers`
(`numeric_audit.py:275`). **6+ независимых харвестов, 0 общего объекта.**
- Буквального «`if intent ==` который добавляет источники» НЕТ. Per-intent связь обратная:
  `_INTENT_SKIP_CRITIC` (`critic.py:259`, применяется `:307`) — набор интентов, которые **целиком
  выключают grounding** (антипаттерн «волки-волки» лечится опт-аутом интента).
- **Ключевой пробел:** ни одно место сборки не подмешивает **carried/prior-turn контекст**, хотя
  `history.merge_with_history` (`planner/history.py:760-816`) тащит слова предыдущего хода вперёд.
  Перенесённое слово не в trusted-set, если тул не переэмитит его в этом ходу.

**(b) surface-match без леммы — ПОДТВ.** Критик делает только lowercased substring/set-membership
(`critic.py:212,240,245,890`); **лемматизатор не импортируется нигде в пути критика**.
`_claim_entity_candidates` (`critic.py:202-216`) извлекает кандидатов только из кавычек / PG-id /
2+-словных Latin TitleCase — голое `said` даёт **ноль кандидатов** → ветка «kept» (`:247-248`).
Даже в кавычках «said»: blob держит лемму `say`, не surface `said` → substring-промах → ложный
флаг выживает. `profiles/lemma.py` / `tools/words/lemma_profile.py` — это corpus-stats lookup
(SQLite), к критику не подключён. **Это и есть механизм «said» в bug B.**

**Инвариант-убийца.** ОДИН grounding-контракт: trusted-set = авто-полный набор
`{tool-выходы хода + carried-контекст}` (не per-guard, не per-intent) + **лемма-матч**.
Ретайрит R-INTERCEPT-CARRIES-ALL как енфорсимый инвариант.

**Спека ремедиации:**
- **Файлы/функции:** свернуть `rag_v2.py:1539` и `:2155` в общий `build_grounding_context(results,
  history)` (+ добавить carried-источник из `history.py`); прогнать 4 харвеста критика
  (`critic.py:219/509/764/860`) и `numeric_audit.py:275` через этот объект; добавить
  surface↔lemma индекс; пересмотреть `_INTENT_SKIP_CRITIC` (`:259`) после.
- **Размер:** **M** (унификация 2 сборок + лемма-индекс). ⚠️ `profiles/lemma.py` — это частотность,
  НЕ инфлекционный лемматизатор; для `said→say` может понадобиться реальный лемматизатор (новая
  зависимость или spaCy, если уже в lock).
- **Blast-radius:** критик + render-summary плумбинг. Тесты, пиннящие точные kept/suppressed
  (`test_critic.py`, `test_r28_learning_polish.py::B119*`), **перевернутся → re-record**; golden-футеры
  ответов (`⚠️ Проверь` / `🔧 Honesty`) сдвинутся. **v1-contract фикстуры НЕ трогаются.**
- **Риск фикстур/рестампа:** MEDIUM (test-голдены критика/футеров, не v1-контракты).
- **Пин:** `test_s2_inflected_surface_grounded_by_lemma_is_suppressed` (лемма-половина = bug B).
  Carried-context половину на unit-уровне не пиннуть (функция берёт только `tool_records`) — нужен
  общий-контекст шов; помечено как зависимость.

---

## S3 — Рендер «сначала сочини, потом проверь» → **ПОДТВ.**

Живой путь рендера — единственный free-form LLM-вызов: `_dispatch_render` → `_llm_render`
(`rag_v2.py:741`), который получает **сырой `question` + tool `data` вместе**
(`rag_v2.py:831-838`: system=RENDER_PROMPT, user=question, user=«Tool data» JSON). Honesty
ловится 100% **после** рендера (`rag_v2.py:1561-1601`: `strip_service_lines`,
`audit_operation_claims`, `audit_claimed_vs_shown`, critic `review`, `audit_numbers`).
RENDER_PROMPT (`rag_v2.py:50-138`) сам признаёт пост-фактум: правило 14 «Numeric audit и critic
поймают… но это пост-фактум, и пользователь уже прочитал кривой ответ».

**Pre-render гейта НЕТ.** Структурный «render-from-evidence» путь построен, но **МЁРТВ**:
`template_executor.render_view` (`template_executor.py:841`) не имеет прод-вызова (только тесты);
v5-типизированный рендер-бранч удалён (`rag_v2.py:1095-1097`); `_types.py:14-16` — «Phase 3 never
shipped». Scope-лейбл author-агрегатных тулов НЕ доезжает до рендера: payload проецирует только
`tool/query/data/warnings/coverage/ok/error` (`rag_v2.py:785-799`) — **никогда `r.view`** (несущий
scope-headline). Значит LLM выводит scope из формулировки юзера → конфляция bug A возможна.
`render_sanitizer.py` — пост-рендер line-scrub утёкших служебных строк, НЕ верификатор фактов/scope.

**Инвариант-убийца.** Рендер-из-доказательств: утверждать только слоты `tool_results`
(scope/числа/клеймы); структурный рендер verified-слотов (вкл. `scope_kind`/`scope_label`),
LLM — только проза; критик → бэкстоп.

**Спека ремедиации:**
- **Файлы:** `rag_v2.py::_dispatch_render/_llm_render` (восстановить структурный бранч; `summary_payload`
  передаёт verified-слоты); `template_executor.py::render_view` (нужен прод-вызов + prose-binding шов);
  `view_types.py`/`view_builders.py` (добавить scope-слот в `RenderableView` + фабрики);
  ~18 тулов-эмиттеров view; `critic.py`/`numeric_audit.py` (сдвиг на diff-против-слотов).
- **Размер:** **L** (ре-архитектура). **Blast-radius: большой** — ~3 ядровых рендер-файла + ~18 тулов +
  honesty-вертикаль; меняется streaming token-контракт.
- **Риск фикстур/рестампа:** **HIGH** — каждый golden-ответ записан против free-form prose;
  переход на детерминированные скелеты меняет почти все ожидаемые строки.
- **Трек:** отдельный, ПОСЛЕ S1/S2 (совпадает с рычагом аудита). ⚠️ **N2 ниже:** скелет уже наполовину
  построен (мёртвый структурный путь) — это форточка для сайзинга S3.
- **Пин:** **отложен.** Детерминированного unit-шва нет — единственный шов это инлайн-ассемблер
  payload в `_llm_render`, и его вынос **сам является частью фикса** S3. Характеризацию шипим ВМЕСТЕ
  с этим треком, не раньше. (Подтверждение — статикой выше, file:line.)

---

## S4 — Inter-step контракт строковый → (a) **ПОДТВ.** / (b) **PARTIAL**

Живой fan-out — v3-путь `router._inject` (`scripts/v2/planner/router.py:65-152`). (NB: `plan_spec.py`
это отдельный v4 `$sN.field` DAG-путь, не там, где живут B120/B121.)

**(a) строковый ключ без схемы — ПОДТВ.** `inject_result_as` = строка `"pg_id@<rank>"`, парсится
`split("@")` (`:104`); rows-ключ `src.data.get("top")` (`:107`) и row-value `row.get("id")` (`:110`)
оба **хардкод-литералы**; на промахе `return None` (`:114`). `router.py` **не импортирует**
`contracts`. B120-фикс = **точечный**: убран фантом `pg_id` из `V1TopBooksByDownloads.__row_keys__`
(`schemas.py:566-572`) + литерал `id` в router; **контракт producer-ключ ↔ consumer-ключ не введён**.
Вторая хардкод-копия `_RANK_SOURCE_KEYS` (`router.py:157`) дублирует чтение `_inject` и держится
в синхроне руками (см. N1).

**(b) ширина fan-out = мощность источника — PARTIAL.** Ширина N всё ещё фиксируется планом на
build-time: `for rank in range(_LEARNING_BOOKS_POOL)` (=10, `builders/learning.py:189`),
word@ `range(trans_top)` (`:106`). B121 НЕ сделал N=мощность; вместо этого добавил **runtime-skip**:
`_inject` → None (`:114`) → `_skip_for_empty_injection` (`router.py:160`) кладёт placeholder +
`ToolWarning(code="inject_shortfall")` + render-note «SHORTFALL ИСТОЧНИКА … top_requested/top_returned»
(`:188-195`), событие `step_skip`. Перезаполнение больше не диспатчит `{}`, но план over-declares N
и полагается на пост-хок подрезку → ширина ≠ мощность.

**Инвариант-убийца.** Схема-контракт ключей шагов (producer-выход ↔ consumer-вход, registry-driven) +
ширина fan-out = факт. мощность источника.

**Спека ремедиации:**
- **Файлы:** `router.py::_inject` (`:65-152`) — заменить хардкод `row.get("id")`/`get("top")` на
  registry-lookup по схеме producer-тула; **громкая ошибка** на «ключ не в схеме producer» вместо
  тихого None; `_RANK_SOURCE_KEYS` (`:157`) свернуть в тот же источник; `builders/learning.py`
  (`:189`,`:106`) — ширина из мощности → нужен executor-level dynamic-fanout примитив; `schemas.py`
  `__row_keys__` становятся load-bearing в рантайме (+ `output_id_key`/`expects_arg`);
  `rag_v2.py:2016-2041` — 3-е место вызова `_inject`.
- **Размер:** **M** (схема-контракт ключей) / **M-L** (истинный dynamic fan-out — новый executor-примитив).
- **Blast-radius:** router + learning-builder + contracts + rag_v2 stream-loop (3 места вызова).
- **Риск фикстур/рестампа:** **LOW** — `test_r28_inject_fix.py` гоняет реальные builders+golden, фикс их
  УСИЛИТ; golden не меняются, если не переименовывать producer-ключ; следить за asserts строки
  shortfall-ноута (`test_r28_inject_fix.py:149-151`).
- **Пин:** `test_s4_wrongkey_injection_distinguishable_from_empty_source` (пиннит B120-класс: «строка
  есть, ключ не тот» молча == «источник пуст», оба None).

---

## S5 — Дрейф = шум; dev↔prod skew → (a) **ПОДТВ.** / (b) **ПОДТВ.** (+поправка к гипотезе)

**(a) недетерминированный timestamp в фикстуре — ПОДТВ.** Ровно одна фикстура несёт wall-clock:
`scripts/v2/contracts/fixtures/scripts.learning_tools.export_word_list.json:4`
`"out_path": "/workspace/spgc/derived/export_1781202786.csv"`, источник
`scripts/learning_tools.py:881` `int(time.time())`. Реплей shape-only (CI не валит), но значение
churn-ится на каждом re-record → вечный **ADVISORY-дрейф** на deploy F2-RERECORD git-diff гейте.
Рекордер стрипает только `_elapsed_s` (`record_fixtures.py:85` `_VOLATILE_BODY_KEYS`), не `out_path`.
Исчерпывающий скан: это **единственный** такой epoch во всём наборе фикстур.

**(b) env-зависимый fingerprint — ПОДТВ., уже сведён к ОДНОМУ громкому сигналу.**
`v1_fingerprint = sha256(ast.dump(ast.parse(source)))` (`contracts/registry.py:159-162,222-223`)
зависит от Python-минора. Фикстуры записаны на 3.11 (`_manifest.json: python_minor "3.11"`),
dev = 3.13 → `FixtureFreshnessGate` (`tests/v2/test_v1_contracts.py:415,469-481`) шорткатит ОДНИМ
«Python minor mismatch» вместо 28 ложных «source changed». Локально на 3.13 гейт RED **by design**;
честный обход — гонять контракты на 3.11 (сообщение самого гейта, `:478-479`). CI это и делает:
`predeploy.yml` ставит Python 3.11 и проверяет интерпретатор (`:158-161`).

**Поправка к гипотезе.** «Бейзлайн 2.6.45 протух» — это **PREDEPLOY-PROBE бейзлайн**
(`scripts/predeploy_baseline.json:3`, отдельная live-probe подсистема), **НЕ** contract/fixture
бейзлайн. У контрактной системы версионного бейзлайна нет; её векторы протухания — правки v1-исходника
(ловит fingerprint) + 3.11/3.13 skew. Гипотеза смешала две независимые механики.

**Инвариант-убийца.** Нормализовать недетерминизм при записи (плейсхолдер времени); развязать
fingerprint от env (минор-стабильная проекция / единая среда хэширования).

**Спека ремедиации:**
- **HYGIENE (дёшево):** расширить `record_fixtures.py::_strip_volatile_body` / `_VOLATILE_BODY_KEYS`
  (`:85-97`) — нормализовать сегмент `export_<epoch>` в `out_path` к плейсхолдеру на записи; добавить
  guard в `FixtureBodyDeterminism` (`test_v1_contracts.py`). **Размер S.**
- **DEEPER:** fingerprint env-развязка в `registry.py::_ast_part_for` (`:137-162`) — общий с cache-key
  (`registry.py:226-272` → `cache.py`), высокий blast-radius → отдельный ADR. **Размер M/L.**
- ⚠️ **ВНЕ SAFE-MAX (требует re-record/restamp — НЕ трогать в этой сессии):** ретроактивно стереть
  закоммиченный timestamp (нужен re-record фикстуры); закрыть 3.11/3.13 гейт перезаписью на 3.13;
  рестамп `predeploy_baseline.json`. Hygiene-фикс рекордера действует только на СЛЕДУЮЩЕМ re-record.
- **Риск фикстур/рестампа:** сам ФИКС требует re-record (вне scope сейчас); ПИН-тест — нет.
- **Пин:** `test_s5_export_word_list_fixture_has_no_nondeterministic_timestamp` (читает фикстуру,
  пиннит epoch; чистое чтение, фикстуру не меняет).

---

## Новые системные корни (всплыли по ходу)

**N1 — Хэндсинк-дубли (тот же мета-паттерн, что «правила = человеко-память»).** Несколько
load-bearing копий держатся в синхроне ВРУЧНУЮ: trusted-set сборка (`rag_v2.py:1539` и `:2155`),
`_RANK_SOURCE_KEYS` (`router.py:157`) дублирует чтение `_inject`, сам `_inject` зовётся из 3 мест
(`router.py:271`, `:500`, `rag_v2.py:2018`). Каждая копия — будущий шов дрейфа (ровно класс
«забыл обновить вторую копию»). Системный ответ: свернуть в единственные источники (часть фиксов
S2 и S4 это и делают — стоит зафиксировать как явную цель).

**N2 — Построенный, но мёртвый структурный рендер.** Целый render-from-evidence путь
(`template_executor.render_view` + `view_types`/`view_builders` + per-tool view-эмиссия) существует,
покрыт тестами, но **мёртв на живом пути** (`rag_v2.py:1095-1097`). Это латентная ремедиация S3,
уже наполовину построенная — существенно для сайзинга S3 (скелет есть; работа = проводка + scope-слот).
Риск-сигнал: мёртвый код, который тесты держат зелёным, маскирует что прод его не использует.

---

## Рекомендация по последовательности (reframe R-29/R-30)

**Reframe R-29 подтверждён.** Пивотить R-29 (или открыть R-30) вокруг **S1+S2** (scope- и
grounding-контракты); словарь и A/B-фиксы становятся побочным эффектом — ровно как предсказал аудит.
Рычаг S1+S2 («гнездо», гасит bug A, bug B, B119, B118, бóльшую часть misroutes) держится.

Рекомендуемый порядок:
1. **S1 (S-кусок) + S2 (M)** — первыми, вместе. S1 имеет дешёвый высокоценный под-фикс: сделать
   `_plan_author_top_words` book-aware (зеркало `_plan_author_vocab` → `affinity_by_book`) —
   убивает book→author коллапс planner-only, без корпуса. S2 lemma-grounding убивает ложные флаги bug B.
2. **S4 (M, схема-контракт ключей)** — локально, независимо, можно параллельно. (Dynamic fan-out —
   позже, M-L.)
3. **S5 timestamp-нормализация (S, hygiene)** — параллельно дёшево; НО зелёная цель требует re-record →
   делать в обычном prod-цикле re-record, НЕ в night-сессии.
4. **S3 (L, ре-арх.)** — отдельный трек ПОСЛЕ S1/S2; стартовать с N2 (готовый мёртвый скелет).
5. **НЕ** строить роадмап-фичи (#4 carryover, #5 multi-step A+B, #9-11 клики) поверх, пока S1/S2 не сели —
   иначе каждая фича множит баг-поверхность.

---

## CI-вердикт

PR: см. ссылку в конце (база `main`). Воркфлоу `.github/workflows/predeploy.yml` (триггер
`pull_request → main`, Python **3.11**).

**Факт — CI run [27486698687](https://github.com/Standaoerby/wordcracker/actions/runs/27486698687) (PR #52):**

| Job | Результат | Деталь |
|---|---|---|
| `tests/v2 (R10 collect + full)` | ✅ **PASS** (4m56s) | `2511 passed, 15 skipped, 6 xfailed, 678 subtests passed` — **0 failed**. 6 xfail = мои 5 + 1 существующий (`test_cache_ast_fingerprint` D-SF1-4). R10-collect без ошибок; ни один существующий тест не сломан. |
| `12-probe config sanity` | ✅ **PASS** (10s) | конфиг проб не трогали |
| `Mandatory version-bump` | ❌ **FAIL** (5s) — **ОЖИДАЕМО, by design** | `[version-bump] ANALYTICS_VERSION did NOT bump: merge-base(origin/main)=fbb7992ea1f7 == current == '2.7.14'. Edit scripts/v2/__version__.py before deploying.` |

**Про version-bump red.** Гейт `check_version_bump.py` требует движения `ANALYTICS_VERSION`
(`scripts/v2/__version__.py`) на ЛЮБОМ PR→main (exit 3 иначе). Этот PR — audit-доки + xfail-тесты,
**изменений analytics-поведения НЕТ**. По safe-max версия **намеренно НЕ бампнута**: бамп — это
release/deploy-действие (докстринг `__version__.py`: «bumped by hand on each release»), принадлежит
реальному фиксу S1/S2 (решение Стэна), а не аудит-PR; иначе это ложный release-сигнал. Runbook прямо
допускает «красный CI, фикс очевиден/безопасность в этом режиме под вопросом → оставить с заметкой,
НЕ долбить». Красный тут — самый ожидаемый и безопасный; долбления не было. **Чтобы загринить:** одна
строка-бамп в `__version__.py` (тривиально, обратимо) — сознательно оставлено Стэну вместе с go на фикс.

**Вердикт:** содержательные чеки зелёные (тесты + конфиг); единственный red — ожидаемый deploy-гейт,
объяснён. PR готов к ревью.

---

## DoD (Задание Code-сессии)
- [x] Карта S1–S5 ПОДТВ./ОПРОВ. + file:line.
- [x] Ремедиация-сайзинг (инвариант, файлы/функции, S/M/L, blast-radius, риск фикстур/рестампа).
- [x] xfail-тесты-пины на подтверждённые корни (S1×2, S2, S4, S5; S3 — обоснованно отложен).
- [x] Вердикт по reframe R-29 (подтверждён: пивот на S1+S2).
- [x] Новые системные корни (N1 хэндсинк-дубли, N2 мёртвый структурный рендер).
- [x] CI-вердикт — записан (см. §CI): tests/v2 GREEN (0 failed, 6 xfail), probe-config GREEN,
  version-bump RED (ожидаемо, deploy-гейт).
- PR ждёт Стэна. **Мерж/деплой — не в этой сессии.**
