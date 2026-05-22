# RECOVERY_BRIEF — восстановление консистентности по фазам

> Спутник `REFACTOR_BRIEF.md`. После прохождения Фаз 0–6 в `pytest tests/v2`
> осталось 36 падений. Этот документ группирует их по фазам-источникам и
> даёт конкретный шаг восстановления для каждого кластера.

## TL;DR

- Гейты Фаз 0–6 зелёные. Сьют собирается полностью (0 ошибок коллекции), 1407 passed / 19 skipped, **36 failed**.
- 36 падений — структурные (моки, реестры, формы payload), не доменные. Никаких новых багов из-за Фазы 6 не пришло (baseline проверен на `HEAD~1`).
- **Порядок работ незыблем**: A → B → C → D → остальные. Кластер A блокирует ~24 теста; пока он не починен, оценить остальное невозможно.
- Каждый кластер = отдельный PR. **R7 запрещает каскад** (3+ коммита подряд в одну область → эскалация до рефакторинга).

---

## Метод инвентаризации

Каждый failing-тест отнесён к фазе по тому, **что именно сломалось**, не по имени файла:

- `r.ok=False` + `from learning_tools import …` в враппере → **Фаза 2** (контракт v1↔v2; моки бьют не туда)
- `KeyError` в `view.payload` → **Фаза 2** (изменилась форма payload, тест читает старый ключ)
- `view is None`, `data_validity is None` → **Фаза 2.5** (try/except в враппере глотает ошибку attach_view)
- `wrapper_version`, `REGISTRY[tool]` ассерты → **Фаза 1** (схлопывание поколений переименовало/убрало инструменты)
- `KeyError: 'args'` в `rr.results[0].data` → **Фаза 4** (роутер сменил форму результата)
- `similarity_confidence is None` → **Фаза 3-смежная** (scoring; не покрыто гейтом Фазы 3)
- расхождение реальной статистики (`books_matched=2 != 123`) → **тестовая стенда**, не код

---

## Кластер A — v1-моки бьют по неправильному пути (Фаза 2, P0)

**Затронуто (~24 теста):**

- `test_affinity_surname_filter.py::test_surnames_filtered_from_book_top`
- `test_corpus_artifacts_filter.py::test_affinity_by_author_drops_xvth_and_iii`
- `test_count_honesty.py` (3 теста про `affinity_by_*`)
- `test_e14_retry_on_empty.py` (4 теста)
- `test_e15_v1_contract_keys.py::test_view_reads_v1_top_key`
- `test_e23_e26_persona_batch.py::test_legacy_cefr_key_still_works`
- `test_e2_motion_verbs_lexicon.py::test_wrapper_applies_motion_filter`
- `test_e36_no_internal_leaks.py::test_disclosure_no_dev_test_words_or_internal_jargon`
- `test_e38_e43_persona_batch2.py::E41BookArchaicFrequency::test_frequency_populated_from_book_count`
- `test_learning_words_key_contract.py` (4 теста)
- `test_phase2_5_tool_views.py` (8 тестов)

**Симптом.** `result.ok == False`, `result.view is None`, `result.error.message` вида «counts file not found for this PG id» — несмотря на то, что тест мокает v1 c корректными данными.

**Корень.** Враппер импортирует v1 как **bare-name** после вставки `_REPO` в `sys.path`:

```python
# scripts/v2/tools/books/affinity_book.py:65
from learning_tools import affinity_by_book as _v1
```

Тест мокает по пути с префиксом `scripts.`:

```python
with mock.patch("scripts.learning_tools.affinity_by_book", return_value=v1):
    ...
```

`mock.patch("scripts.learning_tools.affinity_by_book")` патчит атрибут на модуле `scripts.learning_tools`. Но враппер уже получил bare-имя `learning_tools.affinity_by_book` через `sys.path`. **Это два разных объекта в `sys.modules`.** Враппер вызывает оригинальный модуль (без диска) → fail.

Это R4 наоборот: мок подгоняли к враппер-импорту 5 лет назад, а после `from learning_tools import …` в Фазе 2 моки осиротели.

**Recovery (один коммит на всю партию):**

1. Источник правды для пути v1 — декоратор `@v1_contract(v1_fn="learning_tools.X", …)`. Сгрепать все аргументы `v1_fn=` в `scripts/v2/tools/` — получишь канонический список модуль-пар.
2. Запустить `grep -rn 'mock.patch("scripts\\.\\(learning_tools\\|rag_tools\\)\\.' tests/v2/` — каждое совпадение **выпрямить** до bare-имени: `scripts.learning_tools.X` → `learning_tools.X`, `scripts.rag_tools.X` → `rag_tools.X`.
3. Для `mock.patch.dict(sys.modules, {"scripts.learning_tools": fake})` — заменить на `{"learning_tools": fake}` (если враппер всё ещё импортит bare-name).
4. **R3 негативный тест** против регресса: добавить контракт-тест, что КАЖДЫЙ враппер с `@v1_contract` мокается тестом по тому же пути, что в декораторе. Дрейф между декоратором и моком ловится в коллекции.

**Не делать.** Не двигать sys.path в врапперах «чтобы моки работали» (R8: чини мок по фактическому пути, не код по моку).

**Gate этого кластера.** После починки: каждый тест в списке либо `passed`, либо чётко-доменный fail (не «mock not applied»). `r.error.message` про «counts file not found» исчезает.

---

## Кластер B — реестр инструментов после Фазы 1 (Фаза 1, P1)

**Затронуто (1 тест):**

- `test_alpha3_pre_prod_fixes.py::CacheKey_WrapperVersionInvalidation::test_bumped_tools_carry_new_version`

**Симптом.** `KeyError: 'hybrid_search'` + `'v5-phase2-contract' != 'v4-e15-normalize'`.

**Корень.** Фаза 1 («схлопнуть поколения») удалила `hybrid_search` (или переименовала) и привела `wrapper_version`-схему к единому виду `vN-phaseN-…`. Тест замораживал точные строки из эпохи alpha3.

**Recovery (точечно):**

1. Запустить `python -c "from scripts.v2.tool_registry import REGISTRY; [print(k, v.wrapper_version) for k, v in REGISTRY.items()]"` — получить актуальный список.
2. В тесте `test_bumped_tools_carry_new_version`:
   - Удалить ассерты на инструменты, которых больше нет в REGISTRY (`hybrid_search` и подобные).
   - Заменить заморожённые строки `wrapper_version` на актуальные из шага 1.
3. **Не превращать тест в зеркало REGISTRY**. Его цель — ловить «забыл бампнуть версию при правке семантики враппера». Оставь ровно те инструменты, чья семантика менялась в alpha-каскаде; для остальных вообще убери ассерты.
4. **R-23 негативный тест**: добавить параметрический тест, что у каждого враппера `wrapper_version` соответствует регексу `^v\d+(-phase\d+)?-[a-z0-9-]+$`. Дрейф формата ловится автоматически.

---

## Кластер C — форма payload во view (Фаза 2, P1)

**Затронуто (4 теста):**

- `test_phase2_tool_views.py::AffinityByAuthorViewEmission::test_count_honesty_caveat_when_filter_drops`
- `test_phase2_5_tool_views.py::BooksViews::test_book_archaic_words` (`KeyError: 'rows'`)
- `test_phase2_5_tool_views.py::BooksViews::test_book_emotion_profile` (`KeyError: 'emotions'`)
- `test_phase2_5_tool_views.py::AuthorsViews::test_author_influences` (`'holmes' not found in []`)

**Симптом.** `result.view.payload["count_returned"]` → KeyError; `payload["rows"]` → KeyError; ожидаемое слово отсутствует в результате.

**Корень.** Контракты `build_top_n_table` / `build_emotion_profile` / `build_author_lookup` нормализовали ключи; тесты читают старые имена либо ожидают данные, которые сейчас фильтруются (surname filter, propn filter).

**Recovery:**

1. Для каждого failing-теста — открыть соответствующий `build_*` в `scripts/v2/view_builders.py`, прочитать форму payload в success-пути.
2. Привести ассерты теста к актуальным ключам. Не двигать ключи в builder ради теста (R4).
3. Где данные «исчезли после фильтра» — проверить через `provenance.filtered`: если корень в фильтре, тест должен это знать (ассертить count в provenance, не сырой массив).
4. Если builder **не** выставляет `count_returned` в каком-то path (например, empty) — это баг Фазы 2 (count-honesty), чинить **builder**, не тест.

**Gate.** Все 4 теста зелёные. `grep -rn 'payload\\[\"count_returned\"\\]' tests/v2/` сверяется со списком success-paths, где builder его реально выставляет.

---

## Кластер D — view-emission timing / `data_validity` (Фаза 2.5, P1)

**Затронуто (4 теста):**

- `test_phase2_tool_views.py::LearningWordsViewEmission::test_normal_result_emits_table_with_words`
- `test_phase2_tool_views.py::LearningWordsViewEmission::test_legit_empty_for_rare_level_not_broken`
- `test_phase2_tool_views.py::LearningWordsViewEmission::test_b_r14_7_b2_on_pride_marks_broken`
- `test_alpha4_round12_fixes.py::LearningWordsStampsCount::test_learning_words_stamps_top_returned`

**Симптом.** `result.data_validity is None` для нормальных результатов. `'NoneType' object has no attribute 'get'` (alpha4) — view не привязался → атрибут не дёрнулся.

**Корень.** Враппер заворачивает `attach_view` в `try/except Exception` и **молча проглатывает** ошибку. В Фазе 6 attach_view стал ленивее (required-field-missing уже не блокирует), но если внутри _строится_ кривой view, до attach_view не доходит. Тест видит result без view → `data_validity` дефолтное None.

Пример (`scripts/v2/tools/learning/enrich.py:115`):

```python
try:
    view = vb.build_etymology_bundle(...)
    vb.attach_view(result, view, data_validity=DataValidity.OK)
except Exception as e:
    logging.warning("enrich_word view emission failed: %s", e)
```

Эта ловушка нужна, чтобы враппер не падал из-за бага рендера. Но она же скрывает баги от тестов.

**Recovery:**

1. **Сначала** — снять exception silently. Заменить `except Exception` на узкий `except (ValueError, TypeError)` И **залогировать с traceback**: `logging.exception(...)`. Тогда CI/тесты увидят корень.
2. Запустить 4 теста выше — увидеть реальный exception. Скорее всего — `build_learning_words` строит view с пустыми словами без `is_broken/empty_reason`, или v1-мок отдаёт ключ, который Фаза 2 нормализовала иначе.
3. Починить **builder или враппер** по факту exception. Тест не подгонять.
4. **R3 контракт-тест**: для каждого враппера с view — golden, что `result.view is not None` для success-сценария и `result.data_validity` строго в `{OK, PARTIAL, EMPTY_EXPECTED, EMPTY_UNEXPECTED, BROKEN}` (не `None`).

**Не делать.** Не удалять `try/except` совсем — рендер не должен ронять тул. Только сузить и шуметь.

---

## Кластер E — router / pipeline shape (Фаза 4, P2)

**Затронуто (1 тест):**

- `test_pipeline_e2e.py::V2PipelineE2E::test_q07_book_chain_resolves_pg_id`

**Симптом.** `KeyError: 'args'` в `rr.results[0].data["args"]["pg_id"]`.

**Корень.** Фаза 4 («fan-out как инвариант роутера») перетряхнула форму `RouterResult.results[i].data`. Раньше там лежал `{"args": {...}}`, теперь либо аргументы доступны через `results[i].query`, либо плоско в `data`.

**Recovery:**

1. Открыть `scripts/v2/planner/router.py`, найти, как формируется `data` (или `args`/`query`) после Фазы 4.
2. Привести ассерт в `test_q07` к актуальному месту хранения `pg_id`. Скорее всего — `rr.results[0].query["pg_id"]`.
3. Если есть ещё E2E-тесты, опирающиеся на `data["args"]` — починить пачкой.

---

## Кластер F — similarity_confidence (scoring, P2)

**Затронуто (2 теста):**

- `test_scoring_v3.py::ConfidenceFloor::test_clear_winner_marked_high`
- `test_scoring_v3.py::ConfidenceFloor::test_tight_cluster_marked_low`

**Симптом.** `raw.get("similarity_confidence")` возвращает `None`.

**Корень.** Слой `scoring` (`scripts/v2/scoring/`) перестал штамповать `similarity_confidence` на сырой результат — либо при миграции в Фазе 1 (схлопывание), либо при перенаправлении через chokepoint Фазы 5. Это не покрывалось ни одним гейтом.

**Recovery:**

1. `grep -rn 'similarity_confidence' scripts/v2/` — найти, кто его выставляет и где.
2. Если выставление осталось, но dispatch отрезал — добавить шаг в pipeline.
3. Если логика выставления была удалена — восстановить из git blame `scripts/v2/scoring/__init__.py`.
4. Негативный тест: clear_winner / tight_cluster должны давать разные значения (high vs low). Этот тест уже есть — просто пройдёт после восстановления.

---

## Кластер G — тестовая стенда (нет фазы, P3)

**Затронуто (1 тест):**

- `test_tool_migration.py::V2MigratedTools::test_author_metadata_happy` — `r.coverage.books_matched=2 != 123`.

**Симптом.** Реальный dispatch на корпус возвращает 2 книги Дойла, тест ждёт 123.

**Корень.** Тест не мокает v1 — идёт в реальный корпус. На тестовой машине Doyle subset = 2 книги; на проде = 123. Тест предполагал прод-корпус, но его в `tests/v2` не разворачивают.

**Recovery:**

1. Замокать v1 `author_metadata` фиктивным `books_in_corpus=123` и проверить только, что враппер прокинул число.
2. ИЛИ — пометить `@skipUnless(WC_FULL_CORPUS)` и оставить только в golden-сьюте.
3. **Не подменять `assertEqual(books_matched, 123)` на `assertGreater(0)`** — это разводнит проверку (потеряет ценность теста как golden number).

---

## Сводная таблица

| Кластер | Тестов | Фаза-источник | Приоритет | Один коммит? |
|---|---|---|---|---|
| A — v1-mock путь | ~24 | 2 | **P0** | да (один батч-фикс моков) |
| B — REGISTRY/wrapper_version | 1 | 1 | P1 | да |
| C — payload form | 4 | 2 | P1 | да |
| D — view emission silently | 4 | 2.5 | P1 | да (сначала снять exception silently) |
| E — router result shape | 1 | 4 | P2 | да |
| F — scoring confidence | 2 | scoring | P2 | да (восстановление из blame) |
| G — corpus stand | 1 | — | P3 | да (мок или skip) |
| Итого | 37* | — | — | 7 коммитов |

\* 37 потому, что `test_affinity_surname_filter`, `test_corpus_artifacts_filter` и `test_e2_motion_verbs_lexicon` сидят и в A, и в их собственных доменах фильтрации. После починки A они автоматически перейдут либо в зелёные, либо в чётко-доменные фейлы — тогда уже видно, надо ли отдельно чинить фильтры.

---

## Anti-pattern checklist (специально для этой работы)

- ❌ Подгонять `mock.patch("scripts.X.Y")` под несуществующий путь — **чини мок, не путь** (R4).
- ❌ Молча глотать exception в view-emission — **узкий `except` + `logging.exception`**.
- ❌ Менять `wrapper_version` строки в коде, чтобы пройти тест — **меняй тест**, версия отражает реальность.
- ❌ `assertGreater(0)` вместо точного числа — **разводнённый ассерт хуже сломанного теста** (теряет ценность golden).
- ❌ Каскад из 5 коммитов в одном кластере — **остановись на 3-м** и эскалируй до подзадачи (R7).

---

## Порядок исполнения

1. **Кластер A** (один коммит, ~24 теста зеленеют). Без него остальное мерить нечем — много тестов прокидывают через v1-мок.
2. **Кластер D** (снять exception silently; узнать корни остальных view-emission падений).
3. **Кластеры B, C, E, F** — параллельно, каждый отдельным коммитом.
4. **Кластер G** — последним; зависит от того, что осталось.

Gate финальный (после всех 7 коммитов): `pytest tests/v2 -q` → **0 failed**.
