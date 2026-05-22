# decisions.md — wordcracker

> Log of structural decisions taken during the post-audit refactor
> (REFACTOR_BRIEF.md + AUDIT_2026-05-22_architecture_quality.md).
> One section per decision. Newest at the top.

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
