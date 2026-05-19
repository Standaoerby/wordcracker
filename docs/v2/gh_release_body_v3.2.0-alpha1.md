# wordcracker v3.2.0-alpha1 — v4 architecture: LLM planner + DAG router

Alpha release of the architectural refactor that closes the rules-based
ceiling. Stan's 2026-05-19 verdict on v3.1.1: «полный отстой … нужно
системное решение, которое понимает вопросы живых людей и может
комбинировать тулзы». This is that solution, behind a feature flag.

**Production behavior is unchanged with the flag off.** v3.1.1
contract preserved.

## What's in v4

A second planning path for queries that the rules-based pipeline drops
to clarify. One LLM call (not a loop) emits a typed `PlanSpec` JSON
DAG; the router executes it deterministically; existing critic +
numeric audit + observability all run unchanged downstream.

```
user query
  │
  ▼
rules-based intent classifier (203 rules, ≤50ms)   ◀── v3 fast path
  │
  ├── matched → plan template → router → renderer
  │
  └── clarify ──▶ v4 LLM planner ──▶ PlanSpec DAG ──▶ router → renderer
       (~1-2s)        │
                      └── still clarify → graceful clarify with options
```

Six new modules + ~1,200 lines + **81 new tests, 718 total in suite**.

### `planner/plan_spec.py` — typed DAG

PlanSpec is the central abstraction. JSON-roundtrippable.

- Step args support `$sN.field[.subfield]` references (nested dicts,
  list indices). Router resolves at execute time.
- `needs` lists explicit dependencies; the validator combines them
  with discovered $-refs to build a topological order. Cycles fail.
- `clarify` is a valid terminal — when the LLM genuinely can't plan,
  it emits `{"clarify":"..."}` instead of inventing steps.

Validator checks tool existence, required args (satisfied by either a
literal OR a `$-ref`), dependency validity, cycles, step count caps,
heavy-tool cost budget. Errors block execution; unknown-arg warnings
flow into observability.

### `planner/tool_catalog.py` — single source of truth

The catalog is built from the `@tool` registry at startup, then
serialized into a compact (~13 KB) prompt block. **Adding a new tool
auto-registers it for the planner — zero prompt edits.**

Few-shot examples live in code, validated by the same `validate()` so
the prompt can never demonstrate an invalid plan. Five canonical
patterns: single-book lookup, author signature words, multi-book
etymology ratio (Stan's screenshot), 3-author triangulation, clarify
fallback.

### `planner/llm_planner.py` — Ollama call

- `qwen3:14b` with `format=json` and a 1200-token output cap.
- Strict JSON parsing with tolerance for accidental markdown fences /
  leading prose.
- **Retry once** on invalid JSON or invalid PlanSpec, with a
  validation summary appended to the retry prompt («your plan failed
  validation because: missing_required_arg, unknown_tool»). Both
  retries fail → graceful clarify with the original query as scaffold.
- Per-process LRU cache on `(query, last_user_msg)` → 128 entries.
- Per-request observability: attempts count, elapsed seconds, full
  PlanSpec in JSONL log.

### `planner/router.py` (extended) — DAG execution

New entry points: `execute_spec` (sync) + `execute_spec_stream` (SSE).
Topological order, `$sN.field` resolution before dispatch, identical
failure-handling semantics to the v3 router (optional steps continue
past failure, non-optional abort).

### `tools/meta/resolve_entity.py` — entity resolvers as tools

Two new `@tool` wrappers the LLM planner composes into plans:

- `resolve_author_name(query)` → `{author_regex, canonical, confidence}`
  Layered: curated AUTHOR_ALIASES → surname fallback → fuzzy match
  against SPGC metadata. Handles «Conan Doyle», «Достоевского», «John
  Milton», «у Wodehouse» — no code change required for new authors.

- `resolve_book_title(query)` → `{pg_id, title, confidence}`
  Layered: KNOWN_BOOKS → v1 find_book fuzzy match. Honors the empty-PG
  copyright sentinel (HP / LOTR / 1984 → confidence 0.8 +
  copyright warning).

These exist because the v3 architecture forced new entities through
manual edits to `entities.py`. With v4 they propagate dynamically.

### `rag_v2.py` (extended) — wired behind flag

Two changes:
1. When the rules-based pipeline produces `needs_clarify=True` AND
   `WC_LLM_PLANNER=on`, invoke `llm_planner.plan_query` and route the
   result through `router.execute_spec`.
2. Observability fields: `v4_planner_used`, `v4_planner_attempts`,
   `v4_planner_elapsed_s` in every request log.

If the planner is disabled or returns a clarify, behavior matches v3
exactly — the v3 clarify message is replaced only when v4 has a more
specific question to ask.

## Why v4 will not regress to v1

| | v1 broken | v4 correct |
|---|---|---|
| LLM calls per query | 1-5 iterations w/ drift | 1 plan emit + 1 render + 1 critic |
| LLM picks tool in loop | yes | no — plan emitted ONCE |
| Output format | free text + heuristic parse | strict JSON via `format=json` |
| Hallucinated tool | runs (silently broken) | fails validation, never dispatched |
| Trace visible | no | PlanSpec in JSONL log |
| Critic / audit | absent | unchanged from v3 |
| Fallback | timeout | rules → LLM → graceful clarify |

## Test suite

```
tests/v2/test_v4_plan_spec.py            26 tests — parsing / $-refs / validator / topo sort
tests/v2/test_v4_tool_catalog.py         13 tests — catalog + examples + prompt assembly
tests/v2/test_v4_llm_planner.py          13 tests — Ollama mocked: retry / cache / history
tests/v2/test_v4_router_dag.py           11 tests — DAG ordering / failure / SSE stream
tests/v2/test_v4_resolve_entity.py       15 tests — curated paths + registry wiring
tests/v2/test_v4_rag_integration.py       3 tests — end-to-end with all mocks
```

Combined: **718 passed, 426 subtests passed, 0 failures** (was 637
at v3.1.1; +81 v4 tests).

## What's NOT in this alpha

- **No real Ollama integration test** — that's a separate opt-in
  script behind `WC_INTEGRATION_TESTS=1`. Unit tests mock `_call_ollama`.
- **No unified renderer** — Phase 4 of the v4 plan. Each tool's
  `_render_note` still wins; consolidation is the next refactor.
- **No corpus-aggregate tools** (`corpus_etymology_ratio`,
  `corpus_readability_distribution`) — those are NEW TOOLS, not
  planner work. Sprint 21 backlog.
- **No streaming for v4 in `ask_stream()`** — `execute_spec_stream`
  exists but rag_v2's SSE handler still uses v3. Phase 4.
- **`ask_stream()` rewrite** with v4 — kept v3 path stable; alpha
  rollout uses `ask()` only.

## Deploy

```bash
# 1. Pull
sudo -u claude git -C /home/claude/wordcracker pull

# 2. Restart chat (no admin / chroma changes)
sudo systemctl restart wordcracker-chat

# 3. Optional: flip flag on
sudo systemctl set-environment WC_LLM_PLANNER=on
sudo systemctl restart wordcracker-chat

# 4. Observe via admin dashboard — look for v4_planner_used=true rows
```

Recommended alpha rollout: leave the flag OFF in prod for 24h, ship
v3.2.0-alpha2 with any observability tweaks needed, then flip ON
for a controlled bake. Per-request log gives Stan the dataset to
review what the LLM is generating.

## What the user will see when the flag is on

**Before** («germanic vs latinate ratio в Beowulf и Paradise Lost»):
> «Не уверен, что ты имеешь в виду…»

**After** (v4 plan executes through router):
A table with germanic-affinity and latinate-affinity word counts for
each of the two books, plus the renderer's discussion of the ratio.

**Or** if the LLM declines the plan honestly:
> «Уточни — Burrows Delta для двух авторов даёт одну дистанцию;
> для триангуляции с третьим автором я могу запустить два compare_authors
> и сравнить distances. Это то что нужно?»

Specific, actionable, in the user's language.

Co-developed with Claude Opus 4.7 (1M context).
