# wordcracker

ИИ-компаньон для чтения и изучения языка над корпусом Project Gutenberg
(~55 000 книг). Вопрос на естественном языке (RU/EN) про авторов, слова, стиль
и эпохи → ответ **обоснован реальными текстами, а не выдуман**. Стилометрия
(keyness, Burrows Delta), конкорданс и коллокации, диахрония, этимология,
учебная лексика по уровням CEFR с экспортом в Anki, hybrid retrieval
(FTS5 + semantic). Для лингвистов и изучающих английский. Хост — RTX 3090 /
Ubuntu 24.04 в Docker.

**Live:** <https://slovoeb.net> (chat), <https://status.slovoeb.net> (health
dashboard), <https://admin.slovoeb.net> (upload новых книг). Сервис в закрытой
бете (Cloudflare-гейт).

---

## v2.7.38 (2026-06-25) — текущая версия

Линейка 2.7.x (июнь 2026): **веб-слой** (FastAPI + React поверх того же
v2-движка), **вертикаль честности** (система не сообщает ничего, чего нет
в данных инструментов, — на уровне промпта, критика и детерминированных
санитайзеров), **learning-интенты** (рекомендация книг по уровню CEFR,
учебные списки с переводами, fail-fast вместо 40-секундных отказов) и
**точность стилометрии** — структурные NER-щиты в живом пути не дают именам
и топонимам протекать в keyness и архаизмы (2.7.35–2.7.37: book_archaic
precision + author-keyness щит против «dunwich-leak», с добором сигнатурных
слов на полном списке кандидатов).

```
user → input caps (64KB / 4K chars / 50 turns) + control-char strip
     → intent classifier (rules-based: приоритеты, confidence,
       injection-гарды; learning/meta-паки 2.7.6+)
     → entity extractor (90+ aliases, KNOWN_BOOKS, paired-quote regex)
     → history.merge (multi-turn entity backfill + intent inference)
     → plan builder (templates, @_with_copyright_check; fan-out
       композиции через pg_id@rank / word@rank инжекции)
     → tool router (no LLM in loop; детерминированный executor)
     → renderer (single LLM call, low temp; rule 21: никаких чисел
       и операций без опоры в tool-данных)
     → honesty-пасс: numeric audit (repair/excise + 🔧-дисклоз),
       claimed-vs-shown (заявленный фильтр сверяется с фактическими
       строками таблиц), render_sanitizer (служебные ноты не доходят
       до клиента даже в стриме)
     → critic (second low-temp pass; table-aware payload,
       evidence-фильтр клеймов)
     → answer + UI badges + плашки честности
```

### Вертикаль честности (2.7.2–2.7.10)

Ответ сервиса подчиняется контракту: **каждый факт либо опирается на данные,
либо честно помечен как сгенерированный, либо отсутствует с объяснением.**

- числа без опоры в tool-выходе чинятся или вырезаются (🔧-плашка);
- заявление «после фильтрации имён» сверяется с фактическим содержимым
  таблицы тем же детектором, которым фильтруют (gazetteer + патронимы
  + cap-ratio по корпусу); частичный фильтр обязан называться частичным;
- учебные бандлы: этимология — только из Wiktionary-данных
  (`word_etymology`), примеры — только корпусные цитаты, переводы несут
  кэвиат «сгенерирован моделью, не словарём», имена собственные и топонимы
  перевода не получают;
- внутренние render-инструкции детерминированно вычищаются из ответа
  и стрим-дельт.

### Веб-приложение (Sprint 6, 2.7.0)

`wordcracker-api` (:8000, FastAPI + SSE) + React SPA параллельно чату :8890.
Контракт: [docs/webapp.md](docs/webapp.md).

- `POST /api/query` (SSE-стрим), `/api/health` vs `/api/ready`
- `<ResultTable>` из структурных данных, `data-query`-ячейки (клик →
  запрос в чат), тумблер «только данные», живая трасса инструментов
- `POST /api/export/xlsx` — числа остаются числами
- эмбеддер на CUDA с CPU-фолбэком; multi-stage Vite-билд
- follow-up: mem-limit api после замера; `web/package-lock.json`

### Learning-интенты (2.7.6–2.7.9)

- `learning_books`: «какие книги почитать, если у меня уровень B2» /
  «с чего начать» → пул top-книг × `book_readability` (Flesch → CEFR-банд)
  с честной нотой о методе; дефолт — минимальный порог вхождения
- «дай N слов из книги X с переводами» — одна композиция вместо отказа
- meta-пак: «у вас есть <автор>?», «самый популярный автор» и т.п.
- fail-fast: нераспознанное → мгновенный clarify с рабочими примерами
  (вместо 40s LLM-попыток)

## Возможности

Для **лингвиста**: «фирменные» слова автора (keyness G²/log-ratio,
`affinity_by_author`), сравнение словарей (`compare_authors`), стилистические
влияния по Burrows Delta (`author_influences`), атрибуция текста
(`author_attribution`); KWIC-конкорданс (`word_contexts` / `word_contexts_global`),
коллокации (`word_collocates`), POS-распределение для полисемии
(`word_pos_distribution`), слова вокруг эмоции (`emotion_collocates`); диахрония —
частотная кривая (`word_freq_timeline`), нео-/устаревающие слова
(`words_appearing_after` / `words_disappearing_after`), архаизмы книги
(`book_archaic_words`); этимология (`word_etymology` и `find_words_by_etymology`:
germanic/norse/romance/greek/celtic/slavic/arabic/pie); характерные n-граммы,
лексическое разнообразие (TTR), семантический поиск.

Для **изучающего английский**: учебная лексика из книги/автора по уровню CEFR
(`learning_words`: basic/intermediate/advanced/rare) с экспортом в **Anki**
(`export_word_list`); живые примеры употребления (`word_contexts`); подбор книги
под уровень (`book_readability` + `book_archaic_words`); рекомендации чтения
(`find_book`, `semantic_search`, `top_books_by_downloads` / `top_books_by_recency`);
глоссы и определения (`enrich_word`).

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
| LLM | Ollama + `wordcracker:v2` (Qwen3:14b base, num_ctx 8192) |
| Embedder | paraphrase-multilingual-MiniLM-L12-v2 (GPU) + BGE-reranker |
| Web | FastAPI + uvicorn (SSE) · React + Vite + TS (zustand) |
| ChromaDB | 1.5.9 · SQLite FTS5 для lexical retrieval |

## Tests & contracts

```bash
python -m pytest tests/v2/        # ~2400 тестов
python -m pytest tests/webapp/    # 51, без Ollama/GPU
```

- **Golden fixtures + FixtureFreshnessGate**: каждый инструмент имеет
  записанную фикстуру и AST-fingerprint; CI краснеет на любом дрейфе
  кода относительно записанного поведения. Рестамп — только на
  прод-хосте (нужен корпус), поток R-RESTAMP документирован.
- **R2-дисциплина**: негативные тесты обязательны; новые ассерты
  подтверждаются падением на до-фиксовом коде.

## Deploy

Один скрипт на деплой-хосте:

```bash
git pull --ff-only && bash scripts/deploy.sh
```

Внутри: dirty-tree-проверка по COPY-скоупу Dockerfile → сборка образа
с тегом по SHA → захват rollback-таргета → `compose up --force-recreate`
(gutenberg-lab chat admin api) → верификация образов и `/health.git_sha` →
**12-probe гейт** (error-taxonomy E1–E12; регрессия PASS→FAIL vs baseline
блокирует и откатывает; пара хронически «холодных» проб — advisory) →
advisory re-record фикстур → prune старых тегов (keep 5).

CI на каждом PR: mandatory version-bump, probe-config sanity,
полный pytest-свип (R10 collect + run) с FixtureFreshnessGate.

## Roadmap (R-28/R-29)

- Словарный derived-ресурс (Wiktionary/FreeDict extract) — переводы
  с tool-опорой вместо LLM-кэвиата
- Derived-кэш readability топ-500 книг — learning_books из MVP-пула
  в полноценную рекомендацию
- Semantic discovery: «мрачные авторы» → emotion-агрегация, жанровый
  фолбэк на bookshelves
- Status-панель v2 (единый /health-контракт с деплоем)
- Retrieval-perf (холодный старт semantic-стора)
- Чек-лист публичного запуска

## История

| Версии | Дата | Что |
|---|---|---|
| **2.7.38** | **2026-06-25** | docs: продуктовое описание README + раздел «Возможности» (лингвист / изучающий), синхронизация версии |
| **2.7.37** | **2026-06-23** | author-keyness propn shield: структурный NER-щит в живом пути `affinity_by_author`/`compare_authors` (union `_book_propn_set` по книгам автора, кэш per-slug) против «dunwich-leak»; фикс over-drop — set-дропы применяются к полному ранжированному списку до усечения POS-пула, малый `top` больше не обнуляется; деплой прогревает `author_propn_cache` перед probe-gate |
| **2.7.36** | **2026-06-21** | book_archaic precision #2: `_book_propn_set` покрывает всю книгу (head/middle/tail-окна + версионированный кэш) — поздне-книжные топонимы (`galatz`) ловятся NER-ом структурно; seed-архаизмы исключены из propn-дропа (возврат `art`); починка затирания pass-2 в миграции кэша |
| **2.7.35** | **2026-06-21** | book_archaic precision: чистка seed-словаря (amongst/amidst/ought/…), proper-noun NER-гейт (galatz/varna/bistritz), архаичное СЛОВО vs устаревший РЕФЕРЕНТ в enrich-промпте, честная подпись охвата |
| **2.7.9–2.7.10** | **2026-06-11** | learning-polish (low-temp render, table-aware critic payload) + честный учебный контент (этимология/примеры только из данных, propn-гейт переводов) |
| 2.7.6–2.7.8 | 2026-06-11 | learning_books + meta-пак + fail-fast; words+translations композиция; books-vs-authors дизамбигуация |
| 2.7.4–2.7.5 | 2026-06-11 | honest filtering: propn gazetteer+cap-ratio, claimed-vs-shown критик, numeric table trust |
| 2.7.2 | 2026-06-10 | honest renderer: rule 21, critic repair/suppress, render_sanitizer |
| 2.7.0–2.7.1 | 2026-06-10 | Sprint 6: api+web слой, SSE, xlsx, кликабельные ячейки |
| 2.4–2.6.x | 2026-05-19..06-09 | продакшен-инфраструктура: docker-деплой с гейтом и rollback, golden fixtures + FreshnessGate, inference-стабильность, learning_words corpus fixes |
| v2.0–v2.3.1 | 2026-05-17..18 | детерминированный планировщик, adversarial hardening, critic v1 |
| v1.0–v1.1.7 | 2026-05-15..17 | (legacy) agentic loop |

---

**License:** MIT.
**Контакт:** [github.com/Standaoerby/wordcracker](https://github.com/Standaoerby/wordcracker).
