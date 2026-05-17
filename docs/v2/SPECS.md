# wordcracker v2 — технические спецификации

> Контракт для tool authors, planner, router, renderer. Меняется только через ADR.

---

## 1. ToolResult

Единый envelope для всех tool returns. Сериализуется в JSON, кладётся в `messages[].content` при tool-role.

### 1.1 Schema (Python dataclass)

```python
@dataclass
class ToolResult:
    ok: bool                          # True если tool отработал без error
    tool: str                         # имя tool, e.g. "affinity_by_author"
    query: dict                       # echo args, normalized
    data: Any                         # payload — dict | list | scalar
    warnings: list[ToolWarning]       # см. 1.2
    coverage: Coverage                # см. 1.3
    source_info: SourceInfo           # см. 1.4
    runtime_ms: int
    cache_hit: bool = False           # was result served from cache
    error: Optional[ToolError] = None # populated only if ok=False
```

### 1.2 ToolWarning

```python
@dataclass
class ToolWarning:
    code: str        # "low_sample" | "stale_cache" | "fallback_used" | "filter_too_strict" | ...
    message: str     # человекочитаемое
    details: dict    # context
```

Примеры:
- `low_sample`: «only 3 books matched, results may not be representative»
- `fallback_used`: «en_core_web_trf unavailable, fell back to en_core_web_sm»
- `stale_cache`: «cache entry from 2026-04-01, before corpus_version 2026-05-17»

### 1.3 Coverage

```python
@dataclass
class Coverage:
    books_matched: int             # сколько книг попало в анализ
    books_total: int               # сколько было в scope
    tokens_analyzed: int | None    # для статистики
    metadata_completeness: dict    # {"pub_year": 0.42, "nationality": 0.83}
```

Coverage решает Q16-style failures — клиент видит «недостаточно данных для bucket».

### 1.4 SourceInfo

```python
@dataclass
class SourceInfo:
    corpus_version: str            # "2026-05-17T15:08" из corpus_meta.json
    chroma_collection: str | None  # "gutenberg-index" если retrieval
    analytics_version: str         # "v2.0.0" — semver tool layer
    spgc_baseline: str             # "SPGC-2018-07-18"
```

### 1.5 ToolError (только при ok=False)

```python
@dataclass
class ToolError:
    type: str        # "invalid_args" | "not_found" | "timeout" | "internal" | "rate_limited"
    message: str
    details: dict
    retryable: bool
```

### 1.6 Serialization rules

- `to_dict()`: drops None fields, дает компактный JSON.
- LLM message content: `ToolResult.to_llm_string()` — abbreviated form, drops `source_info` и `runtime_ms` (planner логирует это отдельно).
- Debug trace: полный JSON.

### 1.7 Backward compatibility

Legacy tools (rag_tools v1.x) возвращают plain dict или `{"error": ...}`. Adapter:

```python
def from_legacy(name: str, raw: Any, runtime_ms: int) -> ToolResult:
    if isinstance(raw, dict) and "error" in raw:
        return ToolResult(
            ok=False, tool=name, query={}, data=None,
            warnings=[], coverage=Coverage(0, 0, None, {}),
            source_info=current_source_info(),
            runtime_ms=runtime_ms,
            error=ToolError("internal", raw["error"], raw, retryable=False),
        )
    return ToolResult(
        ok=True, tool=name, query={}, data=raw,
        warnings=[], coverage=Coverage(-1, -1, None, {}),  # unknown
        source_info=current_source_info(),
        runtime_ms=runtime_ms,
    )
```

Этот adapter позволяет включить v2 dispatcher до того как все 32 tools мигрированы.

---

## 2. FilterSpec

Единый контракт для «по какому подкорпусу анализируем».

### 2.1 Schema

```python
@dataclass
class FilterSpec:
    # scope
    author_regex: str | None = None     # "^Wodehouse,"
    pg_id: str | None = None            # "PG1342"  (book scope)
    user_id: str | None = None          # "U7"
    lang: str = "en"
    country: str | None = None          # "GB" | "US" | "RU" ...

    # period (writing prime proxy)
    year_from: int | None = None
    year_to: int | None = None
    year_basis: Literal["auto", "pub_year", "birth_plus_30"] = "auto"

    # filtering quality
    min_corpus_count: int = 0           # для affinity OOV filter
    min_author_count: int = 5
    exclude_proper_nouns: bool = True
    exclude_metalinguistic: bool = True  # title содержит "dictionary"/"grammar"
    pos_filter: list[str] | None = None  # ["NOUN", "VERB", "ADJ", "PROPN"]

    # limits
    max_books: int = 10_000             # cap для тяжёлых scans
    top_n: int = 50
```

### 2.2 Apply API

```python
class FilterSpec:
    def apply_to_metadata(self, meta_df: pd.DataFrame) -> pd.DataFrame: ...
    def explain(self) -> str:
        """Human-readable description of the filter for renderer."""
        # "Все английские книги авторов из GB, написанные 1837-1901, 12 480 книг"
```

### 2.3 Legacy adapter

```python
@classmethod
def from_legacy_scope(cls, scope: dict | str) -> FilterSpec:
    if scope == "all_corpus": return cls()
    if isinstance(scope, dict):
        if "book" in scope: return cls(pg_id=scope["book"])
        if "author" in scope:
            return cls(
                author_regex=scope["author"],
                country=scope.get("country"),
                year_from=scope.get("year_from"),
                year_to=scope.get("year_to"),
            )
    raise ValueError(f"bad scope: {scope}")
```

---

## 3. Tool Registry

### 3.1 ToolSpec

```python
@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., ToolResult]
    category: Literal[
        "search", "statistics", "authors", "books",
        "words", "learning", "emotion", "corpus_meta",
    ]
    description: str                     # для LLM tool schema
    input_schema: dict                   # JSONSchema
    output_data_schema: dict             # для tests/renderer/typed clients
    requires: list[Literal[              # validation hints
        "author", "book", "word", "scope", "country", "year_range",
    ]]
    cost: Literal["cheap", "medium", "heavy"]
    timeout_s: int = 30
    cacheable: bool = True
```

### 3.2 Decorator

```python
@tool(
    name="affinity_by_author",
    category="statistics",
    description="Фирменные слова автора по affinity-метрике.",
    requires=["author"],
    cost="medium",
    input_schema={
        "type": "object",
        "properties": {
            "filter": {"$ref": "#/definitions/FilterSpec"},
            "top": {"type": "integer", "default": 50},
        },
        "required": ["filter"],
    },
)
def affinity_by_author(filter: FilterSpec, top: int = 50) -> ToolResult: ...
```

### 3.3 Build OpenAI/Ollama schema

```python
def build_tools_spec(category_filter: list[str] | None = None) -> list[dict]:
    """Returns list of {"type": "function", "function": {...}} for Ollama."""
```

### 3.4 Dispatch

```python
def dispatch(name: str, args: dict, *, planner_context: dict | None = None) -> ToolResult:
    spec = REGISTRY[name]
    args = _coerce_args(spec, args)            # cast FilterSpec dict → FilterSpec instance
    t_start = time.perf_counter()
    try:
        if spec.cacheable:
            cached = cache.get(name, args)
            if cached: return cached.with_cache_hit(True)
        result = spec.fn(**args)
        runtime_ms = int((time.perf_counter() - t_start) * 1000)
        result.runtime_ms = runtime_ms
        if spec.cacheable and result.ok:
            cache.put(name, args, result)
        return result
    except TimeoutError:
        return ToolResult.error(name, "timeout", retryable=True)
    except Exception as e:
        log.exception(f"tool {name} crashed")
        return ToolResult.error(name, "internal", message=str(e))
```

---

## 4. Metadata Resolver

### 4.1 CanonicalBook

```python
@dataclass
class CanonicalBook:
    book_id: str          # "PG1342" | "U7"
    pg_id: str | None
    title: str
    author: str
    author_id: str        # slug
    lang: str
    year: int | None
    source: Literal["spgc", "orphan_pg", "user_upload"]
    coverage: dict        # {"has_tokens": True, "has_pub_year": False, ...}
```

### 4.2 API

```python
class MetadataResolver:
    def resolve_book(self, query: str, author_hint: str = "") -> CanonicalBook | None: ...
    def resolve_author(self, query: str) -> list[Author]: ...
    def list_books_by_author(self, author_regex: str, fs: FilterSpec) -> list[CanonicalBook]: ...
```

---

## 5. Corpus Version

```python
@dataclass
class CorpusVersion:
    timestamp: str                    # "2026-05-17T15:08"
    books_total: int                  # 55_101
    chunks_total: int                 # 3_862_006
    analytics_version: str            # "v2.0.0"
    spgc_baseline: str                # "SPGC-2018-07-18"
    user_uploads: int
    orphan_pg: int
```

Read from:
- `/workspace/spgc/derived/corpus_meta.json`
- `/data/raw_text/` count
- chroma collection stats
- `scripts/v2/__version__.py`

Включается в каждый ToolResult.source_info.

---

## 6. Cache Layer

### 6.1 Storage

- In-process LRU (size=512) — для горячих lookups в одном request.
- Disk JSON in `/data/v2_cache/<tool>/<arg_hash>.json` — TTL и corpus_version-tagged.

### 6.2 Invalidation

- При смене corpus_version (новый upload завершён) — soft invalidate (mark stale, не delete).
- Stale entries возвращаются с `ToolWarning("stale_cache")`.
- TTL отдельный для каждой category:
  - `etymology`: forever (Wiktionary API immutable)
  - `enrich_word`: 30 days
  - `profiles`: invalidate at corpus_version bump
  - `heavy queries`: 7 days

### 6.3 Key

```python
def cache_key(tool: str, args: dict) -> str:
    norm = json.dumps(args, sort_keys=True, default=str)
    return f"{tool}:{hashlib.sha256(norm.encode()).hexdigest()[:16]}"
```

---

## 7. Observability

### 7.1 RequestLog

```python
@dataclass
class RequestLog:
    request_id: str
    timestamp: str
    user_question: str
    intent: str
    intent_confidence: float
    entities: dict
    plan_steps: list[str]
    tool_calls: list[dict]            # {name, args, runtime_ms, ok, cache_hit}
    fallback_used: bool
    llm_tokens_prompt: int
    llm_tokens_completion: int
    total_elapsed_ms: int
    answer_confidence: Literal["high", "medium", "low"]
    answer_truncated_to_300: str
```

JSONL append-only в `/data/logs/v2/queries-YYYY-MM-DD.jsonl`.

### 7.2 Metrics surfaced на dashboard

- Top 10 slowest tools по runtime_ms p95.
- Cache hit rate by tool.
- Intent distribution (last 24h).
- Failed plans by intent.
- LLM token spend.

---

## 8. Versioning

- `scripts/v2/__version__.py`: `ANALYTICS_VERSION = "2.0.0-alpha1"`.
- Bump rules:
  - patch: bug fix in tool implementation, no schema change.
  - minor: new tool added.
  - major: breaking schema change (ToolResult/FilterSpec).
- Each ToolResult tagged с current version → tests могут отслеживать regression.
