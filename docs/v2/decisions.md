# decisions.md — wordcracker

> Log of structural decisions taken during the post-audit refactor
> (REFACTOR_BRIEF.md + AUDIT_2026-05-22_architecture_quality.md).
> One section per decision. Newest at the top.

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
