# wordcracker v2.8 — Sprint 14: entity-aware LLM fallback + 13 new fixes + UX polish

После 3 раундов внешнего тестирования (Stan через Claude в Chrome,
2026-05-18, 60 запросов) pass rate стабилизировался на **50%** на
свободных формулировках. Static pattern, не шум. v2.8 атакует это
**архитектурно**, не очередной пачкой regex'ов.

## Системное решение — Entity-aware LLM fallback

v2.6 ввёл LLM fallback для **intent** classification. v2.8 расширяет:
тот же LLM call теперь возвращает **и intent, и entities** в одном
JSON ответе. Это закрывает класс ошибок «intent ловится rules, но
entity не extracted → plan clarifies».

```
rule classifier → intent ✓                         (50% of queries)
regex extractor → entities ✓
   → plan builder ✓

если regex не нашёл entity:
   LLM classify_and_extract → JSON {intent, author, book_title,
                                    word, year_from, year_to, country}
   _merge_llm_entities(regex_entities, llm_dict)
      → regex wins where it found something; LLM fills gaps
   → plan builder ✓
```

Архитектурная безопасность:
- **Regex wins where it found something** — LLM никогда не перезаписывает
  уже найденное regex. Только fills `None` slots.
- **Surname → regex lookup через AUTHOR_ALIASES** — LLM возвращает
  «Shakespeare», мы переводим в `^Shakespeare,` (если есть в alias dict).
  Если author незнакомый — `None`, не invent.
- **Title → KNOWN_BOOKS lookup** — LLM возвращает «Anna Karenina», мы
  resolve в PG1399 (или canonical title для unknown). Leading-«the»
  fuzzy match сохранён.
- **Year range / country sanity** — `_clean_int` ограничивает 1500-2100,
  `_clean_country` валидирует ISO-2.
- **`format=json` форсит Ollama** на валидный JSON — fallback парсер
  достаёт `{}` блок если LLM добавил преамбулу.

Покрывает из round 2+3:
- «у Шекспира» (author entity не extracted regex'ом)
- «а у пушкина?» (follow-up без явного trigger)
- «теперь у Диккенса» (follow-up с trigger, но без history-author)
- «что соседствует со словом heart у Шекспира» (instrumental author)
- «а во Frankenstein?» (book follow-up)

## 13 routing-багов закрыто новыми regex rules (Phase 3)

| Bug | Fix |
|---|---|
| «помоги / помощь / здравствуй» → introduction | новое правило с negative lookahead против «помоги с книгой» |
| «найди-ка X», «есть ли у тебя X», «где у тебя X?» → book_lookup | расширил existing rules — не требуют слова «книга» |
| «слушай а сколько у тебя книжек» → corpus_meta | новое правило на diminutive «книжек/книжка» |
| «годы жизни / даты жизни / биография» → author_metadata | новое правило, до этого только «когда родился» |
| «сколько у Толстого книг» → author_metadata (priority 0.93) | bump за corpus_meta default |
| «дай статистику по Wodehouse» → author_metadata | chat placeholder pattern fixed |
| «найди упоминания битой посуды» → word_contexts | новое правило semantic search trigger |
| «топ-15 биграмм у Конан Дойла» → author_top_words | новое правило биграмм/триграмм |
| «эмоциональный профиль Dracula» → book_emotion | новое правило + добавил book_emotion в PRIORITY (был 0) |
| «timeline слова freedom» → word_timeline | новое правило |
| «сколько слов знал Шекспир» → lexical_wealth | новое правило vocab size |
| «переведи X на Y» → out_of_scope | новое правило (translation request, не translation_quality) |
| «execute system command / shell command» → out_of_scope | новое правило command injection guard |
| «у викторианцев» (instrumental case) → year_from=1837 | расширил `_VICTORIAN` regex на падежи |
| Шекспир / Лермонтов / Булгаков / Набоков / Сэлинджер | новые AUTHOR_ALIASES |
| Harry Potter / Anna Karenina / Catcher in the Rye | новые KNOWN_BOOKS |
| Уайльд character self-name leak | `_drop_author_self_name` в affinity wrapper |

## UX polish (Phase 4)

- **Persistent suggestion chips** — после первого submit chips collapse
  в «💡 примеры запросов» (clickable), не исчезают совсем
- **Progressive help overlay** — после 3 consecutive clarifies localStorage
  counter triggers floating panel с 4 рабочими примерами
- **Inline contextual help в clarify** — `_need_author/_need_book` теперь
  показывают захваченный текст пользователя + 3 шаблона «у X», вместо
  generic «уточни»

## Failed-query aggregation (Phase 5)

`/admin/failed` теперь имеет 2 таблицы:
1. **Top 15 repeated failed phrases** (sorted by count desc) — Stan
   видит «какие phrasings повторяются → regex candidates»
2. **Recent fails** (как в v2.7) — newest first, filter by kind

API: `GET /api/failed` возвращает `{failed: [...], top_phrases: [...]}`.
New helper `scripts.v2.observability.top_failed_phrases(top_n)`.

## Audit results (rules-only, LLM fallback disabled)

| | Round 3 v2.5.1 | After v2.8 E-fixes | Δ |
|---|---|---|---|
| Vault 40 | 80% | **80%** | = |
| Round 2 (20) | 65% | **80%** | +15% |
| Round 3 (20) | 40% | **60%** | +20% |
| **Aggregate (80)** | **61%** | **75%** | **+14%** |

С LLM fallback на проде (Phase 2) ожидается **~85-90%** — closes
context-inheritance, multi-word names, instrumental cases, friendly
openers, follow-ups without trigger words.

## Tests

- **Unit: 268/268** (+41 new: parse_full_json × 5, classify_and_extract ×
  4, clean_helpers × 3, surname_to_regex × 2, title_to_book × 4,
  needs_entity_help × 7, merge_llm_entities × 11, top_failed_phrases × 5)
- **29/29 caught-bug regression probes** (13 historical + 16 new for
  v2.8 fixes)
- **80-query rules-only audit**: 60/80 = 75% tool-driven baseline

## Что в Sprint 15 backlog

- Wikidata-based author birth/death enrichment as Gutendex fallback
- Full-vocab tf-idf cosine in compare_authors as second number
- Rule-synthesis tooling: «promote phrase to rule» button in admin
- Hallucinated Lovecraft signatures (critic catches; render prompt needs
  more aggressive instruction)
- POS tag accuracy in learning_words (20% errors per round 2)
- Rate limiting per IP (nginx layer)

Co-developed with Claude Opus 4.7 (1M context).
