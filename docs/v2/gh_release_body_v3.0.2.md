# wordcracker v3.0.2 — Sprint 17 + 18 (production-ready cluster)

Patch release on top of v3.0. Eleven commits across two sprints. Closes
all closeable Round 7 + Round 8 findings, adds retrieval-quality
foundation (BGE rerank default, source logging, token observability),
and lands four UX gaps caught by Stan in prod testing.

**Suite:** 501 unit tests, 0 failures (was 422 at v3.0).

## Highlights for end users

### Retrieval quality (Sprint 18)
- **BGE cross-encoder rerank ON by default** for the three relevance-
  critical intents: `topic_book_search` («найди книгу про X»),
  `book_similar` («похожие на X»), `word_contexts` no-author path.
  +1-2s latency, significant relevance lift — BAAI/bge-reranker-base
  reorders the bi-encoder candidate pool by full cross-attention.
- **Retrieval source logging.** Every RAG-tool match now writes
  `{tool, pg_id, title, author, score, score_kind, snippet_preview}`
  into the JSONL log. Diagnostic bridge between «модель ответила плохо»
  and «retrieval подсунул мусор».
- **hybrid_search candidate pool 30 → 50.** Wider RRF input for the
  cross-encoder to choose from.
- **Token observability.** `renderer_prompt_tokens` /
  `renderer_eval_tokens` / `critic_prompt_tokens` / `critic_eval_tokens`
  pulled from each Ollama response. Data-driven basis for future
  num_ctx decisions.

### New intents (Sprint 17 + 18)
- `book_readability_compare` — «что сложнее читать X или Y»; dispatches
  `book_readability × N` (cap 3). Multi-book extraction added via
  `multi_book_ids` / `multi_book_titles`.
- `book_similar` — «похожие на X», «продолжение X», «similar to X»;
  uses `find_book_by_topic` with X's title as semantic topic + BGE
  rerank.
- `similar_to` — ambiguous «в стиле X» router; plan-builder dispatches
  to book_similar OR author_closest based on which entity (book or
  author) resolved.
- Bibliographic `who wrote X` / `кто автор Дракулы` now properly
  chains via find_book (was eating clarify or hitting wrong tool).

### Closed prod bugs

**Sprint 17:**
- Multi-author `word_contexts` (Round 7 Q8) — «примеры ajar у Остин/
  Диккенса/Дойла» dispatches 3 parallel word_contexts calls. Plus
  bare-word extraction after «примеры/examples» so verbatim phrasing
  works.
- Intent classifier short-circuit — pre-sorted rule iteration with
  early-break. Bit-identical output, 60-95% fewer regex evaluations
  on typical short queries.
- Extended critic skip-list — 6 more table-echo intents skip the LLM
  critic call (author_lookup / corpus_extremum / book_extremum /
  topic_book_search / book_pub_year / book_lookup / book_similar).
  Saves ~3-5s wall-clock on ~40% of query volume.
- `ask_stream()` observability gap — chat-UI path (`/api/chat/stream`)
  never called `obs_mod.log_request()`. Result: `/admin/failed` was
  permanently empty for streamed queries. Fixed; records carry
  `via_stream: True`.
- «что почитать после X» rerouting — was `book_recommendation` returning
  generic popular books, now `book_similar` returning books
  thematically related to X.
- Readability-compare clarify drop — «что сложнее читать X или Y»;
  Shakespeare titles added to KNOWN_BOOKS (Midsummer/Hamlet/Macbeth/
  Romeo+Juliet) with full RU declension.
- Round 8 P0 silent context fallback — «теперь у Марло» (Marlowe not
  in aliases at the time) silently restored Shakespeare from prior
  turn. User-deceptive. Now: detect explicit-author-after-swap pattern,
  block backfill, clarify honestly.
- Elizabethan dramatists cohort — Marlowe / Webster (John) / Jonson
  (Ben) / Dekker / Kyd / Beaumont / Fletcher (John) / Middleton, all
  with EN + RU forms.
- `author_attribution` passage phrasings — «угадай автора отрывка»,
  «чей этот отрывок», «identify the author of this passage», «whose
  excerpt is this».

**Sprint 18:**
- Book-scope override for author_vocab — «характерные прилагательные
  в "The Picture of Dorian Gray"» now correctly chains to
  affinity_by_book (was bouncing to clarify with «нужен автор»).
- Chat hints CSS auto-hide — pure-CSS sibling selector replaces JS-
  driven collapse; chips hide automatically when log non-empty, no
  race conditions. New «💡 примеры» toggle button in header.
- Multi-word timeline (Round 8 C5) — «timeline telephone+automobile+
  aeroplane» now dispatches 3 parallel `word_freq_timeline` calls
  (cap 5). Plus single-word origin queries «когда появилось слово X»
  routed to word_timeline (was eaten by book_pub_year).

## Numbers

| | v3.0 | v3.0.2 |
|---|---|---|
| Unit tests | 422 | **501** (+79) |
| Plan templates | 38 | **40** |
| Intent labels | 41 | **44** (+similar_to, +book_similar, +book_readability_compare) |
| Scoring plugins | 7 | 7 (no change) |
| v2 tools | 36 | 36 (no change) |
| `_INTENT_SKIP_CRITIC` | 3 | **9** |
| `hybrid_search` per_retriever default | 30 | **50** |
| BGE rerank default-on intents | 0 | **3** |

## Deploy

```bash
# On SOW:
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-chat

# First query that uses BGE will lazy-download BAAI/bge-reranker-base
# (~440 MB → HF-cache). Subsequent queries: ~1-2s rerank overhead.
```

No container rebuild needed — `sentence-transformers` was already in the
gutenberg-lab Dockerfile.

**Browser cache:** `Ctrl+Shift+R` on slovoeb.net to pick up the new chat
UI CSS (sibling-selector auto-hide for hint chips).

## Spot-check after deploy

Recommended Round 9 verification queries:

```
характерные прилагательные в "The Picture of Dorian Gray"
  → intent: author_vocab → tool: affinity_by_book (was clarify)

что почитать после Преступления и наказания
  → intent: book_similar (was book_recommendation popularity-only)

в стиле Pride and Prejudice
  → intent: similar_to → plan: find_book_by_topic (book)

в стиле Достоевского
  → intent: similar_to → plan: author_influences (author)

who wrote Hamlet
  → intent: author_attribution → plan: find_book (was clarify)

timeline telephone+automobile+aeroplane
  → intent: word_timeline → 3 × word_freq_timeline (was clarify)

теперь у Нонэксистентов (after a Shakespeare query)
  → clarify with «не узнаю автора 'Нонэксистентов'» (was silent
    Shakespeare fallback)

любой topical query
  → check /admin/failed populates correctly; check retrieval_log
    block in JSONL
```

Co-developed with Claude Opus 4.7 (1M context).
