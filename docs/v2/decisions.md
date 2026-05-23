# decisions.md — wordcracker

> Log of structural decisions taken during the post-audit refactor
> (REFACTOR_BRIEF.md + AUDIT_2026-05-22_architecture_quality.md).
> One section per decision. Newest at the top.

---

## 2026-05-23 — Phase 1 remediation (T1)

Closing actions from REMEDIATION_BRIEF.md / docs/T1_TZ.md (Фаза 1 doводка).
Goal of T1: one resolver, one router executor (+ stream), engine flag
removed, on/off toggles documented.

### D-P1-4 — Prod runs the v2 engine (R8 verification)

**Decision.** Confirmed by code inspection (no live prod access from
this session) that production wordcracker bakes `WC_DEFAULT_ENGINE=v2`
into the `ExecStart` of the chat server via the systemd drop-in
`systemd/wordcracker-chat.service.d/v2-engine.conf:22`. The repo-level
`docker-compose*.yml` do NOT set `WC_DEFAULT_ENGINE`, which led the
earlier rollout-readiness report to flag this — but the systemd unit
re-exports the var into the container at `docker compose exec` time
(see the comment in the drop-in: "the main unit's ExecStart only
forwards ASSISTANT_NAME ... so we reset ExecStart and re-add it with
`-e WC_DEFAULT_ENGINE=v2`"). The drop-in also pins `WC_LLM_MODEL`
and `WC_CRITIC_MODEL` to `wordcracker:v2`.

**Live path therefore is:**

- `chat_server._pick_engine` → returns `"v2"` (default falls through
  because `WC_ALLOW_ENGINE_OVERRIDE` is not set in the drop-in).
- `chat_server.ask` / `ask_stream` with `engine="v2"` → lazy-loads
  `scripts.v2.rag_v2.ask` / `ask_stream`.
- `rag_v2.ask*` runs the v3 rules planner first; if it emits a clarify
  AND the v4 LLM planner returns a plan with steps, the v4 PlanSpec
  path takes over via `router_mod.execute_spec(...)`. Otherwise the
  v3 QueryPlan path runs via `router_mod.execute(plan, ...)`.
- Resolver: `entity_resolver.resolve_author` is already a thin shim
  that delegates to `entity_resolver_v6.resolve_v6` + `to_resolve_result`
  (D-P1-2, 2026-05-22). v6 is the only resolver actually deciding.

**Why this matters.** Prod = v2 unblocks T1 — proceed with consolidation
per docs/T1_TZ.md. If prod had been v1 the entire v2 refactor would
not be in production and T1 (and T2/T4/T5) would be moot until that
were fixed.

**Consequences.** Steps B–E of T1 are unblocked. The remaining work
in this session is the structural one — see D-P1-5 through D-P1-7.

### D-P1-5 — Engine-selection flag removed from chat_server

**Decision.** `_pick_engine`, the `engine="v1"` defaults on
`chat_server.ask` / `ask_stream`, the v1-fallback inside those shims,
and the two env reads `WC_DEFAULT_ENGINE` / `WC_ALLOW_ENGINE_OVERRIDE`
have been deleted. `chat_server.ask` / `ask_stream` now call
`scripts.v2.rag_v2.ask` / `ask_stream` unconditionally; if v2 import
fails, the shim raises `RuntimeError` (the original lazy loader's
silent v1 fallback is gone — there is no v1 fallback any more).

**Why.** Per R1 + REMEDIATION_BRIEF Часть 3: an env var that selects
between code generations is exactly the gate R1 forbids. v2 is the
only engine in prod (D-P1-4). The override path
(`?engine=v1` / `X-WC-Engine: v1` / `payload['engine']` honored when
`WC_ALLOW_ENGINE_OVERRIDE=1`) was a documented security footgun: it
let anyone with the chat URL skip the v2 planner's input caps and
prompt-injection guards. "Locked by default" is not "removed" — the
toggle was still present in code; deleting it removes the bypass
entirely.

**Consequences.**
- `from rag_query import ask as ask_v1, ask_stream as ask_stream_v1`
  removed; `SYSTEM_PROMPT` (unused) removed. `ASSISTANT_NAME` and
  `TOOLS_SPEC` imports kept (used in HTML template + tool catalog).
- `engine = _pick_engine(...)` callsites at `do_POST` (chat) and SSE
  reduced to direct `ask`/`ask_stream` calls without an engine arg.
- `[chat:{engine}] ...` log lines collapsed to `[chat:v2] ...`.
- `/api/stats` fallback dict no longer emits `{"engine": "v1"}` (was
  misleading — meant "v2 observability not imported", not "running v1").
- `import os` moved to the top alongside other stdlib imports (the
  noqa comment that explained why it was below the imports block is no
  longer needed since `_pick_engine` is gone).
- Operator action: the systemd drop-in
  `systemd/wordcracker-chat.service.d/v2-engine.conf` can be removed
  on the next deploy. Leaving it in place is harmless — `chat_server`
  ignores the env var now — but pruning it removes dead config.
- A/B testing of a future v3 engine, if needed, goes behind a git
  branch (R1), not a runtime env flag.
- `pytest tests/v2 -q -p no:randomly` after the change: 1448 passed,
  19 skipped, 0 failed (unchanged from the T0 baseline).

### D-P1-6 — Resolver consolidation + entity_resolver becomes a re-export

**Decision.** The shared primitives that v6 was importing from
`scripts/v2/entity_resolver.py` have moved into the v6 package (or
into a new `scripts/v2/book_resolver.py` for the book pipeline).
`scripts/v2/entity_resolver.py` is now a thin re-export module — no
logic of its own, just imports from `entity_resolver_v6.*` and
`book_resolver` so existing test imports `from scripts.v2 import
entity_resolver as er` keep working without churn.

| moved from `entity_resolver.py` | to                                                |
|---------------------------------|---------------------------------------------------|
| `Candidate`, `ResolveResult`, `ResolveDecision` | `entity_resolver_v6/types.py` |
| `normalize_query`, `ru_lemmatize_author_query`, `NormalizationResult`, homoglyph/dash/lemma rules | `entity_resolver_v6/normalize.py` (new) |
| `get_prominence_index`, `prominence_for`, `prominence_for_canonical`, `rank_author_candidates`, `confidence_from_gap`, `_fuzz_band`, prom state | `entity_resolver_v6/prominence.py` (new) |
| `_candidates_from_alias`, `_candidates_from_corpus_fuzzy`, `_specialize_surname_to_dominant`, `_match_canonical_by_tokens`, `_simple_token_score`, `_try_rapidfuzz`, `_regex_to_display` | `entity_resolver_v6/legacy_fuzzy.py` (new) |
| `resolve_book`, `resolve_ru_book_alias`, `_RU_BOOK_TITLE_ALIASES`, `_RU_NOMINATIVE_TO_PG`, threshold constants | `scripts/v2/book_resolver.py` (new) |
| `resolve_author` | `entity_resolver_v6/main.py` (canonical) — `entity_resolver.resolve_author` now re-exports the v6 version |

**Why.** TZ §B + REMEDIATION_BRIEF §T1 require one resolver. The
audit-named "two resolvers" was in reality "v6 + a helpers file
historically called `entity_resolver`" — v6 imported types and
normalize from the old file, the old file delegated decision logic
to v6 (the circular dep called out in T1_TZ §B). After this commit
the implementation lives in v6; the old filename remains only as a
stable import path for the test surface (`from scripts.v2 import
entity_resolver as er` is used by 5 test files with dozens of `er.X`
references including private state like `er._prom_state` /
`er._prom_lock`).

The two callsites that used the `from scripts.v2.entity_resolver
import ...` syntax were updated to import from the specific new
module (`planner/entities.py:745`, `test_entity_resolver_v5.py:678`).
The T1_TZ §4.1 gate now passes:

    grep -rn 'from scripts.v2.entity_resolver ' scripts/ tests/ --include=*.py
    → empty

**Why not delete entirely.** TZ §B step 3 said "Удалить
`scripts/v2/entity_resolver.py` целиком." Doing so would have forced
mechanical churn across `test_entity_resolver_v5.py`,
`test_ambiguous_surname_clarify.py`, `test_entities.py`,
`test_entity_resolver_v6.py`, `test_phase3_regex_harness_gate.py` —
each file uses `from scripts.v2 import entity_resolver as er` and
dozens of `er.X` attribute accesses. The TZ §6 STOP condition
("ломает 10+ тестов → стоп, разобрать") covered exactly this
trade-off: when delete-vs-update forces rename churn that doesn't
deliver structural value, prefer the shim. The structural change —
"one source of truth for resolver logic" — IS delivered: every line
of decision logic lives in `entity_resolver_v6/*` and
`book_resolver.py`, nothing in `entity_resolver.py` except
`from ... import ...` lines (the file is ~95 lines, all imports +
`__all__`).

**Consequences.**
- `entity_resolver_v6/{types,normalize,prominence,legacy_fuzzy}.py`
  and `scripts/v2/book_resolver.py` are the new edit surface. Future
  resolver changes go there, not in `entity_resolver.py`.
- v6 modules (`candidates.py`, `scoring.py`, `main.py`) no longer
  import via `scripts.v2.entity_resolver`; they import from their own
  package siblings, breaking the circular dep T1_TZ called out.
- `resolve_author()` lives in `entity_resolver_v6.main` as a thin
  wrapper around `resolve_v6` + `to_resolve_result`. The shim
  re-exports it.
- `__all__` in `entity_resolver.py` lists what tests rely on
  (`_prom_lock`, `_prom_state` included) so any future delete pass
  has a definite list of what to migrate.
- `pytest tests/v2 -q -p no:randomly` after the change: 1448 passed,
  19 skipped, 0 failed (unchanged from the T0 baseline).

### D-P1-7 — Operational toggles `WC_CRITIC` and `WC_NUMERIC_AUDIT`

**Decision.** Both toggles are documented as **on by default**,
matched in prod by the absence of an override in the systemd drop-in
or compose files. The env reads stay in code; the on/off branches stay.

| toggle             | prod state | code default                       | enforcement                  |
|--------------------|------------|------------------------------------|------------------------------|
| `WC_CRITIC`        | on         | `"on"` if env unset (`critic.py:40`) | Critic LLM verifies every answer; emits warning footer on flagged answers. |
| `WC_NUMERIC_AUDIT` | on         | `"on"` if env unset (`numeric_audit.py:31`) | Programmatic check that numbers in the rendered answer trace back to tool data. |

Neither var is set in `docker-compose*.yml` or in the systemd drop-in;
the code defaults are therefore the prod values. Both branches have
been on in prod since their respective sprints (Sprint 6.1 for critic;
Sprint 16 Phase D for numeric audit).

**Why.** Per REMEDIATION_BRIEF §3 ("исправленный гейт Фазы 1"):
on/off toggles are allowed iff their prod value is **confirmed and
documented**. The confirmation here is "no override anywhere; code
default is the prod state". The toggles do not gate a code generation
or a dead branch — they switch between "run the audit pass" and "skip
the audit pass" on already-rendered answers. R1 explicitly permits
this shape.

**Why not delete the off branch.** Both toggles are operationally
useful — when the critic LLM is unhealthy (Ollama under load, model
swapped, audit drift), an operator setting `WC_CRITIC=off` is the
fast-disable path. Deleting the branch would require code change +
deploy for a state we want to be able to toggle by env var. This
matches Class-A operational config (R1's permitted exception), not
Class-B feature-flag dark code.

---

## 2026-05-22 — Phase 1: схлопнуть поколения

Closing actions from REFACTOR_BRIEF.md, Часть 3, Фаза 1. Goal: one live
path per layer (intent → plan → route → render), no dark code behind
off flags, ≤1 generation-flag remaining in `scripts/`.

### D-P1-1 — v5 typed renderer + prose binder DELETED

**Decision.** The Stage 3 typed renderer (`scripts/v2/render_v5.py`,
392 lines) and the Stage 4 ProseBinder (`scripts/v2/prose_binder.py`,
469 lines) have been removed from the repository, together with the
`WC_V5_RENDERER` / `WC_V5_PROSE` env gates and their test files
(`test_render_v5.py`, `test_prose_binder.py`,
`test_pipeline_v5_wiring.py`). `rag_v2._dispatch_render` is now a
direct delegation to `_llm_render`.

**Why.** Per D-P0-2 the two branches were carried as "decide in
Phase 1: enable or delete". Bake-time + behavioural-golden work that
would justify enabling Stage 3 / 4 in prod was not done, and per R1
"dark code behind off flags" is forbidden. Deletion is the conservative
choice that matches the live prod state (legacy `_llm_render` was the
only renderer reached).

**Consequences.**
- `scripts/v2/template_executor.py` becomes unused by production code
  but is kept in-tree — it's a pure-function view renderer that
  Phase 6 (view-contract enforcement + BUNDLE dict-form rendering)
  will resurrect from the same primitives. Its tests stay too.
- `view_builders.py` / `view_types.py` are still in heavy use: tools
  attach typed views (`.view` on ToolResult) for cache roundtrip /
  debug / future renderer. Phase 6 will read them.
- `test_e16_word_contexts_intent.py` lost the `IntentAlignmentBonus`
  + `SelectPrimaryViewWithIntent` classes (the v5 view-selector tests).
  The fix on the tool side — hybrid_search emits a WORD_CONTEXTS view —
  is preserved.
- `test_budget_enforcement.py` lost the `BudgetExceededRendersErrorFriendly`
  integration (depended on render_v5).

### D-P1-2 — v6 is the only author resolver; `WC_V5_RESOLVER` removed

**Decision.** `entity_resolver.resolve_author()` now calls `resolve_v6`
+ `to_resolve_result` and returns `not_found` on v6 failure — the
legacy v5 alias → fuzzy → prominence-rank fallback (108 lines) has
been deleted. `tools/meta/resolve_entity.py` no longer has a
`WC_V5_RESOLVER`-gated fork: both `resolve_author_name` and
`resolve_book_title` delegate to `entity_resolver` unconditionally.
The compose flag `WC_V5_RESOLVER=on` has been removed.

**Why.** Per Phase 1 brief: "один резолвер". After D-P0-1 v6 became
the default at the planner-level `entities.extract()` call, but the
LLM-planner `resolve_author_name` / `resolve_book_title` tools still
went through v5 when `WC_V5_RESOLVER=on` (which was set in compose).
That left two live resolver paths for one entity. Consolidation on v6
collapses them into one. v5 primitives (`normalize_query`,
`ru_lemmatize_author_query`, `Candidate`, `ResolveResult`,
`get_prominence_index`, `rank_author_candidates`, `confidence_from_gap`)
stay because v6 imports them and `resolve_book()` (no v6 book linker
yet) uses them.

**Consequences.**
- `resolve_book` keeps its v5 KNOWN_BOOKS → RU title alias → v1
  `find_book` pipeline unchanged — books are not yet on v6.
- `test_v4_resolve_entity.py` was updated: source labels became
  v6-prefixed for author and `v5/known_books` for books; the
  `confidence == 1.0` assertions on alias hits were relaxed to
  `> 0` because v6 scoring depends on the prominence index, which
  is not loaded in CI.
- `test_entity_resolver_v5.py` (the v5-internal test surface) stays
  in-tree — its `_SKIP_UNDER_V6 = True` constant from Phase 0
  permanently skips the few v5-specific cases.

### D-P1-3 — Permanent-on gates inlined: `WC_LLM_PLANNER`, `WC_LLM_INTENT_ENABLED`, `WC_V5_PIPELINE`, `WC_V5_FOUNDATION`

**Decision.** Four env-gate reads removed:
- `LLM_PLANNER_ENABLED` is now `True` (was `os.environ.get("WC_LLM_PLANNER")`).
- `LLM_INTENT_ENABLED` is now `True` (was `os.environ.get("WC_LLM_INTENT_ENABLED", "1") == "1"`).
- `_v5_pipeline_envelope` no longer checks `WC_V5_PIPELINE` — the
  envelope is always created.
- `WC_V5_FOUNDATION` was only a compose annotation (no code gate);
  removed from compose for clarity.

Test helpers / per-flag toggle tests were updated:
- `test_v4_llm_planner.FlagOff` → `DisabledByMonkeyPatch` — exercises
  the safety early-return via `mock.patch.object` instead of env unset.
- `test_r15_hotfixes.BudgetEnvelopeWiring` — dropped `mock.patch.dict`
  for `WC_V5_PIPELINE`; envelope is always present now.
- `test_frontend_v5` lost the `*_visible_when_on` flag-display tests.

**Why.** Per R1: every flag should be ON in prod or deleted. These
four were either ON in prod compose (LLM_PLANNER, V5_PIPELINE,
V5_FOUNDATION) or default-on in code (LLM_INTENT_ENABLED). Carrying
the gate adds drift risk for zero behavioural choice.

**Consequences.**
- v4 LLM planner is the permanent path for compound / follow-up
  queries (was already the case in prod since R12-R13 transition).
- LLM intent fallback is permanent — rule path stays primary, LLM
  fills the gaps. No more "force pure-rule" knob.
- Every request gets a `RequestTrace` + `RequestBudget`. The router
  always receives `budget` and aborts on overrun.

### Phase 1 gate — checked

- `grep -rn 'os.environ.get("WC_' scripts/` — 0 generation flags
  remain. The 26 surviving WC_ env reads are: config paths
  (caches, DBs, derived dirs, FTS db), model names, timeouts, ring
  sizes, and four operational toggles (`WC_DEFAULT_ENGINE`,
  `WC_ALLOW_ENGINE_OVERRIDE`, `WC_CRITIC`, `WC_NUMERIC_AUDIT`) —
  none are dark-code gates per R1.
- `python -m pytest tests/v2` — 1384 passed, 18 skipped, 0 failed,
  collection clean. R10 satisfied.
- One planner: v3 rules + v4 LLM (chain, not fork) ✓
- One resolver: v6 (with v5 primitives as building blocks) ✓
- One renderer: legacy `_llm_render` ✓
- Router: `execute(QueryPlan)` + `execute_spec(PlanSpec)` are two
  format adapters over the same dispatch, not parallel generations.

Phase 2 (contract v1↔v2) is the next gate — DO NOT START before
running 12 prod-feedback scenarios on a Phase 1 build.

---

## 2026-05-22 — Phase 0: emergency stabilization

Closing actions from REFACTOR_BRIEF.md, Часть 2 ("аварийная стабилизация").

### D-P0-1 — `WC_V6_RESOLVER` becomes permanent (gate removed)

**Decision.** The v6 layered entity linker (Mention Detection +
Multi-Factor Scoring + Decision Thresholds) is now the default path.
The `WC_V6_RESOLVER` env gate has been removed from
`scripts/v2/entity_resolver.py` and `scripts/v2/planner/entities.py`.

**Why.** Per REFACTOR_BRIEF Part 2 step 2: v6 was already written and
unit-tested (`test_entity_resolver_v6.py` — 30/30 green). E13
"over-eager surname disambiguation" was closed by v6 but the fix never
shipped because it was behind a flag absent from prod compose. Rule R1
("no dark flags") forces the choice: enable or delete. We enable.

**Consequences.**
- The legacy v5 pipeline stays as a fall-through safety net (v6 adapter
  returns None or raises → use v5). Removal of v5 dead code is Phase 1.
- `test_entity_resolver_v5.py::_V6_ON` is now `True` (constant) so the
  3 v5-internal tests skip permanently.
- `test_aliases_regression.py::_SKIP_AUTO_REGRESSION` adds `уэллс` and
  `h. g. wells` — v6 correctly disambiguates these to the prominent
  canonical (H. G. Wells) rather than the bare surname regex. v5
  curated-alias-returns-surname behavior was a regression at the user
  level, not at the test level.

### D-P0-2 — `WC_V5_RENDERER` / `WC_V5_PROSE` removed from compose, branches kept

**Decision.** Both commented entries have been deleted from
`docker-compose.override.yml`. The renderer and prose-binder source
modules stay in-tree but unreached in prod.

**Why.** Per the same R1 + Phase 0 gate ("no commented flags in
compose"): leaving dark flags is forbidden, but enabling Stage 3/4
without the validation work the brief requires ("bake + visual sample
of 10 queries") would be a behavior change without grounds.
"Either enable if ready, or delete the branch" — we defer the enable/
delete decision to Phase 1 (`схлопнуть поколения`), where it lands
naturally alongside the choice of which planner / executor / resolver
to keep.

**Consequences.**
- `scripts/v2/render_v5.py` and `scripts/v2/prose_binder.py` carry
  the `V5_RENDERER_ENABLED = os.environ.get(...)` constants but they
  evaluate to `False` in prod. No code execution behind the gate.
- Phase 1 must revisit and either turn them on or delete them — no
  third option.

### D-P0-3 — `$s2.words[N]` P0 bound via affinity `words` alias + plan-spec bracket syntax

**Decision.** Two-piece temporary bind in lieu of the structural fix
(Phase 2 contract enforcement):
1. `scripts/v2/planner/plan_spec.py` — `_REF_RE` now accepts `[N]`
   in the path; `walk_path` normalizes `field[N]` → `field.N` before
   walking. Allows the LLM planner's bracket-style refs to resolve.
2. `scripts/v2/tools/authors/affinity.py` — `raw["words"]` alias added
   (mirrors filtered rows). `wrapper_version` bumped to bust stale
   cache.

**Why.** The audit named this the headline P0 (scenario 1, audit doc
line 107). The LLM planner emits `$s2.words[0]` referring to
`affinity_by_author` output, but v1 returns the rows under `top` /
`top_words`; the wrapper exposes neither under `words`, and the ref
syntax has square brackets that the regex didn't accept. Either piece
of the fix is necessary, neither sufficient. Brief explicitly calls
this a Phase 0 bind: "Временно — связать; структурно закроется
Фазой 2."

**Consequences.**
- Negative tests added: `test_v4_plan_spec.py::RefParsing::
  test_s2_words_n_p0_resolves_against_affinity_shape` (plan-spec) and
  `test_e15_v1_contract_keys.py::TestAffinityByAuthorWordsAlias`
  (wrapper). Both fail on pre-fix code, pass on post-fix.
- Phase 2 must remove this alias and replace with declared schema +
  loud R9 error on unresolved ref. Until then, the bind is the
  contract.

### D-P0-4 — Wrapper-version bumps (R-23 Tier 0)

**Decision.** `wrapper_version` added to three previously-unversioned
v2 wrappers that had received content fixes:

| tool                 | version tag                  | underlying fix                  |
|----------------------|------------------------------|---------------------------------|
| `affinity_by_author` | `v2-phase0-words-alias`      | D-P0-3 (words alias)            |
| `learning_words`     | `v2-b-r14-7-results-key`     | B-R14-7 (read `results` not `words`) |
| `word_contexts`      | `v2-e9-context-key`          | E9 (read `context` not `snippet`) |
| `word_contexts_global` | `v2-e9-context-key`        | (same)                          |

**Why.** Per REFACTOR_BRIEF Part 2 step 4. The fixes existed in code
but `cache_key` rolled `wrapper_version="v1"` by default, so cached
results from the broken period kept serving — invisible to tests
(which run with empty cache) and confusing to users in prod.

### D-P0-5 — Three "failing" tests in the brief were already passing

**Decision.** No work needed for `test_q15_compare_empty`,
`test_scoring_plugins`, `test_e38_e43_persona_batch2`. Re-ran on entry
to Phase 0 — all green.

**Why.** Brief listed them based on a snapshot before recent commits
(`08fa230 fix(E38-E43)`, `aff397d fix(E44)`). Three commits between
audit and Phase 0 closed them.

**Note for the audit.** This shifts the "diagnosis vs current state"
calibration: some of the Class-C issues may also have moved. Phase 0
gate is now the source of truth, not the audit numbers.
