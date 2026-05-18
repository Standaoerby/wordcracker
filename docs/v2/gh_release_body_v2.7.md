# wordcracker v2.7 — failed-query log в админке

Stan хочет видеть «что пользователи спрашивали, на что мы не смогли
ответить» с разбором по причинам. v2.7 добавляет логирование
clarify/OOS ответов и admin UI для их просмотра.

## Что нового

### Логирование failed queries

`rag_v2.ask()` теперь пишет в observability ring buffer (и JSONL) для
**всех** ответов, не только tool-driven. Когда `plan.needs_clarify` или
`plan.out_of_scope_reason` сработали — пишется запись с полями:

- `question_truncated` — обрезанный текст запроса (300 chars)
- `intent` — финальный intent после rules + history + LLM fallback
- `original_intent` — если OOS интент перекрыл другой (например
  `period_vocab + gender` → `out_of_scope`)
- `is_failure: true`
- `failure_kind`: `clarify` / `out_of_scope`
- `failure_reason` — из `plan.explain` или OOS-reason
- `answer_truncated` — что user реально увидел

### Admin UI

`/failed` на admin.slovoeb.net — таблица с фильтром:

```
time          kind    intent                  query                  answer    reason
20:14:35     clarify  author_vocab            фирменные слова Поэта  Для этого нужен автор  no specific reason
20:13:02     oos      out_of_scope            процитируй полностью   полнотекстовый анализ невозможен  ...
```

- Фильтр по `kind` (clarify / out_of_scope / all)
- Newest first, 100 rows max из ring buffer
- Auto-refresh every 15 s
- Доступ через ссылку «→ failed queries log» с upload-страницы
- JSON endpoint `/api/failed` для tooling

### Helper API

`scripts.v2.observability.recent_failures(limit=100)` — for any custom
admin tooling.

## Прогон

- **Unit tests: 227/227** (+3 для recent_failures)
- **40-question vault dry-run: 31 tool / 1 intro / 6 OOS / 2 clarify** (=
  baseline)
- All v2.6 fixes still pass (нет regression от v2.7 изменений — это
  чистая телеметрия + UI)

## Что НЕ в v2.7 (Sprint 13 backlog)

- Live dashboard на status.slovoeb.net с graph «failed-rate over time»
  (сейчас только current ring buffer view)
- Auto-classification failed queries — какие можно конвертить в новые
  regex rules
- Failed-query export to CSV / Anki (если Stan захочет тренировочный
  dataset)
- POS-tag accuracy в learning_words (20% errors per round 2)
- Hallucinated signature words (Q13 Lovecraft round 2) — needs render
  prompt tweak

Co-developed with Claude Opus 4.7 (1M context).
