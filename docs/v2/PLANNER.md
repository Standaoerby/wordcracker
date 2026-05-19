# wordcracker v2 — Query Planner & Tool Router

> Контракт между LLM-driven dialog и detereministic-tool dispatch.
> Цель: LLM не выбирает tools на каждой итерации. Plan детерминирован.
> LLM остаётся только в (a) clarify ambiguous input, (b) render структурированного результата, (c) out-of-scope refusals.

---

## 0. Failure modes из v1.1.7 которые planner должен решить

| ID | Симптом | Корневая причина | Planner-решение |
|---|---|---|---|
| Q02 | "Rowling characteristic words" → 180s timeout | LLM не знает что Rowling нет в PG, loop без tool calls | Intent `author_vocab`, entity `Rowling` → planner резолвит → not_found → ASK clarify «нет в корпусе, ближайший Доступный?» |
| Q03 | "B2 hard words for Lovecraft" → 180s timeout | LLM не понимает chain learning_words+enrich | Intent `learning`, scope=author, level=B2 → fixed chain learning_words → enrich → render |
| Q04 | Compare Dickens vs Hemingway → partial | LLM плохо роутит compare_authors | Intent `author_compare`, fixed call to compare_authors |
| Q05 | "British words by Christie" → 180s timeout | LLM не знает country filter синтаксис | Intent `author_vocab` + entity country=GB → set FilterSpec.country |
| Q11 | "Compare GB vs US" → отвечает планом без tool | LLM выбирает «think out loud» вместо execute | Router принудительно executes plan, LLM получает результат, не пишет план |
| Q16 | "Words disappearing after 1920" → partial | Coverage недостаточен для bucket | Planner проверяет `BookProfile.has_pub_year` ≥ 50% before run, иначе предлагает birth-year fallback |
| Q17 | Emotion fear → stopwords | Tool quality, не planner | Tool unit test (отдельно) + min_corpus_count filter inside emotion_collocates |
| Q19 | "Translation mistakes" → отказ | Корректное вне scope | Intent `out_of_scope`, краткий отказ |

---

## 1. Pipeline

```
┌──────────────┐
│ user message │
└──────┬───────┘
       ▼
┌──────────────────┐
│ intent classifier │  rules first, LLM fallback
└──────┬───────────┘
       ▼
┌──────────────────┐
│ entity extractor │  regex + name dict + LLM fallback
└──────┬───────────┘
       ▼
┌──────────────────┐
│   plan builder   │  intent + entities → QueryPlan
└──────┬───────────┘
       │
       ├─── clarify needed? ──▶ LLM clarify renderer ──▶ user
       │
       ▼
┌──────────────────┐
│   tool router    │  executes plan step-by-step
└──────┬───────────┘
       ▼
┌──────────────────┐
│  result renderer │  LLM gets ToolResult, writes summary
└──────┬───────────┘
       ▼
       user
```

---

## 2. Intent Classifier

### 2.1 Intent taxonomy

| Intent | Triggers (RU/EN) | Tool chain |
|---|---|---|
| `corpus_meta` | "сколько книг", "что у тебя за корпус", "how many books" | corpus_overview |
| `author_metadata` | "когда родился X", "сколько у X книг", "books by X" | author_metadata |
| `author_vocab` | "фирменные слова", "характерные", "signature words", "what makes X different" | affinity_by_author |
| `author_compare` | "сравни X и Y", "X vs Y", "compare X and Y" | compare_authors |
| `author_attribution` | "определи автора", "кто автор", "stylometry of this text" | author_attribution |
| `author_influences` | "влияния", "похожие авторы", "writers similar to X" | author_influences |
| `book_vocab` | "фирменные слова в книге", "что отличает книгу" | find_book → affinity_by_book |
| `book_readability` | "уровень сложности", "CEFR книги", "how hard is X to read" | find_book → book_readability |
| `book_archaic` | "архаизмы в", "устаревшие слова в", "old-fashioned words" | find_book → book_archaic_words |
| `book_emotion` | "эмоциональный профиль книги", "тон книги" | find_book → book_emotion_profile |
| `word_contexts` | "приведи примеры", "как X использует слово Y", "examples of word Z" | word_contexts / word_contexts_global |
| `word_collocates` | "соседи слова", "collocates", "слова рядом с" | word_collocates |
| `word_timeline` | "когда слово появилось", "history of word", "слова после 1920" | word_freq_timeline / words_disappearing_after |
| `word_pos` | "как используется слово (NOUN/VERB)", "polysemy of" | word_pos_distribution |
| `word_etymology` | "этимология", "происхождение слова", "germanic/french words" | word_etymology / find_words_by_etymology |
| `word_emotion` | "слова страха/гнева", "fear words", "anxious vocabulary" | emotion_collocates |
| `learning` | "слова для изучения", "B1/B2/C1", "vocab to learn" | learning_words → enrich → export |
| `top_authors_books` | "топ авторов", "самые скачиваемые", "most popular X" | top_authors_by / top_books_by_* |
| `country_filter` | "британские/американские слова", "BrE vs AmE" | top_authors_by_country + author_vocab |
| `author_lookup` 🆕v3.0 | "какие книги у X", "what books does X have" | author_metadata (sample_titles) |
| `book_extremum` 🆕v3.0 | "самая длинная / популярная книга", "the longest book" | top_books_by_downloads(top=1) or clarify |
| `corpus_extremum` 🆕v3.0 | "самый плодовитый / популярный автор" | top_authors_by(top=1) |
| `topic_book_search` 🆕v3.0 | "найди книгу про X", "book about Y" | find_book_by_topic (BGE rerank default) → hybrid_search dedupe |
| `book_pub_year` 🆕v3.0 | "когда была опубликована X", "year of publication" | find_book → pub_year from OL enrichment |
| `book_readability_compare` 🆕v3.0.1 | "что сложнее читать X или Y" | book_readability × N (multi-book) |
| `book_similar` 🆕v3.0.1 | "похожие на X", "продолжение X", "что почитать после X", "similar to X" | find_book_by_topic (BGE rerank default) |
| `similar_to` 🆕v3.0.2 | "в стиле X" (book/author ambiguous) | plan-time disambiguate: book → book_similar, author → author_closest |
| `out_of_scope` | "напиши рассказ", "переведи стих", "что в новостях" | → refusal renderer |
| `clarify` | LOW confidence на classifier | → clarify renderer |

**Sprint 16 (v3.0) intent priorities** — placed above the generic
catch-alls so specific phrasings win:

```python
"author_lookup":            160,  # «какие книги у X» — wins over author_metadata (55)
"book_extremum":            158,  # singular superlative
"corpus_extremum":          155,  # singular author superlative
"book_readability_compare": 152,  # «сложнее читать X или Y» (Sprint 17)
"vocab_passport":           150,  # (existing)
"book_pub_year":            148,  # «когда вышла X» — wins over book_lookup (122)
"book_similar":             146,  # «похожие на X», «что почитать после X» (Sprint 17)
"topic_book_search":        145,  # «найди книгу про X» — wins over book_recommendation (118)
"similar_to":               130,  # ambiguous «в стиле X» router (Sprint 18)
```

### 2.2 Rules engine

```python
INTENT_RULES = [
    # (regex_or_keyword_set, intent, confidence_boost)
    (re.compile(r"(сколько|how many) книг"), "corpus_meta", 0.9),
    (re.compile(r"(фирменные|характерные|signature) слова"), "author_vocab", 0.8),
    (re.compile(r"(сравни|compare|vs\b|против)"), "author_compare", 0.7),
    (re.compile(r"(этимол|origin|germanic|french|латинск)"), "word_etymology", 0.8),
    (re.compile(r"(b1|b2|c1|c2|level|уровень).*(слов|word|вокаб)"), "learning", 0.75),
    (re.compile(r"(написать|напиши|сочини|стихотворен|рассказ)"), "out_of_scope", 0.95),
    # ...
]

def classify_intent(q: str) -> tuple[str, float]:
    matches = [(intent, score) for pat, intent, score in INTENT_RULES if pat.search(q.lower())]
    if not matches:
        return ("clarify", 0.0)
    return max(matches, key=lambda x: x[1])
```

### 2.3 LLM fallback

При confidence < 0.5 → minimal LLM call с предписанной taxonomy:

```python
def llm_classify(q: str) -> str:
    """Single-shot LLM call. Returns intent label or 'clarify'."""
    prompt = f"""
Classify the following user question into ONE of these intents (return just the label):
{INTENT_TAXONOMY_BULLETS}

User question: {q}

Intent:
"""
```

Гейтится `WC_PLANNER_LLM_FALLBACK=on`.

---

## 3. Entity Extractor

### 3.1 Entity types

```python
@dataclass
class Entities:
    author: Author | None              # canonical record or None
    book: CanonicalBook | None
    word: str | None
    year_from: int | None
    year_to: int | None
    country: str | None                # ISO-2
    level: Literal["basic", "intermediate", "advanced", "rare"] | None
    emotion: str | None                # fear/joy/etc
    pos_filter: list[str] | None
    raw_misc: dict                     # everything else
```

### 3.2 Extraction strategies

| Field | Strategy |
|---|---|
| `author` | (1) `^Surname,` regex literal — extract surname → metadata fuzzy match; (2) known-aliases dict («Конан Дойл» → `^Doyle,`); (3) LLM fallback |
| `book` | (1) `PG\d+` literal; (2) title in quotes — `find_book(title)`; (3) LLM fallback |
| `word` | (1) word in quotes; (2) word after `слово/word ` ; (3) LLM extract |
| `year_*` | `(?:18\|19\|20)\d{2}` near keywords «после/до/between/после/before» |
| `country` | keyword dict ("британский"→GB, "американский"→US) |
| `level` | `(B1\|B2\|C1)` regex + keyword («средний/intermediate») |

### 3.3 Resolution

- Author / Book resolution дёргают `MetadataResolver`.
- Если author не найден в корпусе → `Entities.author = None`, planner ставит `clarify_needed`.

---

## 4. Plan Builder

### 4.1 QueryPlan

```python
@dataclass
class QueryPlan:
    intent: str
    entities: Entities
    steps: list[PlanStep]
    fallback_steps: list[PlanStep]
    expected_cost: Literal["cheap", "medium", "heavy"]
    needs_clarify: bool
    clarify_question: str | None
    explain: str                       # human-readable «что я собираюсь сделать»

@dataclass
class PlanStep:
    tool: str
    args: dict                         # already in v2 schema, includes FilterSpec dict
    depends_on: list[int] = []         # indices of prior steps
    inject_result_as: str | None = None  # how to thread output to next step
    optional: bool = False             # skip if previous step empty
```

### 4.2 Plan templates по intent

#### author_vocab
```python
def build_author_vocab_plan(e: Entities) -> QueryPlan:
    if not e.author:
        return ask_for_author()
    fs = FilterSpec(
        author_regex=e.author.regex,
        country=e.country,
        year_from=e.year_from,
        year_to=e.year_to,
        pos_filter=e.pos_filter,
        min_corpus_count=auto_min_corpus_count(e),
        exclude_proper_nouns=True,
    )
    return QueryPlan(
        intent="author_vocab",
        entities=e,
        steps=[PlanStep(tool="affinity_by_author", args={"filter": fs.to_dict(), "top": 30})],
        fallback_steps=[],
        expected_cost="medium",
        needs_clarify=False,
        clarify_question=None,
        explain=f"Найду фирменные слова автора {e.author.display} по affinity-метрике.",
    )
```

#### book_vocab
```python
def build_book_vocab_plan(e: Entities) -> QueryPlan:
    if e.book:
        # already resolved
        return single_step("affinity_by_book", {"pg_id": e.book.book_id, ...})
    # need find_book first
    return QueryPlan(
        intent="book_vocab",
        entities=e,
        steps=[
            PlanStep(tool="find_book", args={"title": e.raw_misc.get("title")}),
            PlanStep(tool="affinity_by_book", args={...},
                     depends_on=[0], inject_result_as="pg_id"),
        ],
        ...
    )
```

#### learning
```python
def build_learning_plan(e: Entities) -> QueryPlan:
    if not (e.author or e.book):
        return ask_for_scope()
    fs = FilterSpec(...)
    return QueryPlan(
        intent="learning",
        steps=[
            PlanStep(tool="learning_words", args={"scope": fs.to_dict(), "level": e.level, "top": 30}),
            PlanStep(tool="bulk_enrich_words", args={...}, depends_on=[0]),  # batched
        ],
        expected_cost="heavy",
        ...
    )
```

### 4.3 Heavy cost gating

Если `expected_cost == "heavy"` И coverage не подтвержден → planner может:
- (a) выполнить cheap probe (`corpus_stats_by_author` чтобы убедиться что author present) перед heavy step;
- (b) если probe fails — ask clarify.
- (c) async-defer в background job (только для extreme heavy типа build_index full reindex).

---

## 5. Tool Router

### 5.1 execute()

```python
def execute(plan: QueryPlan, *, observability: bool = True) -> RouterResult:
    if plan.needs_clarify:
        return RouterResult(kind="clarify", question=plan.clarify_question)

    results: list[ToolResult] = []
    for step in plan.steps:
        # inject prior result
        args = _resolve_args(step.args, results, step.depends_on, step.inject_result_as)
        tr = registry.dispatch(step.tool, args)
        results.append(tr)
        if not tr.ok and not step.optional:
            return _handle_error(plan, step, tr, results)

    return RouterResult(kind="results", plan=plan, results=results)
```

### 5.2 Error handling

- Если step fails `retryable=True` — попробовать `fallback_steps`.
- Если non-retryable — собрать partial results, перейти в renderer с warning.
- Никогда не loop > 7 iterations.

### 5.3 No LLM in router

Router детерминирован. LLM не выбирает следующий step. Это устраняет «narrating plan without execution» (Q11).

---

## 6. Result Renderer

### 6.1 Modes

```python
class RenderMode(Enum):
    SHORT     = "short"         # 1-2 sentence summary
    DETAILED  = "detailed"      # full markdown table + explanation
    EXPORT    = "export"        # download CSV/Anki
    DEBUG     = "debug"         # tool trace + raw JSON
```

UI exposes toggle.

### 6.2 LLM prompt structure

```
Ты рендерер результата. Тебе пришёл:
- intent: {intent}
- explain: {plan.explain}
- tool_results: {ToolResult.to_llm_string() для каждого}
- coverage_warnings: {агрегированные warnings}

Напиши:
1. Краткий summary (1-2 предложения).
2. Markdown-таблица с данными (если есть).
3. 1-2 follow-up предложения в формате «можно дальше спросить...».
4. Если coverage низкое — упомяни это.

НЕ ВЫДУМЫВАЙ цифры и имена.
```

LLM не имеет доступа к tools на этом шаге. Только формат данные → текст.

### 6.3 Refusal renderer (out_of_scope)

Fixed templates, без LLM:

```
Я аналитик корпуса Project Gutenberg, не генератор. Не пишу художку.
Могу показать [предложить 2-3 ближайших intent].
```

---

## 7. Integration с current API

### 7.1 Feature flag

`/api/chat/stream?engine=v2` или header `X-WC-Engine: v2`.

### 7.2 SSE events совместимы с v1

v2 emits all v1 events (`start/iter/tool_call/tool_result/answer/done`) **плюс**:
- `intent` — early signal что planner определил
- `plan` — full QueryPlan для debug panel
- `clarify` — если planner просит clarify

Front-end игнорирует unknown events → backward compatible.

### 7.3 Старый rag_query.ask()

Остаётся как `?engine=v1`. Удалим после 1 недели стабильной работы v2 в production.

---

## 8. Test strategy

### 8.1 Unit (`tests/v2/unit_*.py`)

- `test_intent.py`: 60 examples → expected intent label (regex rules only, no LLM).
- `test_entities.py`: 40 examples → expected extracted entities.
- `test_plan.py`: для каждого intent ожидаемые plan steps.
- `test_filter_spec.py`: edge cases (empty, country only, year only).
- `test_tool_result.py`: serialization roundtrips.

### 8.2 Integration (`tests/v2/integration_*.py`)

- `test_routing_q01_q20.py`: каждый из 20 примеров → правильный plan → tool returns valid ToolResult.
- `test_metadata_resolver.py`: real metadata_df, fuzzy matches.
- `test_cache.py`: hit/miss/stale paths.

### 8.3 LLM behavior (`tests/v2/llm_*.py`, RUN_LLM_TESTS=1)

- `test_q01_q20_full.py`: real qwen3, real SSE, end-to-end.
- `test_clarify.py`: ambiguous inputs → expected clarify question.

---

## 9. Rollout

1. Sprint 1+2 closed → v2 engine на staging endpoint (slovoeb-v2.net) — отдельный CF tunnel.
2. Stan тестит вручную против v1, обнаружим regressions.
3. После 1 недели — flip default к v2, v1 как fallback.
4. После 2 недель — удаляем v1 code, merge в main.

---

## 10. v4 — Hybrid LLM planner (Sprint 20)

После v3.1.1 стала очевидна **граница rules-based архитектуры**: 203
regex правила, 44 plan-функции, 18 hand-coded branches в
`_smart_clarify_recipe`. Каждый Stan-screenshot добавляет правило.
Naive-user pass-rate упёрся в ~30%, research-user — в ~60–70%
(«сервис работает если ты знаешь как его прошепнуть»).

**v4 hybrid pipeline** добавляет **LLM-планнер как slow-path** для
запросов, которые rules-based path сбрасывает в clarify. Это **не**
v1 agentic loop — один LLM-call (не iterative), strict JSON output,
deterministic execution через router.

```
user query
  │
  ▼
intent classifier (203 rules + LLM fallback)      ◀ v3 fast path, ≤50ms
  │
  ├── matched → entity extractor → plan.build()
  │            │
  │            ├── needs_clarify ──┐
  │            │                    │
  │            └── ok → router.execute(plan) → renderer ▶ user
  │                                   │
  ▼                                   │
                                       │
v4 LLM planner (slow path, ~1-2s)   ◀ v4: fires only on v3 clarify
  ├── prompt = tool catalog + few-shot examples
  ├── Ollama format=json → PlanSpec JSON
  ├── validate (tool existence, args, $-refs, cycles)
  └── retry once on invalid output → fallback clarify
       │
       ▼
PlanSpec → router.execute_spec(spec) → renderer ▶ user
```

### 10.1 PlanSpec — typed DAG

LLM emits a `PlanSpec` JSON object:

```json
{
  "intent_hint": "etymology_ratio_compare",
  "rationale": "compare germanic vs latinate signature words per book",
  "steps": [
    {"id": "s1", "tool": "resolve_book_title",
      "args": {"query": "Beowulf"}},
    {"id": "s2", "tool": "resolve_book_title",
      "args": {"query": "Paradise Lost"}},
    {"id": "s3", "tool": "find_words_by_etymology",
      "args": {"scope": {"book": "$s1.pg_id"},
                "family": "germanic", "top": 20},
      "needs": ["s1"]},
    {"id": "s4", "tool": "find_words_by_etymology",
      "args": {"scope": {"book": "$s2.pg_id"},
                "family": "germanic", "top": 20},
      "needs": ["s2"]}
  ],
  "render_hint": "etymology_ratio_table"
}
```

Key features:
- **`$sN.field` references** — router resolves at exec time.
  Supports nested dicts (`$s1.matches.0.id`) and arbitrary depth.
- **`needs` declares deps** — combined with discovered $-refs to build
  topological order. Cycles fail validation.
- **`clarify` is a valid terminal** — when LLM genuinely can't plan,
  it emits `{"clarify":"<question>"}` instead of inventing steps.

See `scripts/v2/planner/plan_spec.py` for the dataclass + validator.

### 10.2 Tool catalog — single source of truth

`scripts/v2/planner/tool_catalog.py` serializes the registry into a
compact prompt block (≤25KB). Adding a new `@tool` automatically
makes it available to the planner — **zero prompt edits required**.
Few-shot examples live alongside in code, organized by query
pattern type (lookup / compound / triangulation / etymology-ratio /
genuinely ambiguous).

### 10.3 Entity resolvers — `resolve_author_name` / `resolve_book_title`

New v4 tools that turn free-text phrasings into canonical
`author_regex` / `pg_id`. Layered: curated alias dict → fuzzy match
against SPGC metadata. The LLM planner uses them as the first step
of any plan that touches entities, so new authors / books propagate
without code changes.

### 10.4 Router DAG — `execute_spec` / `execute_spec_stream`

Router now has a v4 entry point. Topological sort by `needs` +
discovered $-refs. Each step's args are resolved against prior
results before dispatch. Failure handling, optional-step semantics,
and the SSE stream mirror the v3 path exactly so chat_server doesn't
need a protocol fork.

### 10.5 Validation pipeline

Before execution, every PlanSpec passes through
`plan_spec.validate(plan, registry=REGISTRY)`:

| Check | Severity |
|---|---|
| tool exists in registry | error |
| required args present (or come from $-ref) | error |
| `needs` references existing step ids | error |
| no cycles | error |
| $-ref targets known step | error |
| step id matches `/^s\d+$/` | error |
| step count ≤ `max_steps` (12) | error |
| heavy-cost tool count ≤ `max_heavy` (6) | error |
| arg not in `input_schema.properties` | warning |

Validation runs both at LLM-output parse time (planner retries on
failure) and as a defence in depth in the router.

### 10.6 Feature flag

`WC_LLM_PLANNER=on` enables the v4 path. **Off by default during
alpha rollout** — the v3 path is completely unchanged when the flag
is off. Stan can flip the flag per environment without code
changes.

### 10.7 Observability

Every v4 plan attempt logs:
- `v4_planner_used: true|false`
- `v4_planner_attempts: 1|2` (retry count)
- `v4_planner_elapsed_s: float`
- the full PlanSpec is stored in the JSONL log so Stan can review
  what the LLM generated for each query.

The log is the **input dataset for promotion**: when a v4 plan
pattern recurs, it can be promoted to a v3 rule (or stay in v4 if
the pattern is too varied for regex).

### 10.8 Why v4 will NOT regress to v1

| | v1 (broken) | v4 (correct) |
|---|---|---|
| LLM calls per query | 1–5 iterations with drift | 1 plan emit + 1 render + 1 critic |
| LLM picks tool in loop | yes | no — plan emitted ONCE |
| Output format | free text + heuristic parse | strict JSON via `format=json` |
| Hallucinated tool | runs (silently broken) | fails validation, never dispatched |
| Trace visible | no | PlanSpec in observability log |
| Critic / numeric audit | absent | unchanged from v3 |
| Fallback | timeout fail | rules → LLM planner → graceful clarify |

---

## 11. v4 — Implementation map

| Module | Responsibility |
|---|---|
| `planner/plan_spec.py` | PlanSpec dataclass, JSON roundtrip, $-ref resolver, validator, topo sort |
| `planner/tool_catalog.py` | Catalog builder, prompt assembly, few-shot examples |
| `planner/llm_planner.py` | Ollama call + retry + cache, returns `PlannerResult` |
| `planner/router.py` (extended) | `execute_spec` / `execute_spec_stream` for PlanSpec DAGs |
| `tools/meta/resolve_entity.py` | `resolve_author_name`, `resolve_book_title` @tool wrappers |
| `rag_v2.py` (extended) | Calls LLM planner on v3-clarify with `WC_LLM_PLANNER=on` gate |

**Tests (`tests/v2/`):**
- `test_v4_plan_spec.py` — 26 tests (parsing, $-refs, validator, topo)
- `test_v4_tool_catalog.py` — 13 tests (catalog + examples + prompt)
- `test_v4_llm_planner.py` — 13 tests (Ollama mocked; retry, cache, history)
- `test_v4_router_dag.py` — 11 tests (DAG ordering, failure modes, SSE)
- `test_v4_resolve_entity.py` — 15 tests (curated aliases + tool registry)
- `test_v4_rag_integration.py` — 3 tests (end-to-end with all mocks)

Total: **81 new tests, 718 in suite, 0 failures.**
