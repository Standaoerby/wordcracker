# webapp.md — веб-слой wordcracker (S6+)

Сервис `wordcracker-api` (FastAPI, :8000) + React SPA (`web/`). Параллелен
`chat_server` (:8890) — тот же образ и v2-движок, другой HTTP-слой.
Решения: `docs/v2/decisions.md` → D17 (стек), D18 (раздача статики).

## Архитектура

```
Браузер (React SPA, Vite; prod — статика с того же :8000)
   │  fetch POST /api/query   (стрим тела ответа; НЕ EventSource — он GET-only)
   ▼
FastAPI api/main.py  →  scripts/api_loop.stream_answer()
   │  sync-генератор в threadpool; SSE-кадры
   ▼
scripts/v2/rag_v2.ask_stream(stream_render=True, skip_render=data_only)
   │  planner → router → tools → renderer (stream) → critic/audit
   ▼
Ollama НАТИВНЫЙ /api/chat (НИКОГДА /v1 — теряет tool-call дельты) + ChromaDB
```

## Контракт API

### `POST /api/query`

```json
{ "question": "string", "data_only": false }
```

`history` принимается и **игнорируется до S9**; `project_id` появится в S7.
Ответ — `text/event-stream`.

| event | data | смысл |
|-------|------|-------|
| `thinking` | `{"delta"}` | размышления модели; клиент прячет по умолчанию (Q1) |
| `token` | `{"delta"}` | дельта финального ответа (markdown) |
| `tool_call` | `{"name","args"}` | вызов инструмента |
| `tool_result` | `{"name","elapsed","ok"}` | elapsed в секундах |
| `table` | `{"tool","columns","rows","col_meta"?}` | структурные данные; ячейки — нативные скаляры (N1) |
| `trace` | `{"kind",...}` | v2-события (intent/plan/v4_plan/critic/clarify/…) для трасс-блока |
| `error` | `{"message","kind"}` | ошибка; после неё ВСЁ РАВНО придёт `done` |
| `done` | envelope ↓ | **всегда последний кадр** |

**Envelope `done`:**

```json
{
  "query_id": "uuid",
  "answer_md": "...",
  "tables": [ {"tool","columns","rows","col_meta"?} ],
  "entities": [],
  "tool_trace": [ {"name","args","elapsed","ok"} ],
  "data_only": false,
  "stop_reason": "complete | max_iterations | tool_error"
}
```

Правила для клиента:
- **`done` — источник истины**: стримленные `table`-события замещаются
  `envelope.tables`, накопленные `token`-дельты — `envelope.answer_md`
  (критик/аудит дописывают аннотации после стрима).
- `stop_reason` ≠ событие `error`: `max_iterations` → мягкая плашка, не error-UI (Q2).
- Неизвестные `event`/`kind` молча игнорировать (forward-compat).
- `entities` всегда `[]` до S10 — поле есть, парсер не переписывать потом.

### Как читать стрим на клиенте

`fetch POST` → `response.body.getReader()` + `TextDecoder` → буфер →
кадры резать **и по `\r\n\r\n`, и по `\n\n`** (fastapi.sse шлёт `\n`,
sse-starlette — `\r\n`) → из кадра `event:` + склейка `data:`-строк →
JSON.parse → диспатч. Реализация: `web/src/sse.ts`.

### `POST /api/export/xlsx`

```json
{ "tables": [ {"tool","columns","rows"} ], "filename": "wordcracker_export.xlsx" }
```

Файл `.xlsx`, один лист на таблицу (имя = `tool`: ≤31 символ, `[]:*?/\` → `_`,
дубли — `_2/_3`). Числа остаются числами (N1). В S7 переключим на `?query_id=` из БД.

### `GET /api/health` / `GET /api/ready`

- `/api/health` — чистый liveness, **это** docker healthcheck. Ollama НЕ
  проверяет (иначе рестарт Ollama → restart-петля api). (Q3)
- `/api/ready` — readiness: Ollama reachable + эмбеддер прогрет; 503 пока нет.
  Для смоука/оператора, в healthcheck НЕ вешать.

## B105 — кликабельные ячейки

`table.col_meta = {"<колонка>": {"kind": "book"|"word", "id_col": "pg_id"?}}`
(опционально; без него ячейка просто не кликабельна). Фронт ставит
`data-query="book:PG1342"` / `"word:ardour"` и по клику отправляет шаблонный
запрос в чат. Расширенный UX (шаблоны, hover) — S6.5/S7.

## Задокументированные упрощения S6

- **`data_only` = «стоп после первого набора тулов»**: рендер, критик и
  numeric-аудит пропускаются; многошаговые планы не продолжаются. Clarify /
  out-of-scope текст приходит и в data_only (юзер должен видеть вопрос).
- **disconnect** = отмена в пределах ~одного токена: закрытие соединения →
  GeneratorExit → `cancel_event` → Ollama абортит генерацию на следующем чанке.
- **`keep_alive=-1`** пинит qwen3 в VRAM навсегда — ок для выделенной 3090;
  появятся другие модели → пересмотреть на конечное значение.
- **VRAM-бюджет (N2)**: эмбеддер запросов на CUDA с авто-фолбэком на CPU
  (`rag_tools._resolve_embedder_device`); 3090 24GB = qwen3:14b ≈ 11.7GB
  pinned + эмбеддер/reranker ≤ 3GB. **mem-limit api-контейнера не выставлен
  намеренно** — сначала замер RSS/VRAM под реальным `semantic_search`
  (`docker stats` + `nvidia-smi`), цифры в `infra.md`, лимит follow-up-коммитом.
- SPA catch-all отсутствует (один экран); `/api/*` роуты регистрируются до
  маунта статики.

## Dev-цикл

```bash
# бэкенд (нужны fastapi/uvicorn/openpyxl + остальной стек)
uvicorn api.main:app --reload --port 8000
# фронт: Vite dev-сервер :5173, /api проксируется на :8000
cd web && npm install && npm run dev
# typecheck фронта (build её не гоняет — см. package.json)
cd web && npm run typecheck
# тесты веб-слоя (без Ollama/Chroma/GPU)
python -m pytest tests/webapp -q
```

Прод: образ собирает фронт в multi-stage (`Dockerfile` → stage `webbuild`),
FastAPI раздаёт `web/dist` через StaticFiles. nginx-сайдкара нет (D18).

## Smoke

```bash
docker compose ps                      # ollama, chat, admin, wordcracker-api — Up
curl -s localhost:8000/api/health      # {"ok": true, ...} мгновенно
curl -s localhost:8000/api/ready       # 503 → 200 после прогрева
curl -N -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"question":"дай статистику по автору Достоевский"}'        # …→ done
curl -N -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"question":"топ-15 биграмм Достоевского","data_only":true}' # table+done, без token
curl -s -X POST localhost:8000/api/export/xlsx -H 'Content-Type: application/json' \
  -d '{"tables":[{"tool":"t","columns":["a","b"],"rows":[[1,2]]}]}' -o out.xlsx
python -c "import openpyxl; print(openpyxl.load_workbook('out.xlsx').sheetnames)"
```

В логе api при старте: `embedder device: cuda` (или WARNING + cpu-фолбэк).
