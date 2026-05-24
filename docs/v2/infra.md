# infra.md — wordcracker inventory of runtime env-vars

> Honest map of every `WC_*` (and adjacent) env variable read by
> production code, what sets it, and what its prod state actually is.
> Companion to [decisions.md](decisions.md). Goal: R8 ("читай живой
> путь") becomes mechanical — open this file, see the truth.
>
> **Snapshot.** 2026-05-24, HEAD `5b32530`.
> **Compose files audited.** `docker-compose.yml`, `docker-compose.override.yml`.
> **systemd drop-ins audited.** `systemd/wordcracker-chat.service.d/v2-engine.conf`.
> **Refresh policy.** Update on any add/remove of a `WC_*` read, on any
> change to the compose files, and on any deploy that touches the
> systemd unit. Predeploy harness (ADR-B4 §3 `--check-env`) will close
> this loop mechanically once it lands.

---

## 0. Summary

- **No feature flag selects between code generations.** All
  generation gates (`WC_V5_*`, `WC_V6_*`, `WC_LLM_PLANNER`,
  `WC_LLM_INTENT_ENABLED`, `WC_PLANNER_LLM_FALLBACK`,
  `WC_DEFAULT_ENGINE`, `WC_ALLOW_ENGINE_OVERRIDE`) have been removed
  from `scripts/` per D-P0-1, D-P1-1, D-P1-2, D-P1-3, D-P1-5.
  Verified: `os.environ.get("WC_V5_…"|"WC_V6_…"|"WC_LLM_PLANNER"|
  "WC_LLM_INTENT_ENABLED"|"WC_PLANNER_LLM_FALLBACK"|"WC_DEFAULT_ENGINE"|
  "WC_ALLOW_ENGINE_OVERRIDE")` returns zero hits in `scripts/`.
- **No commented-out flag in compose.** `docker-compose.yml` declares
  only `WC_IMAGE_TAG` (image tag template); `docker-compose.override.yml`
  declares only `WC_OLLAMA_NUM_CTX=16384`. The Phase 1 comment block at
  `override.yml:42-50` documents removed gates (no live `# WC_X=` lines).
- **One known dead-config drift, explicitly pending-removal.**
  Systemd drop-in `v2-engine.conf:22` still passes
  `-e WC_DEFAULT_ENGINE=v2` to a container that no longer reads it
  (D-P1-5). Disposition is set by D-S0-5: removal lands with S-F4 /
  S-B5 (when ADR-B3 deletes the drop-in wholesale and moves its still-
  live pins into the new compose service) or opportunistically with
  any earlier block that touches the same drop-in. Not "tolerated" —
  scheduled.
- **R1-permitted operational toggles (2).** `WC_CRITIC` and
  `WC_NUMERIC_AUDIT` — both default `"on"` in code, both unset
  everywhere (compose + systemd) so prod state = code default = on.
  Documented in D-P1-7. They switch a verification pass on already-
  rendered answers, not a code generation.

---

## 1. Variables set in compose

### `WC_IMAGE_TAG` — image tag template

| where | value |
|---|---|
| `docker-compose.yml:7` | `${WC_IMAGE_TAG:-latest}` |

- **Purpose.** Per ADR-B1 (Accepted, 2026-05-24): deploy-by-SHA. Deploy
  hook exports `WC_IMAGE_TAG=$(git rev-parse --short HEAD)` so a
  rollback is `WC_IMAGE_TAG=<prev-sha> docker compose up -d`.
- **Code reads.** None — consumed by Docker Compose's variable
  substitution, not by application code.
- **Status.** Active. `:-latest` fallback survives until ADR-B1 phase 3
  (deploy hook + drop fallback).

### `WC_OLLAMA_NUM_CTX` — Ollama context size override

| where | value |
|---|---|
| `docker-compose.override.yml:41` | `16384` |
| `scripts/v2/token_budget.py:67` | reads env, no default (caller guards) |

- **Purpose.** Bump `qwen3:14b` ctx from upstream default 8192 to 16384
  so the Christie 200-word case fits without ladder shrink (override.yml
  comment 36-41).
- **Status.** Active in prod, set by override.

---

## 2. Variables set in systemd drop-in

`systemd/wordcracker-chat.service.d/v2-engine.conf:22` resets ExecStart
and re-adds three vars via `docker compose exec -e`:

| var | value in drop-in | code read | status |
|---|---|---|---|
| `WC_DEFAULT_ENGINE` | `v2` | **none** — deleted in D-P1-5 | **DEAD, pending-removal (D-S0-5).** Passes to container, ignored. Removed wholesale when S-F4 / S-B5 retires the drop-in (or opportunistically earlier if another block touches the same file). |
| `WC_LLM_MODEL` | `wordcracker:v2` | `rag_v2.py:43`, `llm_planner.py:78`, `llm_intent.py:63` | **Active** — pins the chat/critic/planner model identity in prod. |
| `WC_CRITIC_MODEL` | `wordcracker:v2` | `critic.py:38` (falls back to `WC_LLM_MODEL`) | **Active** — explicit pin even though fallback would land on same value. |

---

## 3. Variables read by code, NOT set anywhere

These either have working code defaults or are operator-only overrides.
Prod state = the default in column 3 unless an operator exports otherwise.

### 3.1. Operational toggles (R1-permitted, see D-P1-7)

| var | where | default | prod state | purpose |
|---|---|---|---|---|
| `WC_CRITIC` | `scripts/v2/critic.py:40` | `"on"` | **on** | Critic LLM verification pass over every rendered answer. |
| `WC_NUMERIC_AUDIT` | `scripts/v2/numeric_audit.py:31` | `"on"` | **on** | Programmatic check that numbers in the answer trace back to tool data. |

**Why allowed (R1 carve-out).** Neither gates a code generation or a
dead branch. Both switch between "run verification pass" and "skip
verification pass" on an already-produced answer. Operationally
useful: when the critic LLM is degraded (Ollama under load, model
swapped, audit drift), `WC_CRITIC=off` is the fast-disable path —
deleting the off-branch would require a code change + deploy.

### 3.2. Model identity (overridable; prod pins via §2 drop-in)

| var | where | default |
|---|---|---|
| `WC_LLM_MODEL` | `rag_v2.py:43`, `llm_planner.py:78`, `llm_intent.py:63` | `"qwen3:14b"` (prod: `wordcracker:v2` via drop-in) |
| `WC_CRITIC_MODEL` | `critic.py:38` | falls back to `WC_LLM_MODEL` (prod: `wordcracker:v2`) |
| `WC_LLM_PLANNER_MODEL` | `llm_planner.py:77` | falls back to `WC_LLM_MODEL` |
| `WC_LLM_INTENT_MODEL` | `llm_intent.py:62` | falls back to `WC_LLM_MODEL` (its inner default is `"wordcracker:v2"`) |

Model drift class is the subject of pre-staged ADR-C1 in `decisions.md`.

### 3.3. Timeouts (seconds, ints/floats)

| var | where | default |
|---|---|---|
| `WC_CRITIC_TIMEOUT` | `critic.py:41` | `30` |
| `WC_LLM_PLANNER_TIMEOUT_S` | `llm_planner.py:81` | `30` |
| `WC_LLM_INTENT_TIMEOUT_S` | `llm_intent.py:65` | `8` |
| `WC_LLM_PLANNER_NUM_PREDICT` | `llm_planner.py:84` | `1200` |

### 3.4. Audit tuning

| var | where | default |
|---|---|---|
| `WC_AUDIT_MIN` | `numeric_audit.py:33` | `5` |
| `WC_AUDIT_TOL` | `numeric_audit.py:34` | `0.10` |

### 3.5. Paths (container-rooted)

| var | where | default |
|---|---|---|
| `WC_V2_CACHE_DIR` | `cache.py:43` | `/data/v2_cache` |
| `WC_V2_PROFILES_DB` | `profiles/store.py:25` | `/data/v2_profiles/profiles.sqlite` |
| `WC_V2_FEEDBACK_DIR` | `feedback.py:35` | see `feedback.py` |
| `WC_V2_LOG_DIR` | `observability.py:49` | see `observability.py` |
| `WC_RAW_TEXT_DIR` | `corpus_version.py:26`, `tools/corpus_meta/overview.py:28` | `/workspace/raw_text` |
| `WC_CHROMA_PATH` | `tools/corpus_meta/overview.py:26` | `/workspace/chroma_db` |
| `WC_CHROMA_COLLECTION` | `tools/corpus_meta/overview.py:27` | `gutenberg-index` |
| `WC_RSYNC_DIR` | `tools/corpus_meta/overview.py:29` | `/workspace/gutenberg_raw` |
| `WC_DERIVED_DIR` | `tools/corpus_meta/overview.py:30` | `/workspace/spgc/derived` |
| `WC_FTS_DB` | `tools/search/lexical.py:127` | see `lexical.py` |

### 3.6. Sizes / capacities

| var | where | default |
|---|---|---|
| `WC_V2_RING_SIZE` | `observability.py:52` | `256` |

### 3.7. Test gates (not runtime — only affect test execution)

| var | where | meaning |
|---|---|---|
| `WC_GOLDEN_LIVE` | `tests/v2/test_golden_v5.py:415` | `=1` enables live golden suite (needs Ollama). ADR-B4 §3 sets this in predeploy. |
| `WC_CONTRACT_LIVE_V1` | `tests/v2/test_v1_contracts.py:197` | `=1` enables live v1 contract probes. |

### 3.8. Pre-deploy probe

| var | where | default |
|---|---|---|
| `WC_PROBE_BASE_URL` | `scripts/predeploy_probe_suite.py:360` | `DEFAULT_BASE_URL` |

---

## 4. What this audit confirms vs. what stays open

### Confirmed by code grep on HEAD `5b32530`

- D-P0-1 — `WC_V6_RESOLVER` gate removed: ✓ no `os.environ.get("WC_V6_…")` in `scripts/`.
- D-P0-2 / D-P1-1 — `WC_V5_RENDERER`, `WC_V5_PROSE` removed; `render_v5.py`, `prose_binder.py` deleted: ✓ files absent.
- D-P0-3 — `$s2.words[N]` bound; negative tests present: ✓ `test_v4_plan_spec.py:180`, `test_e15_v1_contract_keys.py:363`.
- D-P0-5 — three historic test failures pass on HEAD: ✓ confirmed today (`test_q15_compare_empty`, `test_scoring_plugins`, `test_e38_e43_persona_batch2` — all green).
- D-P1-2 — `WC_V5_RESOLVER` removed; v6 only resolver: ✓ no `os.environ.get("WC_V5_RESOLVER")` in `scripts/`.
- D-P1-3 — `WC_LLM_PLANNER`, `WC_LLM_INTENT_ENABLED`, `WC_V5_PIPELINE`, `WC_V5_FOUNDATION` inlined: ✓ no reads in `scripts/`.
- D-P1-5 — engine-selection flag removed from `chat_server.py`: ✓ `chat_server.py:35-36` is the tombstone comment; no `os.environ.get("WC_DEFAULT_ENGINE"|"WC_ALLOW_ENGINE_OVERRIDE")` in `scripts/`.
- D-P1-6 — `entity_resolver.py` is a re-export shim; logic in `entity_resolver_v6/` + `book_resolver.py`: ✓ `entity_resolver_v6/{types,normalize,prominence,legacy_fuzzy}.py` and `book_resolver.py` all present.
- D-P1-7 — `WC_CRITIC` / `WC_NUMERIC_AUDIT` documented as default-on toggles: ✓ defaults match code; both unset in compose/drop-in.
- D-P1-8 — router collapsed to `execute` + `execute_stream`: ✓ `router.py:105` and `router.py:349`; no `execute_spec`/`execute_spec_stream` definitions remain.
- ADR-B1 — `WC_IMAGE_TAG` templated in compose: ✓ `docker-compose.yml:7`.

### Reopens — none from this S-0 pass (in-repo scope only)

No "closed" claim in `decisions.md` was found unconfirmed against
current code. `backlog.md` is the operator's external tracker (docs
vault outside this code repository — referenced from
`AUDIT_2026-05-22_architecture_quality.md` §6 line 205 and
`REFACTOR_BRIEF.md` line 13, absent from disk and from git history
here, and per D-S0-4 not reachable by Claude Code by design).
Reconciliation of `backlog.md` entries against the verified
D-P0 / D-P1 series and against this `infra.md` is on the tracker
owner. The prior R6 run confirmed E13 and W-1 closed in prod, so the
external tracker's state is most likely already consistent — but the
in-repo S-0 pass does not attest to it.

### CI surface

- `predeploy.yml` jobs touching `tests/v2` (ubuntu-latest):
  - `test-collect` — `pytest tests/v2 --collect-only -q` (R10 gate).
  - `test-cache-race-linux` — `pytest tests/v2/test_cache_concurrent_writes.py -v`
    (D-S0-2 — canonical Linux gate for the cache race-safety contract
    while `CacheWriteDiskRaceSafe` is `@skipIf(win32)`).
- Full-suite `pytest tests/v2` execution on Linux is **not yet a CI
  gate**. ADR-B4 §1 plans to make it one as part of the predeploy
  harness. Until then, full-suite runs happen on developer machines.

### Open follow-ups (out of S-0 scope)

- **D-S0-5.** `v2-engine.conf:22` exports the dead var
  `WC_DEFAULT_ENGINE=v2`. Pending-removal — lands with S-F4 / S-B5
  (drop-in retired wholesale, live pins move into compose service)
  or opportunistically with any earlier block that touches the same
  file. Acceptance gate for S-F4 / S-B5: `grep -rn "WC_DEFAULT_ENGINE"
  systemd/` returns zero hits.
- **S-F1 — cache.py Windows portability re-decide.** When S-F1 reopens
  `cache.py` for AST-hash invalidation refresh / R-23 Tier 1A
  follow-ups, decide whether to add a small retry-on-`PermissionError`
  loop inside `_write_disk` (would drop the `@skipIf(win32)` on
  `CacheWriteDiskRaceSafe`) or keep the platform skip indefinitely.
  Decision depends on whether Windows local dev is a supported
  workflow at that point.
- **ADR-B5 deliverable.** `scripts/v2/env_registry.py` does not yet
  exist. When it lands, this `infra.md` should reference it (or be
  generated from it) so the registry is the single source of truth and
  this file becomes a rendered view.
- **R-23 Tier 1A.** Cache invalidation has moved past the
  `wrapper_version="v2-phase0-words-alias"` / `v2-b-r14-7-results-key`
  / `v2-e9-context-key` tags listed in D-P0-4. Current tags
  (`v5-w3-columns-hint`, `v6-e22-lang-query-fix`, etc.) reflect later
  fixes; AST fingerprinting (`cache.py:55-58`, `cache_key:117-121`)
  now folds wrapper + v1-callee source into the key, so manual bumps
  are no longer the primary invalidation mechanism. The D-P0-4 audit
  table is historical, not a live contract.
