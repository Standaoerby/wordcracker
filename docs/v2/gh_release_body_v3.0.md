# wordcracker v3.0 — Sprint 16 release

First major version bump since v2.0 (deterministic pipeline). Sprint
16 lands an **open architecture**: three plugin surfaces (author
aliases, scoring metrics, intent rules) + a programmatic numeric
audit + five new intents covering meta-queries, semantic book search,
and publication years.

422 unit tests, 0 failures (up from 303 at the start of the sprint).
All existing chat queries hit the same tools with the same shapes —
this is additive, not breaking.

## Highlights

**1. Pluggable scoring (Phases B3 + B4 + C)**
Single `ScoringPlugin` Protocol unifies three use cases that used to
have separate ad-hoc code paths:

- `author_similarity` — `burrows_delta`, `jaccard_top200`, `ensemble`
  (Borda count). Default for `author_influences` is now `ensemble`.
- `retrieval_rerank` — `bge_reranker` (BAAI/bge-reranker-base,
  lazy-loaded cross-encoder). Use as `hybrid_search(rerank_with=
  "bge_reranker")` or `find_book_by_topic(rerank_with="bge_reranker")`.
- `word_pair` — `pmi`, `npmi` (Bouma 2009), `dice` for collocate
  strength. New `word_collocates(metric="npmi", min_cooccurrence=5)`.

Adding a new metric = one class + one REGISTRY entry. The contract
test auto-iterates the registry so the new plugin is validated for
free.

**2. Numeric audit (Phase D)**
After the critic LLM pass, a deterministic check extracts numbers
from the rendered answer and verifies them against tool data
(including list lengths for «top 10» style claims). Catches the
v1.0e «Doyle 47 → answer says 200» failure mode that the LLM critic
missed ~30% of the time. Skips intro/clarify intents, year-like
numbers, and small (<5) counts to avoid false positives. Append-only
📊 footer when mismatches found, silent otherwise. Renderer prompt
also gains rule 11 spelling out the count-fidelity contract.

**3. Four new intents (Phases E + F)**

| Intent | Triggers | Routes to |
|---|---|---|
| `author_lookup` | «какие книги у Doyle» | `author_metadata` (sample_titles) |
| `book_extremum` | «самая популярная книга» | `top_books_by_downloads(top=1)` |
| `corpus_extremum` | «самый плодовитый автор» | `top_authors_by(top=1)` with metric inference |
| `topic_book_search` | «найди книгу про викторианский Лондон» | new `find_book_by_topic` tool |

`find_book_by_topic` wraps `hybrid_search` and dedupes by `pg_id`, so
the result is one row per book (best chunk per book wins) instead of
per-passage. Optional BGE rerank.

**4. Author aliases auto-gen (Phase A)**
`AUTHOR_ALIASES` split into `AUTHOR_ALIASES_CURATED` (handcrafted
overrides, ambiguity guards, Russian stems) and `aliases_generated.
json` (built from `/workspace/spgc/metadata.csv` via the new
`build_author_aliases.py`). Runtime merge: curated wins. Closes
the Walpole / Radcliffe / Maturin gap reported in round 6 without
adding 50 hand-written entries.

**5. Long-tail polish (Phase G)**
- New `book_pub_year` intent + plan for «когда была опубликована
  Война и мир» (uses Sprint 9.7 Open Library enrichment, surfaced
  via find_book).
- RU genitive + prepositional case variants in `KNOWN_BOOKS` for the
  five most-asked-about Russian-titled books. «Слова в Войне и мире»
  / «стиль Преступления и наказания» / «персонажи Анны Карениной»
  now resolve to the right PG id.

**6. Confidence floor on author similarity (Phases B1 + B2)**
Round 6 R19: Doyle and Poe returned near-identical «closest authors»
because Burrows Delta on a popular author clusters near the corpus
mean. v3.0 asks v1 for `top * 3` candidates, filters aggregate
buckets (Various / Anonymous / Encyclopaedia Britannica), and
annotates the result with `similarity_confidence: low` plus a render
note when the top-N spread is < 5% of median. The LLM now tells the
user honestly «no clear stylistic match, this author sits near
corpus mean» instead of confidently listing baseline noise.

## Tests

- Unit: **422 / 422** (was 303 at start of Sprint 16, +119 across
  the 8 phases). 0 failures.
- AST sweep clean.
- All plugin contract tests auto-validate via REGISTRY iteration.

## Deploy

```bash
# On SOW:
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-chat

# Materialize the auto-built alias table (closes Phase A gap):
sudo -u claude bash -c "cd /home/claude/wordcracker && \
  docker compose exec gutenberg-lab python \
  /workspace/scripts/v2/build_author_aliases.py"
```

No container rebuild needed — `sentence-transformers` was already in
the gutenberg-lab Dockerfile. BGE reranker (~440 MB) lazy-downloads on
first use of `rerank_with="bge_reranker"`.

## Optional verification queries

After deploy, the new paths to spot-check:

- «найди книгу про викторианский Лондон» — should chip as
  `topic_book_search` and return 8 unique books (not «не найдено»)
- «когда была опубликована Война и мир» — `book_pub_year`, surfaces
  pub_year from OL enrichment
- «слова в Войне и мире» — genitive resolution should pin to PG2600
- «самый плодовитый автор» — `corpus_extremum`, returns top-1 by books
- Any answer with explicit counts — confirm the 📊 audit footer does
  NOT appear on factually correct numbers (false-positives kill trust)

Co-developed with Claude Opus 4.7 (1M context).
