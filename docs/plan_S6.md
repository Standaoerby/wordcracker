---
project: wordcracker
type: plan
sprint: 6
updated: 2026-06-10
status: review-addressed (v2 — все NOTE из plan_review_S6 закрыты, имплементация не начата)
base: main 1f562f5 / 2.6.48
sources: RAG_TASK_S6.md · plan_review_S6.md · решения Stan 2026-06-10 (N1, N2-GPU, Q1–Q4/Q6, B105)
---

# plan.md — Sprint 6: веб-каркас + быстрые победы (v2 после ревью)

## §0. Disposition нот ревью

| Нота | Статус | Где в плане |
|------|--------|-------------|
| **N1** числовые типы → Excel | закрыто | §2.4 (эскиз `TABLE_EXTRACTORS`/`_scalar`), §5.2, тест §10.3 |
| **N2** девайс эмбеддера / GPU | закрыто решением Stan: **GPU (CUDA) + auto-fallback CPU** | §6.2–6.4 |
| Q1 thinking при data_only | закрыто: эмитить всегда, клиент прячет | §2.5, §7.4 |
| Q2 error при max_iterations | закрыто: `stop_reason` в `done` | §2.6, §4.2 |
| Q3 health | закрыто: `/api/health` liveness + `/api/ready` | §3.3 |
| Q4 react-query | закрыто: zustand-only в S6 | §7.1 |
| Q5 память | = N2 | §6.4 |
| Q6 Vite-билд | закрыто: multi-stage в Dockerfile | §6.5 |
| ~25 тулов / трасса на виду | принято | §1.2, §10.2 |
| `history=None` задел S9 | принято | §2.7 |
| `keep_alive=-1` | принято, задокументировать | §8.2 |
| data_only «стоп после первого набора тулов» | принято, в `webapp.md` | §2.5, §8.2 |
| disconnect ≈ GeneratorExit в ~1 токен | принято, в `webapp.md` | §3.4, §8.2 |
| StaticFiles порядок / SPA catch-all | принято | §6.5 |
| SSE-парсер `\r\n\r\n` и `\n\n` | принято | §7.2 |
| `tables` по `done` — источник истины | принято | §7.3 |
| имена листов xlsx (31 симв./запрещённые/дубли) | принято | §5.3 |
| **B105** clickable cells (новое от Stan) | заложено: `data-query` атрибуты сейчас, полный UX → S6.5/S7 | §7.5 |

---

## §1. Контекст и отклонения от ТЗ-литерала

### 1.1 Движок
ТЗ (§1 спеки) описывает Sprint-5 стек (`rag_query.ask`, 6 тулов, LLM tool-choice). Реальный репо —
**v2-пайплайн**: `scripts/v2/rag_v2.py` → `planner → router → tools (~25, registry) → renderer`,
с готовым `ask_stream()` (события `start/intent/plan/v4_plan/tool_call/tool_result/answer/critic/
clarify/done/error`). Строим веб-слой **поверх v2**, а не по литералу ТЗ. Ревью это подтвердило.

### 1.2 Сервис-изоляция
`wordcracker-api` — **новый** контейнер/процесс, параллельный `chat_server:8890`. 8890 не трогаем
(прод-чат живёт), общий код — через импорт `scripts.v2`. ~25 тулов и поведение планнера — как есть;
следствие: трасса `<ToolTrace>` теперь на виду у юзера, ошибки роутинга видны → регрессия §10.2
обязана задеть learning_tools и multi-table кейсы.

### 1.3 Ограничения S6
БД/проектов нет (S7) → состояние эфемерно, экспорт xlsx — client-roundtrip (§5).
`entities` в envelope всегда `[]` (задел S10). `history` — поле принимаем, но не прокидываем (§2.7).

---

## §2. T1 — `scripts/api_loop.py`: стримящий agent loop

### 2.1 Сигнатура
```python
def stream_answer(question: str, data_only: bool = False) -> Iterator[dict]:
    """Yields {"event": str, "data": dict}. Оборачивает v2-пайплайн."""
```

### 2.2 Реализация: тонкий адаптер над `ask_stream`, стримящий рендер
`ask_stream()` уже отдаёт planner/tool-фазу событиями, но рендер (`_llm_render`) — один
блокирующий `/api/chat` вызов с полным текстом в `answer`. Для S6:

1. В `rag_v2.py` добавить `_llm_render_stream(...)` — тот же RENDER_PROMPT/payload, но
   `chat(stream=True, think=True)`; yields `("thinking"|"token", delta)`, в конце возвращает
   `(full_text, meta)` (счётчики токенов для obs — как у `_llm_render`).
2. `ask_stream(..., stream_render: bool = False)` — новый kwarg, default `False` ⇒
   **поведение 8890 не меняется**; `api_loop` зовёт с `True`.
3. `api_loop.stream_answer` мапит v2-события на контракт §4 (таблица маппинга в §4.3)
   и добавляет `table`-события через `TABLE_EXTRACTORS` (§2.4).
4. `ask()` для ноутбука не трогаем — общий код уже вынесен в v2-пайплайн, дублирования нет.

Ollama — **нативный `/api/chat`** (как сейчас в v2), не `/v1`. Версия Ollama уже запинена в
compose (0.24.0, S-P2). `num_ctx`/`keep_alive` — те же правила, что в S-P2 (выровнены, `-1`).

### 2.3 `max_iterations`
Сохраняется (anti-infinite-loop). Исход фиксируется НЕ событием `error`, а полем
`stop_reason` в `done`-envelope (§2.6).

### 2.4 `TABLE_EXTRACTORS` — N1: нативные числовые типы
Модуль `scripts/api_loop.py` (или `scripts/v2/table_extract.py`): реестр
`{tool_name: extractor}` + generic-фоллбэк по `ToolResult.view`/`data` (list[dict] → columns/rows).

**Инвариант: значение ячейки — скаляр нативного типа** (`int | float | str | bool | None`).
numpy/pandas-типы приводим через `.item()`; вложенные структуры — плющим на этапе экстрактора,
что не плющится — `str()`/компактный json. `default=str` в `json.dumps` остаётся только как
последний рубеж для реально несериализуемого (датавремя и т.п.), а не как способ сериализовать
числа.

```python
import json

def _scalar(v):
    """Ячейка таблицы → нативный скаляр. Числа остаются числами (N1):
    иначе xlsx-ячейки становятся текстом и ломают сортировку/суммы."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    item = getattr(v, "item", None)        # numpy.int64 / float32 / bool_
    if callable(item):
        try:
            return _scalar(v.item())
        except (ValueError, TypeError):
            pass
    if isinstance(v, (list, tuple, dict)): # вложенное: плющить должен extractor;
        return json.dumps(v, ensure_ascii=False, default=str)  # это — фоллбэк
    return str(v)

def rows_from_records(records: list[dict], columns: list[str]) -> list[list]:
    return [[_scalar(r.get(c)) for c in columns] for r in records]
```

Экстракторы для тулов с известной формой (corpus_stats, top_ngrams, affinity, learning_words,
semantic_search, compare_authors, …) объявляют `columns` явно; остальное — generic.
Тот же `rows` уходит и в SSE `table`, и в `tables[]` envelope, и в xlsx (§5) — одна сериализация,
негде разойтись. Тест — §10.3.

### 2.5 `data_only=True`
После **первого** набора tool-результатов рендер-вызов не делается: собираем `tables[]`,
`answer_md=""`, эмитим `done`. Упрощение «стоп после первого набора тулов» обрежет планы из
2+ последовательных шагов — для S6 принято (обычно один шаг), **документируем в `webapp.md`**
(§8.2). thinking-события планнера при этом всё равно эмитятся (Q1) — клиент их прячет сам.

### 2.6 `stop_reason` (Q2)
В `done`-envelope: `"stop_reason": "complete" | "max_iterations" | "tool_error"`.
Событие `error` — только для неожиданных исключений транспорта/пайплайна; **инвариант: стрим
всегда заканчивается `done`** (после `error` — `done` с тем, что успели собрать). Клиентский
error-UI триггерится только событием `error`, не `stop_reason`.

### 2.7 `history`
В теле запроса поле принимаем (Pydantic: `history: list[dict] | None = None`), но в S6 НЕ
прокидываем в пайплайн — задел S9 (#4). Тесты не предполагают его работу.

**Acceptance T1:**
```bash
python -c "from scripts.api_loop import stream_answer; \
  [print(e['event']) for e in stream_answer('дай статистику по Wodehouse')]"
# → ...tool_call → tool_result → table → token* → done
```

---

## §3. T2 — `api/main.py`: FastAPI-сервис

### 3.1 Роуты
- `POST /api/query` → `EventSourceResponse` поверх **синхронного** генератора-обёртки
  `stream_answer()` (FastAPI гонит sync-генератор в threadpool, event loop свободен).
  Каждый yield → `ServerSentEvent(event=..., data=json.dumps(..., ensure_ascii=False, default=str))`.
- `POST /api/export/xlsx` (§5).
- `GET /api/health`, `GET /api/ready` (§3.3).
- `fastapi >= 0.135` (встроенный `fastapi.sse`); если пин не взлетит — fallback `sse-starlette`
  (API совместим). Зафиксировать фактический выбор в `decisions.md` D17.

### 3.2 Middleware
- CORS только для dev-origin (`http://localhost:5173`), через `.env`.
- **GZipMiddleware на SSE-роут не вешать** (ломает стриминг); gzip — точечно на статику, если
  вообще нужен.

### 3.3 health vs ready (Q3)
- `/api/health` — чистый liveness (процесс жив, 200 + `{"ok": true}`), **это** идёт в docker
  healthcheck. Ничего внешнего не проверяет — иначе рестарт Ollama утянет api в restart-петлю.
- `/api/ready` — readiness: reachability Ollama (`GET /api/tags` с коротким таймаутом) +
  Chroma-коллекция открыта. Для смоука/оператора, НЕ для healthcheck.

### 3.4 Disconnect
Клиент ушёл → starlette рвёт генератор → `GeneratorExit` на следующем `yield` → отмена садится
в пределах ~одного токена. Внутрь пайплайна прокидываем `cancel_event` (v2 его уже умеет) из
finally-блока обёртки. Для S6 этого достаточно; семантику документируем в `webapp.md`.

**Acceptance T2:** `curl -N -X POST localhost:8000/api/query -d '{"question":"топ-10 биграмм
Достоевского"}'` течёт событиями в реальном времени, заканчивается `done`.

---

## §4. Контракт API (финальный)

### 4.1 `POST /api/query` — тело
```json
{ "question": "string", "data_only": false }
```
(`history` принимается, игнорируется — §2.7; `project_id` — S7.)

### 4.2 SSE-события и envelope
| event | data | примечание |
|-------|------|------------|
| `thinking` | `{"delta": "..."}` | всегда эмитится (Q1); клиент прячет по умолчанию |
| `token` | `{"delta": "..."}` | дельта финального ответа (markdown) |
| `tool_call` | `{"name", "args"}` | |
| `tool_result` | `{"name", "elapsed", "ok"}` | elapsed в секундах (из `runtime_ms`) |
| `table` | `{"tool", "columns", "rows"}` | rows — нативные скаляры (§2.4) |
| `trace` | `{"kind": "intent"\|"plan"\|"v4_plan"\|"critic"\|"clarify", ...}` | v2-события для `<ToolTrace>`; клиент обязан игнорировать неизвестные `kind` |
| `error` | `{"message"}` | только неожиданные исключения; после него всё равно `done` |
| `done` | envelope ↓ | **всегда последний кадр** |

```json
{
  "query_id": "ephemeral-uuid",
  "answer_md": "...",
  "tables": [ {"tool": "...", "columns": [...], "rows": [...]} ],
  "entities": [],
  "tool_trace": [ {"name": "...", "args": {...}, "elapsed": 1.2} ],
  "data_only": false,
  "stop_reason": "complete | max_iterations | tool_error"
}
```

### 4.3 Маппинг v2 → контракт
`start`→(съедается) · `intent/plan/v4_plan/critic/clarify`→`trace` · `tool_call/tool_result`→
одноимённые (поля нормализуем: `ms`→`elapsed` сек) · стрим-рендер→`thinking`/`token` ·
`answer` (нестримовые short-circuits, напр. repeat)→один `token` с полным текстом · `done`→
envelope §4.2. Forward-compat правило для клиента: неизвестные `event`/`kind` молча игнорировать.

### 4.4 `POST /api/export/xlsx`
```json
{ "tables": [ {"tool": "...", "columns": [...], "rows": [...]} ],
  "filename": "wordcracker_export.xlsx" }
```
Ответ — файл (один лист на таблицу). В S7 переключим на `?query_id=` из БД.

---

## §5. T4 — Excel-выгрузка

### 5.1 Бэкенд
`pandas.DataFrame(rows, columns=columns)` → `pd.ExcelWriter(engine="openpyxl")` → лист на
таблицу → `Response`/`StreamingResponse` c `Content-Disposition: attachment`.

### 5.2 Числа — числа (N1)
Благодаря §2.4 `rows` приходят с нативными `int/float` ⇒ pandas/openpyxl пишут числовые ячейки
(`cell.data_type == "n"`). Никаких `astype(str)`/`default=str` на пути таблиц. Тест §10.3.

### 5.3 Имена листов
Из `tool`: обрезка до **31 символа**, запрещённые `[ ] : * ? / \` → `_`, пустое → `sheet`;
дубли — суффикс `_2`, `_3` (с учётом того, что суффикс тоже влезает в 31).

**Acceptance T4:** клик скачивает `.xlsx`, открывается в Excel/LibreOffice, данные и **типы**
совпадают с таблицей в UI (числовая колонка сортируется как число).

---

## §6. T5 — Упаковка (compose, GPU, билд)

### 6.1 Сервис
`docker-compose.override.yml`: сервис `wordcracker-api` (uvicorn, тот же образ/venv, что RAG)
в сети `wordcracker_default`; `OLLAMA_HOST=http://ollama:11434`, Chroma — тот же volume/путь,
что у `chat`. Порт 8000. `.env` для хостов/портов/CORS-origin.

### 6.2 Девайс эмбеддера — N2, РЕШЕНО: GPU (CUDA) + auto-fallback CPU
Эмбеддер запросов (`semantic_search` эмбедит query; MiniLM через
`SentenceTransformerEmbeddingFunction`) в `wordcracker-api` работает на **CUDA** с автоматическим
фолбэком: try CUDA → `log.warning` → CPU. Текущий хардкод `device="cuda"`
(`scripts/rag_tools.py:131`) заменить на резолвер (общий для api и chat_server — поведение 8890
не меняется, на проде CUDA есть):

```python
def _resolve_embedder_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            torch.zeros(1, device="cuda")   # smoke: девайс реально живой
            return "cuda"
        log.warning("CUDA unavailable for query embedder; falling back to CPU")
    except Exception as e:
        log.warning("CUDA probe failed (%s); embedder falls back to CPU", e)
    return "cpu"
```
Тот же резолвер — для `bge_reranker` (lazy-load в registry).

### 6.3 Compose: GPU для api-контейнера
```yaml
wordcracker-api:
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```
(симметрично тому, как GPU отдан ollama/lab).

### 6.4 VRAM/RAM-бюджет и mem-limit (= Q5)
3090 = 24 GB VRAM. `qwen3:14b` ≈ **11.7 GB**, запинен навсегда (`keep_alive=-1`).
Эмбеддер MiniLM (~0.5 GB) + `bge-reranker-base` (~1.2 GB) обязаны умещаться в **≤3 GB** —
влезают с запасом; остаётся ~9 GB headroom под KV-cache/пики.
**mem-limit контейнера НЕ хардкодим.** Отдельный шаг плана (после T5, до merge):
> прогнать реальный `semantic_search` (+ reranker-кейс «похожие книги») через `/api/query`,
> снять RSS контейнера (`docker stats`) и VRAM (`nvidia-smi`) → выставить `mem_limit` по факту
> с запасом ×1.5 → зафиксировать цифры в `infra.md`.

### 6.5 Раздача статики и билд (Q6 + D18)
- **Multi-stage Dockerfile**: stage 1 `node:22-alpine` → `npm ci && npm run build`;
  stage 2 — python-образ, `COPY --from=build /app/dist /workspace/web/dist`,
  FastAPI `StaticFiles(directory=..., html=True)` на `/`. Воспроизводимо, без node на хосте.
- Порядок маунтов: **все `/api/*` роуты регистрируются ДО `app.mount("/")`**. SPA catch-all
  на `index.html` в S6 не нужен (один экран, без роутера) — отметить в коде комментарием.
- Внутренний цикл разработки: Vite dev-сервер `:5173` + CORS на `:8000` (без пересборки образа).
- nginx-сайдкар НЕ берём (лишняя движущаяся часть; nginx+CF уже есть выше по стеку) → D18.

**Acceptance T5:** `docker compose up` поднимает api+фронт; UI открывается с хоста; smoke §10.4
зелёный; в логе api видно `embedder device: cuda`.

---

## §7. T3 — Фронт (Vite + React + TS)

### 7.1 Стейт (Q4)
**zustand-only.** react-query в S6 не ставим (один POST-стрим + один export — нечего кешировать);
вернёмся в S7 (проекты/сохранёнки/история). Стор: `{question, status, thinkingBuf, answerBuf,
tables, trace, dataOnly, error, stopReason}`.

### 7.2 Чтение стрима
`fetch('/api/query', {method:'POST', body})` → `response.body.getReader()` + `TextDecoder` →
буфер → **резать кадры и по `\r\n\r\n`, и по `\n\n`** (`fastapi.sse`/`sse-starlette` шлют `\r\n`);
из кадра парсить `event:` + склейку `data:`-строк → диспатч по `event`. Неизвестные события
игнорировать (forward-compat, §4.3).

### 7.3 Таблицы
`<ResultTable>` рендерит из **структурных данных** (`table`-события по ходу стрима), НЕ из
markdown. По `done` стримленные таблицы **замещаются** `envelope.tables` — источник истины
(дедуп/финальный состав — за бэкендом). Сортировка по клику на колонку — клиентская, типо-чувствительная
(числа как числа — N1 это гарантирует).

### 7.4 Рендер ответа и трасса
- `answer_md`: `react-markdown@^10` + `remark-gfm` (v10: `components`-проп; `source`/`renderers` НЕТ).
- `<Thinking>`: свёрнут по умолчанию, наполняется `thinking`-дельтами (Q1 — без спецкейса data_only).
- `<ToolTrace>`: сворачиваемый блок — `tool_call`/`tool_result`/`trace`-события (имя, args, время).
  Трасса видима юзеру → косяки роутинга теперь публичные; ловим регрессией §10.2.
- Тумблер **«только данные»** → `data_only: true`; UI показывает только таблицы + трассу.
- Событие `error` → error-баннер; `stop_reason: "max_iterations"` → мягкая плашка
  «ответ обрезан по лимиту шагов», НЕ error-UI (Q2).

### 7.5 B105 — кликабельные ячейки (задел сейчас, UX — S6.5/S7)
В S6 в разметку `<ResultTable>` закладываем **`data-query` атрибуты**:
- ячейка книги → `data-query="book:PG1342"` (бэкенд отдаёт `title`+`pg_id` с B100);
- ячейка слова → `data-query="word:ardour"`.

Один делегированный onClick на таблице: `data-query` → диспатч в чат (заполнить input
шаблоном запроса и отправить). Экстракторы (§2.4) помечают такие колонки в `table`-данных
(`"col_meta": {"book": {"kind": "book", "id_col": "pg_id"}}` — поле опциональное, клиент без
него просто не делает ячейку кликабельной).
**Scope-граница:** сами атрибуты + простейший «клик → отправить запрос» — в S6 (дёшево);
если спринт поползёт — шаблоны запросов/hover-UX/иконки уезжают в **S6.5/S7** (пометка в
`backlog.md`), но разметка с `data-query` и `col_meta` остаётся в S6 безусловно.

**Acceptance T3:** в браузере вопрос даёт стримящийся ответ + таблицу + трассу; тумблер
«только данные» отдаёт таблицу без текстовой обёртки; у книжных/словесных ячеек в DOM есть
`data-query`.

---

## §8. T6 — Документация

### 8.1 decisions.md
- **D17** — web-стек: FastAPI (>=0.135, `fastapi.sse`; fallback sse-starlette если пин не взлетел),
  SSE поверх sync-генератора, React/Vite/TS, react-markdown@10, zustand-only (Q4),
  эмбеддер CUDA+fallback (N2).
- **D18** — раздача статики: FastAPI StaticFiles + multi-stage Vite-билд, без nginx-сайдкара (Q6).

### 8.2 webapp.md (новый)
Контракт §4 целиком + SSE-протокол + как читать стрим на клиенте + **явно задокументированные
упрощения S6**:
- `data_only` = «стоп после первого набора тулов» (многошаговые планы обрезаются);
- disconnect = отмена в пределах ~одного токена (GeneratorExit на следующем yield);
- `keep_alive=-1` пинит qwen3 в VRAM навсегда — ок для выделенной 3090; появятся другие
  модели → пересмотреть на конечное значение;
- `history` принимается и игнорируется до S9; `entities` всегда `[]` до S10.

### 8.3 README.md + CLAUDE.md
README: блок Sprint 6 с галочками. CLAUDE.md: блок «Web app (Sprint 6+)» из §7 спеки
(нативный /api/chat, не /v1; no-gzip на SSE; tables из структурных данных; fetch+ReadableStream;
держать файл < 200 строк) + hook на регрессию eval после задач, трогающих loop.

---

## §9. Открытые вопросы — НЕТ (все закрыты)

Q1 thinking → эмитить всегда, клиент прячет (§2.5/§7.4). Q2 → `stop_reason` в done (§2.6).
Q3 → health/ready разнесены (§3.3). Q4 → zustand-only (§7.1). Q5/N2 → GPU CUDA + fallback CPU,
лимит по замеру (§6.2–6.4). Q6 → multi-stage Vite (§6.5). N1 → нативные числа (§2.4/§5.2/§10.3).
Решений, требующих Stan до имплементации, не осталось.

---

## §10. Тесты и acceptance

### 10.1 Unit/integration (tests/webapp/, py3.11-ориентир как в CI)
- `test_api_loop.py`: последовательность событий happy-path; `data_only` (нет `token`, есть
  `table`+`done`, `answer_md==""`); `stop_reason` при `max_iterations` (мок планнера);
  `done` — всегда последний кадр, в т.ч. после `error` (мок исключения в тулзе).
- `test_table_extract.py`: numpy.int64/float32 → нативные `int/float`; вложенный dict →
  json-строка; None/bool проходят как есть.
- `test_export_xlsx.py`: см. §10.3; имена листов — 31 символ, запрещённые символы, дубли.
- `test_main.py` (TestClient): health=200 без Ollama; ready=503 без Ollama; CORS-заголовки;
  SSE-роут отдаёт `text/event-stream`, кадры с `\r\n\r\n`.

### 10.2 Регрессия через HTTP
10 вопросов из `04_rag.ipynb` через `/api/query` — осмысленный ответ/таблица, без exception,
< 90 c. Состав расширить: **≥1–2 кейса learning_tools** (трасса на виду) и **≥1 кейс с
несколькими таблицами** (multi-table envelope + xlsx с несколькими листами). Параллельно
смотреть латентность/токены: ~25 тул-спеков в промптах планнера — следить, не распухло ли.

### 10.3 Тест N1 (обязательный)
`test_xlsx_numeric_cell_is_number`: таблица с ячейкой `numpy.int64(42)` и `float 3.14` →
`POST /api/export/xlsx` → `openpyxl.load_workbook` → `cell.value` это `int`/`float`,
`cell.data_type == "n"`; колонка из [10, 9, 100] сортируется в Excel-семантике как числа
(10 < 100, не "10" < "100" < "9").

### 10.4 Smoke (§6 спеки, прод/комп)
`compose up` → ps (ollama, gutenberg-lab, chat, wordcracker-api — Up) → health → ready →
стрим-curl → data_only-curl (table+done, без token) → xlsx-curl + openpyxl sheetnames.
Плюс: лог api содержит `embedder device: cuda`; `nvidia-smi` показывает процесс api на GPU.

### 10.5 Definition of Done (= §8 спеки + дельты ревью)
- [ ] T1–T6 acceptance зелёные; smoke §10.4 проходит; регрессия §10.2 зелёная.
- [ ] `ask()`/ноутбук и chat_server:8890 не сломаны (default-флаги, поведение не изменено).
- [ ] Тест §10.3 (числа в xlsx) зелёный.
- [ ] Замер RSS/VRAM выполнен, `mem_limit` выставлен по факту, цифры в `infra.md` (§6.4).
- [ ] `decisions.md` (D17/D18), `webapp.md`, `README.md`, `CLAUDE.md` обновлены (§8).
- [ ] `data-query`/`col_meta` в разметке таблиц; остаток B105-UX помечен в `backlog.md` как S6.5/S7.

---

## §11. Порядок работ и процесс

T1 (api_loop + table_extract + стрим-рендер seam) → T2 (FastAPI) → T3 (фронт) → T4 (xlsx) →
T5 (compose/GPU/билд + замер лимита) → T6 (доки). Ветка от `main` 1f562f5/2.6.48, version-bump
sed-ом в том же коммите, PR-флоу; деплой в этом спринте не трогаем. Re-record фикстур НЕ
требуется (веб-слой не меняет v1/v2 тул-поведение; default-флаги сохраняют байт-в-байт пути
8890). Между задачами при заполнении контекста — дамп в `SESSION_2026-06-xx.md` → `/clear`.

**Риски:** (1) `fastapi>=0.135` пин против текущего lock — если конфликт, сразу fallback
sse-starlette (решение в D17, не блокер); (2) VRAM-пик при одновременном rerank+генерации —
ловится замером §6.4; (3) латентность от стрим-рендера не должна отличаться от блокирующего
рендера (тот же один вызов) — проверяется регрессией §10.2.
