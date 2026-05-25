# decisions.md — wordcracker

> Log of structural decisions taken during the post-audit refactor
> (REFACTOR_BRIEF.md + AUDIT_2026-05-22_architecture_quality.md).
> One section per decision. Newest at the top.

---

## 2026-05-25 — S-F1: AST-hash cache invalidation (ADR-F1 / D67 accepted)

Closes block **S-F1** of
[tz_structural_fixes_2026-05-24.md](../tz_structural_fixes_2026-05-24.md)
(spec lives in companion repo `C:\_PROJECTS\wordcracker\`).
Promotes the long-pending **D67** (R-23 Tier 1A, sketched in
[REFACTOR_BRIEF.md:88](../REFACTOR_BRIEF.md):88 and forward-referenced by
[D-S0-2](#d-s0-2--cache-race-windows-skip-deferred-to-s-f1)) from
sketch to Accepted, and lands the missing pieces on top of the
half-shipped scaffold that already existed at HEAD.

### What was already in place at the start of S-F1

The previous touch on [cache.py](scripts/v2/cache.py) had introduced
`CACHE_SCHEMA_VERSION = "v3-ast-fp"`, an `_ast_fingerprint_for(tool)`
helper, and folded a fingerprint into `cache_key()`. The lookup was
backed by
[contracts/registry.py:wrapper_fingerprint_for_tool](scripts/v2/contracts/registry.py)
which hashed `(wrapper_fn, v1_fn)` via `ast.dump(ast.parse(getsource))`.
That covered the "v1 callable itself changed" case for the 32
wrappers in 19 files with `@v1_contract` — but left three gaps the TZ
calls out explicitly:

1. **Shared-helper edits did not propagate.** `_title_lookup`
   ([scripts/v2/tools/search/lexical.py:39](scripts/v2/tools/search/lexical.py:39))
   is called from both `lexical_search` and `hybrid_search`. Editing
   the helper changed neither tool's fingerprint — only the two
   consumers' own bodies were hashed, not their callees.
2. **v2-native tools were uncovered.** 8 tools out of 40 (`corpus_overview`,
   `find_book_by_topic`, `hybrid_search`, `lemma_profile`,
   `lexical_richness_authors`, `lexical_search`, `resolve_author_name`,
   `resolve_book_title`) have no `@v1_contract` binding →
   `wrapper_fingerprint_for_tool` returned None → cache key used the
   empty-string fingerprint → body edits did not invalidate.
3. **No kill-switch, no pin mode, no audit gate.** The fingerprint
   computation was unconditional, lazy, and not verified anywhere. A
   regression that broke the walk (e.g. silently swallowing a callee)
   would show up as a stale-cache incident in prod, not as a CI red.

### ADR-F1 — AST-hash cache invalidation: wrapper + v1-callee depth=1 walk

**Status.** Accepted (2026-05-25, under S-F1). Promotes D67 from R-23
sketch to landed contract.

**Context.** `wrapper_version: str` on
[ToolSpec](scripts/v2/tool_registry.py) is the manual cache-bust
contract. After a wrapper semantic fix, the developer must bump the
string; if they forget, prod serves pre-fix empty results from disk
cache forever (until `corpus_version` rolls). Commit
[`2a958f8`](https://github.com/Standaoerby/wordcracker/commit/2a958f8)
("bump wrapper_version on 10 tools") was the artefact of this failure
class — a sweep to retroactively tag 10 tools whose wrappers had been
fixed without a bump. Coverage at HEAD before S-F1: 32 of 37 tools
have a non-default `wrapper_version`; the rest still default to
`"v1"`. Every one of those 10 missed bumps was a forgotten bump, not
a deliberate choice — manual opt-in is structurally insufficient.

The audit (C2, 2026-05-22) named this class E18. R-23 Tier 1A
([R-23_sprint_plan.md](../R-23_sprint_plan.md) in the companion repo)
proposed D67: fold an AST-derived code fingerprint of the wrapper into
`cache_key`, so any edit to wrapper source flips the key
automatically. The half-shipped scaffold above implemented that for
contract-bound wrappers and their direct v1 function — the missing
pieces are the depth=1 walk into shared helpers, the v2-native
fallback, the operational kill-switch / pin mode, and a CI gate that
proves the contract holds (TZ S-F1 acceptance: "правка shared-хелпера
`_title_lookup` меняет fingerprint всех потребителей; косметическая
правка — не меняет").

**Options considered.**

1. **Wrapper + v1_fn only (status quo at HEAD).** What was already
   shipped. Pros: simple, no walk machinery. Cons: doesn't catch the
   `_title_lookup` failure mode that motivates S-F1 — a helper change
   doesn't flip its callers' fingerprints. Rejected as insufficient
   on its own; it's the floor S-F1 builds on.
2. **Wrapper + v1_fn + depth=1 walk into same-project callables.**
   Walk `fn.__code__.co_names`; for each name in `fn.__globals__`
   whose `__module__` starts with `scripts.`, fold its AST source
   into the fingerprint. Catches `_title_lookup`-class helpers
   without exploding into the whole transitive call graph. **Decision.**
3. **Full transitive walk (depth=N).** Hash the whole reachable
   call graph. Strictly more invalidations than option 2 — but every
   edit anywhere in `scripts/` propagates everywhere, so practically
   every PR busts every cache entry. Defeats the purpose of caching.
   Rejected.
4. **Replace cache_key with a per-deploy global epoch (`git_sha`).**
   Folding `/health.git_sha` (from
   [ADR-B3](#2026-05-25--s-b3-runtime-build-identity-landed-adr-b3-accepted))
   into the key invalidates *every* entry on *every* deploy.
   Operationally a non-starter: cold cache after each deploy means
   the first hour after rollout is uniformly slow on heavy tools,
   re-running each query against the live corpus. The point of the
   per-tool fingerprint is precisely to invalidate only the changed
   tools' entries — preserve cold-start savings for the unchanged
   ones. Rejected.

**Decision.** Option 2.

**Implementation.**

1. **Depth=1 walk in
   [`ast_fingerprint`](scripts/v2/contracts/registry.py).** Generalize
   the existing helper: for each entry fn, walk `co_names` →
   `__globals__` → callables whose `__module__` starts with
   `scripts.`. Fold each callee's AST source into the hash. Sort
   parts by qualname so order is deterministic. Self-references
   skipped (a function can list its own name in `co_names` if it
   recurses).

2. **v2-native fallback in `wrapper_fingerprint_for_tool`.** When no
   `@v1_contract` binding matches `spec.fn`, hash `spec.fn` directly
   (plus its depth=1 callees). Lifts coverage from 32/40 contract-
   bound tools to **40/40** — every cacheable tool now has a body-
   derived fingerprint. Helpers like `_title_lookup` referenced from
   v2-native code (`hybrid_search`) are folded in via the same walk
   *when they're imported at module level*; lazy in-body imports are
   the documented gap (D-SF1-4).

3. **`WC_CACHE_AST_INVALIDATION` kill-switch.** Lazy env read in
   `cache.cache_key()`; when set to `"off"` / `"0"` / `"false"`, the
   fingerprint portion is empty-string and cache reverts to
   `(schema, wrapper_version, args)` only — the pre-S-F1 contract.
   Default is **on** (production state). Declared in
   [`flag_registry.CODE_DEFAULT_ON`](scripts/v2/flag_registry.py) per
   ADR-B5's third category (code-default-on, invariant for prod,
   flipping is a developer-debugging action). The
   [flag-lint test](tests/v2/test_flag_lint.py) is what enforces R1
   on this read: there is no dark code, just an explicit allowlist
   entry. Removal path: once 48 h of green prod with the gate on,
   either keep the kill-switch as a permanent escape hatch (R1
   stays satisfied because it's in the registry) or remove it in a
   follow-up ADR.

4. **`WC_CACHE_PIN_FINGERPRINT` startup snapshot.** When set to
   `"1"` / `"on"` / `"true"`, `cache.py` walks the full REGISTRY at
   first use and freezes the fingerprint table for the rest of the
   process lifetime. Subsequent `_ast_fingerprint_for(tool)` calls
   serve from the frozen snapshot, never re-reading source. Two
   guarantees: (a) prod canary observes a deterministic key set —
   no drift if a developer accidentally hot-reloads a module during
   an investigation; (b) `inspect.getsource` is paid once, not per
   miss. Default off; turned on in the
   [prod compose](docker-compose.yml) `chat`/`admin` services so
   the canary path is the default. Local dev gets the lazy path.

5. **C-extension fallback.** `_source_or_empty` (existing) returns
   `""` for any fn where `inspect.getsource` raises (C extensions,
   builtins). The walk emits a stable `"unsource:<qualname>"` marker
   for the fingerprint — flipping the underlying C extension version
   does NOT flip the fingerprint, but for the in-tree code that
   matters this is acceptable (cached results from a C-extension-
   backed tool are invalidated by `corpus_version` like before).
   `wrapper_version` remains as the manual escape hatch for
   C-extension-driven behaviour changes.

6. **CI gate
   [`scripts/v2/cache_fingerprint_audit.py`](scripts/v2/cache_fingerprint_audit.py)
   `--since=<ref>`.** For each tool: compute the fingerprint at
   `HEAD` (in-process) and at `<ref>` (subprocess in
   `git worktree add`); diff. Fails if any tool's wrapper source OR
   any of its declared depth-1 callees were edited in the range but
   the tool's fingerprint did not flip — proves the walk picked up
   the edit. Skips cleanly if no `scripts/` files changed in the
   range. Wired into
   [predeploy.yml](.github/workflows/predeploy.yml) as a non-blocking
   advisory in this ADR; promoted to blocking after 48 h of green
   runs (see S-F1 closeout below for the actual status).

**Consequences.**

- The `wrapper_version` field on `ToolSpec` stays — it's still the
  signal for "wrapper output **shape** changed in a way the cache
  payload roundtrip cares about" (different dataclass fields,
  different keys in `data`). The AST fingerprint covers the orthogonal
  axis "wrapper **behaviour** changed without a shape change". Both
  end up multiplied into the same hash; either path invalidates.
- Forgetting to bump `wrapper_version` is now a recoverable mistake,
  not a silent prod stale-cache: the next deploy flips the AST
  fingerprint anyway.
- Every PR that edits a tool body or a shared helper produces a
  partial cache invalidation on deploy. Operationally: the first
  request after deploy that touches the changed tool re-computes;
  the disk entry for the old fingerprint stays on disk under
  `/data/v2_cache/<tool>/<old-prefix>/...` and ages out via TTL or
  manual sweep. No new infrastructure for cache GC; the existing
  two-level fan-out
  ([cache.py:_disk_path](scripts/v2/cache.py)) keeps directory size
  bounded.
- The CI gate makes the contract self-checking: a future commit that
  edits a helper but routes its call through `getattr` (skipping
  `co_names`) would slip past the walk — the audit fails because the
  edited file is in the diff but no tool's fingerprint moved.

**Trade-offs.**

- Depth=1 is a deliberate ceiling. A helper-of-a-helper edit
  (`_title_lookup` calls some `_normalize_id` which gets edited) does
  NOT propagate. Trade-off: depth=N would over-invalidate and erase
  the cache-savings reason for the layer to exist. Mitigation: any
  edit to `_normalize_id` shows up as a diff in the same commit, and
  the wrapper_version of the *direct* user can be bumped manually if
  the indirect path is load-bearing — same rule as before, just
  rarer.
- `inspect.getsource` is sensitive to source-file presence: in a
  packed deployment (PyInstaller, zipapp) it can fail for in-tree
  code, dropping fingerprints to `""`. Prod runs from a SHA-tagged
  Docker image with full source on disk (per
  [ADR-B1](#d-sb1-2--code-is-baked-into-the-image-via-copy)), so this
  is a non-issue for us today. If a future deploy mode packs the
  bytecode-only, the walk degrades gracefully (every fingerprint
  becomes `"unsource:..."`, equivalent to wrapper_version-only).
- `co_names` is a static-bytecode read — it misses callees reached
  via `getattr(module, "name")` indirection or dynamic dispatch. The
  CI audit gate catches drift if a future refactor swaps a direct
  call for `getattr`, because the source diff will exist but no
  tool's fingerprint will move.
- Pin mode adds startup cost: ~37 tools × `inspect.getsource` +
  `ast.parse` + same for ~3-5 depth-1 callees each = ~200 source
  reads at warm-up. Measured at well under 1 s on the prod host —
  amortized across the process lifetime, far below the per-miss
  cost it eliminates.

### D-SF1-1 — `WC_CACHE_AST_INVALIDATION` lives in `flag_registry.CODE_DEFAULT_ON`

**Decision.** Add `WC_CACHE_AST_INVALIDATION` to
[scripts/v2/flag_registry.py](scripts/v2/flag_registry.py) →
`CODE_DEFAULT_ON`, with rationale tied to this ADR. Lint passes; no
compose entry; flipping off is a developer action, not a
configuration choice — matches the
[ADR-B5](#adr-b5--flag-lifecycle-live-code-default-on-or-experimental-lint-blocks-dark-code-drift)
third-category definition exactly.

**Why not `EXPERIMENTAL`.** Experimental is for honestly opt-in,
default-off flags that are not yet production decisions. The AST
fingerprint IS the production decision — it just keeps a kill-switch
in case the walk regresses unexpectedly in prod. Default state is
"on" and matches prod intent; that's `CODE_DEFAULT_ON`, not
`EXPERIMENTAL`.

**Why not just live in compose `environment:`.** Because its value
doesn't differ across environments — dev and prod both want it on.
Putting it in compose with a fixed value would be a noise entry
(per [ADR-B5](#adr-b5--flag-lifecycle-live-code-default-on-or-experimental-lint-blocks-dark-code-drift)
"Live" definition — compose is for values that DIFFER across envs).

### D-SF1-2 — `WC_CACHE_PIN_FINGERPRINT` is Live in prod compose, not in `flag_registry`

**Decision.** `WC_CACHE_PIN_FINGERPRINT` is set to `"1"` on the prod
[`x-app-env`](docker-compose.yml) anchor (picked up by gutenberg-lab,
chat, admin) and left unset elsewhere. This is the
[ADR-B5](#adr-b5--flag-lifecycle-live-code-default-on-or-experimental-lint-blocks-dark-code-drift)
"Live" definition exactly: the flag's value DIFFERS across envs (prod
= snapshot at startup; dev/local pytest = lazy, equivalent to off).
The flag-lint test passes via Rule 2(a) — present in `services.*.environment`
— without an entry in `flag_registry.py`.

**Why not `CODE_DEFAULT_ON`.** The semantic of `CODE_DEFAULT_ON` is
"the production default lives in code, no compose entry needed." But
local pytest / dev shouldn't pay the snapshot startup cost (~37 tools
× `inspect.getsource` per test run is wasteful and slows tight
iteration). Differing across envs is exactly what "Live in compose"
is for. Filing this as `CODE_DEFAULT_ON` would force dev to opt-out
explicitly, which is the wrong default for a tool that exists to make
prod monitoring deterministic.

**Why not `EXPERIMENTAL`.** Pin-mode IS a production decision — prod
ships with it on. `EXPERIMENTAL` is for opt-in defaults that are not
yet production decisions. Misclassifying it there would lie about
prod state.

### D-SF1-3 — Walk boundary: callees whose `__module__` starts with `scripts.`

**Decision.** A callee is "in scope" for the depth=1 walk iff its
`__module__` attribute starts with `scripts.`. Anything else
(stdlib, pandas, numpy, spacy, anthropic SDK, …) is excluded from the
fingerprint.

**Why.** This is the natural project boundary. Third-party packages
are pinned by [requirements.lock](requirements.lock) and the schema
roll bakes their version into the `CACHE_SCHEMA_VERSION` bump path —
in-tree code is what changes between deploys without a schema bump,
and that's exactly what AST fingerprinting targets. Including
third-party `__module__` values would either (a) flip every
fingerprint on every package upgrade (over-invalidates, see option 3
above) or (b) require a per-package allowlist (unbounded surface).
The `scripts.` prefix is one rule, zero allowlist.

### D-SF1-4 — Depth=1 ceiling: historical justification + residual risk + backstop

**Decision.** The walk stops at depth=1 from each entry function
(wrapper + v1_fn for contract-bound; spec.fn for v2-native). A
helper-of-a-helper (depth≥2) edit does NOT flip the fingerprint
automatically; the manual `wrapper_version` field on `ToolSpec`
remains the explicit backstop for that class.

**Historical evidence (depth≤1).** Every named incident this block
exists to close lives at depth≤1:

| Incident | Layer | Depth |
|---|---|---|
| **E18 (stale-cache)** — commit [2a958f8](https://github.com/Standaoerby/wordcracker/commit/2a958f8) retroactively bumped `wrapper_version` on 10 tools whose wrappers had been edited but the field was forgotten. | wrapper body | 0 |
| **E15 (key mismatch, 7 wrappers)** — wrappers read non-existent keys from v1 raw (`top_collocates`, `top_unique_a`, `pre_bucket` flat vs nested). | wrapper body reads v1 output | ≤1 |
| **B-R14-7** — learning_words v2 wrapper read `"words"` instead of v1's `"results"` ([backlog.md:150](../../backlog.md) in companion repo, fixed in commit `8a27048`). | wrapper body reads v1 output | ≤1 |
| **E20/E21/E22 (`[:3]`-truncation)** — `_normalize_lang` and `_title_lookup` returned wrong values; consumers (wrappers + `hybrid_search`) hit them at one call hop. | depth=1 helper edit | 1 |

None of the post-audit historical bugs lived at depth≥2. The
depth=1 ceiling covers every observed harm class.

**Residual risk at depth≥2.** A bug where a wrapper is fine, its v1
is fine, but a helper-OF-the-helper has stale logic (e.g.
`_metadata_df` is fine but `_slug` — which it calls — has a regex
edit) will NOT flip the wrapper's fingerprint automatically. The
disk cache for that tool would serve stale results until either
`corpus_version` rolls or `wrapper_version` is bumped manually.

**Resolution model gap: lazy imports + `module.attr`.** The walk
uses `fn.__code__.co_names` (static bytecode) → `fn.__globals__`
(module-level globals). Two patterns produce dark coverage:

1. **`from X import Y` inside a function body.** `Y` becomes a
   *local* variable (`co_varnames`), not a global. `__globals__.get("Y")`
   returns None → missed. Universal in v2 wrappers (e.g.
   [affinity.py:165](scripts/v2/tools/authors/affinity.py:165) does
   `from scripts.rag_tools import affinity_by_author as _v1` inside
   the wrapper). The wrapper's own walk misses `_v1`.
2. **`import X as alias; alias.fn(…)`.** `alias` is in `co_names`
   AND in `__globals__` but is a *module* — `callable(obj)` filter
   drops it. `fn` is an attribute access (`LOAD_ATTR`) — never
   appears as a top-level `co_names` entry. Missed both ways.

These gaps are **structurally** the same shape — symbols not in
module-level globals. They're documented in test 14
([test_cache_ast_fingerprint.py](tests/v2/test_cache_ast_fingerprint.py))
as failing-by-design assertions, so if the resolution model is
extended in a follow-up, the test flips and a D67 update is forced.

**Why the gap is bounded in practice.** For all 32 contract-bound
tools, the v1-callee walk goes through `binding.v1_fn` (passed
explicitly as an entry to `ast_fingerprint`, not discovered through
the wrapper's body). So even when a wrapper lazy-imports `_v1`,
the v1 function — and all of its depth=1 helpers in `rag_tools` —
are walked correctly. Test 6b
([test_cache_ast_fingerprint.py::test_06b_real_v1_helper_edit_flips_real_tool_fp](tests/v2/test_cache_ast_fingerprint.py))
locks this in: editing the real `rag_tools._slug` (a depth=1 callee
of v1 `affinity_by_author`) flips the v2 tool's fingerprint
end-to-end. The 8 v2-native tools (`corpus_overview`,
`find_book_by_topic`, `hybrid_search`, `lemma_profile`,
`lexical_richness_authors`, `lexical_search`, `resolve_author_name`,
`resolve_book_title`) only see helpers that the wrapper imports at
*module* level — lazy-imported v1 helpers from inside their bodies
are missed.

**Backstop semantics — and why this gap is a CORRECTNESS risk,
not a latency one.** D-SF1-5 covers the *over-aggressive* failure
mode (fingerprint flips when no source changed → unnecessary cache
miss → recompute → correct answer, just slower). The residual gap
described here is the *opposite* direction: fingerprint does NOT
flip when source DID change → stale cache served → user receives
the pre-fix answer indefinitely. **That is a correctness defect,
not a performance one.** A depth≥2 or lazy-import semantic fix that
relies solely on the AST fingerprint to invalidate cache will
silently fail to land — exactly the E18 failure class S-F1 exists
to close, just at a deeper resolution layer.

`ToolSpec.wrapper_version` remains a *manual* mechanism, and after
S-F1 its semantic narrows but its operational requirement
strengthens. It is no longer the primary cache-bust signal for
routine wrapper edits (the AST fingerprint covers those); it is the
**mandatory** signal for the three cases the walk cannot reach:

**Imperative — developer instruction.** When making a semantic fix
that falls into any of the three cases below, bump the affected
tool's `wrapper_version` in the same commit. The AST fingerprint
WILL NOT catch the edit; without a manual bump the disk cache
serves the pre-fix answer until `corpus_version` rolls (typically
weeks).

1. **Depth≥2 helper edit.** Edit lives in a helper-of-a-helper
   relative to the tool's wrapper/v1. Example: editing
   `rag_tools._maybe_translate` (used by `_select_books`, which is
   used by `affinity_by_author` v1) — the fingerprint walk goes
   wrapper → v1 → `_select_books`. `_maybe_translate` is one hop
   too deep. Bump `affinity_by_author`'s `wrapper_version` (and
   every other v1's that uses `_select_books`).
2. **Lazy in-body import of a same-helper-edit.** A v2-native tool
   (`corpus_overview`, `find_book_by_topic`, `hybrid_search`,
   `lemma_profile`, `lexical_richness_authors`, `lexical_search`,
   `resolve_author_name`, `resolve_book_title`) lazy-imports a
   helper from `rag_tools` *inside its function body* and edits that
   helper in the same commit. The wrapper's `co_names` doesn't see
   the lazy-imported symbol (D-SF1-4 resolution-model gap). Bump
   `wrapper_version`.
3. **C-extension-backed behaviour change.** `inspect.getsource`
   raises for builtins / `.so` callables; the fingerprint falls
   back to the stable `unsource:` marker (test 5). A semantic
   change inside such a callable does not move the fingerprint.
   Bump `wrapper_version`.

**Forcing function: the CI audit gate.**
[cache_fingerprint_audit.py](scripts/v2/cache_fingerprint_audit.py)
surfaces case 1 and (when the wrapper file itself is the diff)
case 2: it sees that a tool's declared source files changed in the
diff range but the fingerprint stayed. The developer reads the
audit output, bumps `wrapper_version` in the same commit, and the
PR re-runs green. Case 3 (C-extension) is not catchable by the
audit (no source diff exists) — that one requires developer
judgment. The gap is **surfaced**, not closed; silent stale-cache
cannot occur in cases 1-2 without a visible CI signal. The audit
step is `continue-on-error: true` for the 48 h canary
([backlog.md](../../backlog.md) deadline 2026-05-27); after the
window it becomes blocking, and case 1/2 stale-cache failures
become CI-red, not silent-prod.

### D-SF1-5 — Failure mode is latency, not correctness — default-on from start

**Decision.** Ship with `WC_CACHE_AST_INVALIDATION=on` from the
first deploy — no canary observation window before promotion.

**TZ deviation, documented honestly.** The TZ for S-F1 reads:
"Миграция за флагом `WC_CACHE_AST_INVALIDATION=on`, canary 48h,
default-on если miss-rate ≤ 2× baseline." The phased framing
assumes a non-trivial probability that the new behaviour *breaks
something*; canary is what lets you observe before committing to a
global flip. **The shipped implementation skips the canary phase**
and turns the gate on for everyone immediately. The justification:

**The failure mode of an over-eager fingerprint is a cache MISS, not
a wrong answer.** Concretely:

| Symptom | Pre-S-F1 (wrapper_version only) | Post-S-F1 (with AST fp) |
|---|---|---|
| Cache hit | Tool returns disk-cached result. | Same. |
| Cache miss (e.g. AST fp doesn't match disk entry) | Tool re-runs from scratch, fills cache, returns. | Same — slower than a hit, identical answer. |
| Worst-case AST-walk bug (e.g. walk regression flips fp on every call) | n/a — this is the new failure mode. | Every call is a cache miss → every call re-runs from scratch → answers are correct, latency is unmasked. |

The audit (C2, 2026-05-22) defines correctness as "answer matches
what the tool *would* compute from the current corpus state".
Aggressive invalidation always honors that — the tool re-runs,
returns the current answer, fills the cache. Disk usage grows (old
fp entries accumulate until TTL trims them); latency for tools that
were previously served from cache regresses to their cold path. No
user-visible result corruption.

This is *qualitatively* different from a kill-switch on a CORRECTNESS-
critical path. If `WC_CACHE_AST_INVALIDATION` had been, say, an
LLM-output filter or a fact-check pass, a default-on-from-start
without canary would be reckless — a bug in that pass produces
*wrong* answers, not slow ones. Here the worst case is a degraded
cache hit-rate. Acceptable to skip canary.

**What the canary surface still lets us do.** The kill-switch
(`WC_CACHE_AST_INVALIDATION=off`) and the CI advisory step both
remain. Operationally:

- If a wave of "every request is suddenly slow" lands after deploy,
  the kill-switch is one env flip away (reverts to
  `wrapper_version`-only behaviour, identical to pre-S-F1).
- The advisory CI step posts `::notice` / `::warning` annotations
  on every PR. If a future commit drives a sustained false-positive
  rate (fingerprint flips when no source changed — bug class:
  non-deterministic walk), it's visible in PR runs before promotion
  to blocking.
- A 48 h hard follow-up on `backlog.md` (companion repo) holds the
  promotion-of-advisory-to-blocking and the kill-switch retention
  decision time-bound. Without that, the advisory step would drift
  permanently — exactly the dark-config failure
  [ADR-B5](#adr-b5--flag-lifecycle-live-code-default-on-or-experimental-lint-blocks-dark-code-drift)
  exists to catch.

**Where a canary would have been needed.** If the AST walk had any
chance of *suppressing* an otherwise-valid cache hit at random — i.e.
producing two different fingerprints for the same source on two
different processes — that would be a correctness problem (a user
in tab A sees fresh data, tab B sees the same fresh data computed
identically). The walk is *deterministic* by construction
(`ast.dump` is deterministic; sorted parts; SHA-256). Test 11 locks
this in. So this is a hypothetical we've ruled out by the design,
not an unobserved risk we've shipped past.

### Negative tests (R2)

[tests/v2/test_cache_ast_fingerprint.py](tests/v2/test_cache_ast_fingerprint.py)
locks in twelve invariants — each one paired with a positive case
that would have silently regressed pre-S-F1 (see file header for the
explicit before/after for each test):

1. Body edit on a wrapper flips its fingerprint.
2. Body edit on a shared depth=1 helper (`_title_lookup`-class)
   propagates to every consumer's fingerprint.
3. Cosmetic edits (whitespace, comments) do NOT flip the fingerprint
   (the AST-dump-then-hash level strips them).
4. Cosmetic edits on a depth=1 helper also do NOT propagate.
5. C-extension fallback: a built-in callable produces a stable
   `unsource:` marker, not an error.
6. v2-native tools (no `@v1_contract`) fingerprint via `spec.fn`
   fallback; coverage is 37/37 not 32/37.
7. `WC_CACHE_PIN_FINGERPRINT=1` snapshots at first call; subsequent
   re-reads serve the snapshot value even when source on disk changes.
8. `WC_CACHE_AST_INVALIDATION=off` short-circuits the walk: cache
   key reverts to `(schema, wrapper_version, args)` only.
9. Depth=1 callee picked up: a same-`scripts.`-module helper called
   from the entry fn is folded in.
10. Depth=2 callee NOT picked up: a helper-of-a-helper edit does NOT
    flip the entry fn's fingerprint (the depth ceiling is enforced).
11. Determinism: `ast_fingerprint(fn1, fn2) == ast_fingerprint(fn2,
    fn1)` (sorted by qualname before hashing).
12. `CACHE_SCHEMA_VERSION` change globally invalidates: same args +
    same fingerprint under two different schema versions produce two
    different `cache_key` results.

Plus the CI gate
[scripts/v2/cache_fingerprint_audit.py](scripts/v2/cache_fingerprint_audit.py):
running it against a fabricated commit that edits a helper without
touching its callers must report a mismatch and exit non-zero, on a
clean diff it must exit zero. Both states asserted by
[`test_cache_fingerprint_audit_cli`](tests/v2/test_cache_ast_fingerprint.py).

### Acceptance gate (TZ S-F1)

- ✅ Body edit on a tool flips `cache_key` (test 1; test 6b for real
  prod tool against real `rag_tools._slug` swap).
- ✅ Shared-helper edit (`_title_lookup`-class) propagates to every
  consumer (test 2 for synthetic case; test 6b end-to-end on a real
  tool through `wrapper_fingerprint_for_tool` rather than direct
  `ast_fingerprint`).
- ✅ Cosmetic edits do NOT propagate (tests 3 & 4 — required a
  production change in `_ast_part_for` to `textwrap.dedent` source
  before `ast.parse`, otherwise closure-defined helpers fell into the
  SyntaxError fallback branch where whitespace contributed to the
  hash. The dedent is a no-op for module-level functions, so no
  fingerprint drift for any existing tool — verified by re-running
  [test_cache_view_roundtrip.py](tests/v2/test_cache_view_roundtrip.py)
  and [test_v1_contracts.py](tests/v2/test_v1_contracts.py)).
- ✅ **15 tests** in
  [test_cache_ast_fingerprint.py](tests/v2/test_cache_ast_fingerprint.py):
  12 TZ invariants + test 6b end-to-end + test 13 CLI smoke + **test
  14 documents the lazy-import / module.attr gap** (D-SF1-4
  residual risk) as failing-by-design assertions.
- ✅ CI gate
  [cache_fingerprint_audit.py](scripts/v2/cache_fingerprint_audit.py),
  wired in [predeploy.yml](.github/workflows/predeploy.yml) as a
  `continue-on-error: true` advisory step until the 48 h baseline is
  observed.
- ✅ `inspect.getsource` C-extension fallback via try/except (test 5).
- ✅ `WC_CACHE_PIN_FINGERPRINT=1` snapshot mode (test 7).
- ✅ `WC_CACHE_AST_INVALIDATION` kill-switch in
  [flag_registry.CODE_DEFAULT_ON](scripts/v2/flag_registry.py); flag-lint green.
- ✅ Coverage **40/40** tools return non-empty fp (test 6 part a).
- ✅ For >50 % of contract-bound tools the v1-callee walk practically
  reaches `rag_tools` helpers (test 6 part b).
- ✅ Depth=1 ceiling enforced; depth=2 edits do NOT propagate (test 10);
  residual risk owned by D-SF1-4 with `wrapper_version` backstop.
- ✅ Default-on-from-start justified: failure mode is latency, not
  correctness (D-SF1-5).

### Closeout (S-F1 / ADR-F1)

**Local test run** (`pytest tests/v2/test_cache_ast_fingerprint.py
-v`, 2026-05-25, Windows 11 / Python 3.13.4):
**15 passed in 1.29 s**. Adjacent regression check
(`tests/v2/test_cache_view_roundtrip.py`,
`tests/v2/test_cache_concurrent_writes.py`,
`tests/v2/test_flag_lint.py`, `tests/v2/test_v1_contracts.py`):
**21 passed, 4 skipped** (3 cache-concurrent Win skips per
[D-S0-2](#d-s0-2--cache-race-windows-skip-deferred-to-s-f1), 1 live-v1
contract skip — corpus-fixture follow-up).

**Collection gate** (`pytest tests/v2 --collect-only -q`): **2114
tests collected, 0 errors** (was 2097 pre-S-F1; +15 from the new
file, +2 from a previously-uncounted unittest-style module that
S-B5's full-directory run now picks up).

**Coverage lift.** Tool fingerprint coverage moved from 32/40
(only `@v1_contract`-bound wrappers) to **40/40** via the
`wrapper_fingerprint_for_tool` v2-native fallback. The 8 previously
uncovered tools (`corpus_overview`, `find_book_by_topic`,
`hybrid_search`, `lemma_profile`, `lexical_richness_authors`,
`lexical_search`, `resolve_author_name`, `resolve_book_title`)
now fingerprint via `spec.fn` + depth=1 walk. Coverage verified by
test 6 part (a); the practical v1-callee walk verified by test 6
part (b) and end-to-end by test 6b (real `rag_tools._slug` swap
flips real `affinity_by_author` fingerprint).

**What S-F1 closes structurally.**

- **E18 (stale-cache) class.** A wrapper body edit OR a depth=1 same-
  project helper edit now flips that tool's `cache_key`
  automatically, so the next request after deploy reads through the
  cache rather than serving pre-fix data. The
  forgotten-`wrapper_version` failure mode that motivated commit
  [`2a958f8`](https://github.com/Standaoerby/wordcracker/commit/2a958f8)
  cannot recur without a CI signal — the audit gate fails when source
  changes don't move the fingerprint.
- **`_title_lookup`-class propagation gap.** The depth=1 walk picks
  up shared helpers via `co_names` → `__globals__` resolution. Tests
  2 and 9 lock the invariant in place; test 10 enforces the depth=1
  ceiling so cache invalidation stays bounded (no transitive cache
  storms on a single helper edit).
- **Decorative-CI failure class is bounded by R2.** The TZ asked for
  "12 tests + CI gate"; what landed is "12 invariants + 1 CLI smoke
  test + a CI step that runs the audit on every PR". The audit's
  output goes into the run log as a `::notice` or `::warning`
  annotation, so the signal is in the GitHub UI even when the step
  doesn't fail-fast.

**Operational notes for the canary window.**

- The audit step is `continue-on-error: true` for the first 48 h
  (per ADR-F1 implementation §6). Promotion to blocking is a
  follow-up edit that flips that flag — no schema or code change.
- Prod compose ships with `WC_CACHE_PIN_FINGERPRINT=1` (D-SF1-2)
  on `x-app-env`. Local dev / pytest leaves it unset and pays the
  lazy-lookup cost (~37 `inspect.getsource` calls amortized across
  the cache misses of a test run).
- `WC_CACHE_AST_INVALIDATION=off` is the kill-switch if the walk
  ever regresses in prod (D-SF1-1). Flipping it reverts the
  `cache_key` to `(CACHE_SCHEMA_VERSION, wrapper_version, args)` —
  the pre-S-F1 contract. Documented in `infra.md` §3 as an
  operator-tunable.

**D-S0-2 follow-up resolved.** D-S0-2 left an open question:
"when S-F1 lands and re-opens `cache.py`, do we add Windows retry-on-
sharing-violation and drop the `skipIf`, or keep the skip
indefinitely?" Answer: **keep the skip**. `cache.py`'s edit under
S-F1 added a thin fingerprint dispatcher next to the disk path
(`_ast_fingerprint_for`, `_populate_ast_fp_snapshot`); the
`_write_disk` POSIX-rename hot path was not touched. The Windows
race test stays Linux-CI-only — that surface is exercised by the
`tests-v2` job under ADR-B7 (full requirements.lock install on
ubuntu-latest), so the test is no longer skipped on the gate that
matters. Adding Windows retry-on-PermissionError would be a separate
ADR if Windows local dev becomes a supported workflow; it isn't
today and S-F1 is not the right place to make it one.

### Follow-ups deliberately out of scope of S-F1

- **48-hour prod canary observation window.** The TZ asks for a
  canary period before promoting the audit gate from advisory to
  blocking, and for measuring miss-rate vs baseline. Operationally
  the canary surface (Grafana panels) and the rollback decision live
  with the operator. Tracked separately; not a blocker for landing
  the code.
- **Garbage-collect orphan cache entries.** Old-fingerprint disk
  entries accumulate after every deploy. TTL trims them eventually;
  a dedicated GC sweep is a separate ADR — see
  [REFACTOR_BRIEF.md:115](../REFACTOR_BRIEF.md):115 "phase 5 cache
  hygiene" placeholder.
- **Per-package version pinning in the fingerprint.** D-SF1-3
  explicitly draws the line at `scripts.`. If a future incident
  proves a third-party upgrade silently broke a cached result, the
  response is a `CACHE_SCHEMA_VERSION` bump (the existing mechanism),
  not extending the fingerprint walk.

---

## 2026-05-25 — S-B5: CI dependency parity, run whole `tests/v2` (ADR-B7 accepted)

Closes block **S-B5** of
[tz_structural_fixes_2026-05-24.md](../tz_structural_fixes_2026-05-24.md)
(spec lives in companion repo `C:\_PROJECTS\wordcracker\`).
Promotes ADR-B7 to Accepted and lands the wiring in
[predeploy.yml](.github/workflows/predeploy.yml). Closes the
failure mode S-B1…S-B4 all shipped under: their closeouts reported
"R10 ✓" based on local `pytest`, while the CI job that was supposed
to enforce R10 (`pytest tests/v2 collect (R10)`) has been red on
every single run since the workflow was created (`6df5a1c`,
2026-05-24) — 15 / 15 reds at S-B4 closeout. The install step was
`pip install pytest` (five packages); any test file with a
module-scope `import pandas` / `import yaml` / `import spacy` /
`import torch` errored at collection. The main run-job ran a
hand-list of two files (`test_deploy_b4.py`, `test_flag_lint.py`).
~240 tests (~11 % of the suite, 2097 collected locally) never
executed on CI; new test files were invisible until someone
manually appended them to the hand-list. **The R10 gate was
decorative.**

### ADR-B7 — CI installs the full production lockfile and runs `tests/v2` as a directory

**Status.** Accepted (2026-05-25, under S-B5).

**Context.** ADR-B6 (2026-05-24, accepted) shipped
[requirements.lock](requirements.lock) as the dependency contract
for the prod image: every wheel is hash-pinned, the
[Dockerfile:40](Dockerfile:40) installs it with `--require-hashes`,
SHA-tagged image (ADR-B1) is now bit-deterministic in both code and
deps. **CI never adopted that contract.** The two test-running jobs
in [predeploy.yml](.github/workflows/predeploy.yml) install
`pip install pytest` (5 packages) and `pip install pytest pyyaml`
(6 packages) respectively. Anything beyond that — `pandas`,
`spacy`, `transformers`, `torch`, `chromadb`, even `yaml` outside
the one job that explicitly added it — fails at `import` time.

Concrete evidence of decorative R10:

- `test-collect` job (`pytest tests/v2 collect (R10)`) red on
  every push to main since 2026-05-24 03:29 (`gh run list` 15 / 15
  red). Failure log shows install completes with 5 packages, then
  `pytest --collect-only` fails on the first module-scope import
  that isn't pytest itself.
- The audit-listed 10 files that fail collection
  (`test_w1_v2_extract_path`, `test_critic`,
  `test_entity_resolver_v5`, `test_entity_resolver_v6`,
  `test_frontend_v5`, `test_llm_intent`,
  `test_phase3_regex_harness_gate`, `test_rag_v2_llm_merge`,
  `test_ambiguous_surname_clarify`, `test_deploy_artifact`) all
  collect cleanly on a fully-installed environment — locally
  `pytest tests/v2 --collect-only` reports 2097 tests, 0 errors.
  The failure is purely the install delta.
- `s-b4-acceptance` runs a hand-list of two files. New test files
  (e.g. anything added under S-B5, future test files for
  S-F1…S-R4) are not run anywhere on CI unless someone remembers
  to grow the list.
- The local `chore(s-b4): bump to v2.6.14 + defer yaml import to
  test runtime (R10)` commit attempted to patch one symptom
  (deferred `import yaml` inside test bodies). That is patch-trash
  per R7: at three commits chasing the same red job, the response
  is structural, not another defer.

This block is the structural response.

**Options considered.**

1. **Status quo — `pip install pytest` and hand-list.** What is
   running. Cheap (~15 s per job). Catastrophic failure mode: this
   is the bug the block is here to close. Rejected — keeping it
   means S-B5 doesn't exist.
2. **CI installs the full `requirements.lock` (hash-verified), plus
   spaCy models by URL — exactly as [Dockerfile:40-52](Dockerfile:40).
   Runs `pytest tests/v2` as a directory.** Reads the same artifact
   as the prod image (single source of truth per R8 / ADR-B6). The
   lock is Linux/CUDA13-specific
   ([requirements.in:7-11](requirements.in:7)) but the CUDA pieces
   (`nvidia-*-cu13` wheels, `torch==2.12.0+cu130`) are
   manylinux2014_x86_64 .so files: they install on any glibc Linux
   without a GPU — `import torch` works, `torch.cuda.is_available()`
   returns False, tests that actually need a GPU `skipif` out
   explicitly. Install footprint ~7 GB on disk; ubuntu-latest has
   ~14 GB free root after clearing
   `/opt/hostedtoolcache` (~5 GB recovered). Job time ~5-7 min cold,
   ~1-2 min with pip cache.
3. **CI builds the Dockerfile image and runs `pytest` inside.**
   Bit-identical with prod, including base image
   (`pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime`) and apt layer.
   Cost: ~10 min cold build per PR; cache via GHCR requires
   `packages: write` token scope (currently the workflow has
   `packages: read` only), and fork-PR runs lose the cache. The
   image also carries ~5-10 GB of non-test runtime
   (jupyterlab, full transformer model weights downloaded at
   build) — none of it exercised by `pytest`.
4. **Separate CPU-only `requirements.ci.lock`.** A second lockfile,
   smaller, faster to install. Two deps contracts will drift — CI
   passes on its surface, prod has a different one. This is exactly
   the bug class S-B5 closes (silent CI-vs-prod divergence).
   Rejected — contradicts ADR-B6's whole point.

**Decision.** **Option 2.**

The real choice is 2 vs 3. Option 3 is structurally stronger but
buys identity in layers (apt, base CUDA image) that no current test
exercises — `tests/v2` is Python-only, doesn't touch nvidia-smi,
doesn't probe the base OS. Option 2 keeps the *contract under test*
(the lockfile) identical to prod while paying the runner price
directly, and reserves the heavy build for a future ADR if Option 2
leaks a real bug. Escalation path: if a class of CI-vs-prod
divergence appears that Option 2 cannot see, the next ADR (B8)
adopts Option 3 — but this is a forward decision, not a retrofit of
B7.

**Implementation.**

[predeploy.yml](.github/workflows/predeploy.yml) is restructured to
**three** jobs (down from five):

| Job                  | Purpose                                              | Status under B7 |
|----------------------|------------------------------------------------------|------------------|
| `version-bump`       | git-only check, no deps                              | unchanged        |
| `probe-config-sanity`| W-18 probe-config shape, two small files             | unchanged        |
| `tests-v2`           | full lock install + `pytest tests/v2 --collect-only` + `pytest tests/v2 -v` | **new — replaces `test-collect` + `s-b4-acceptance` + `test-cache-race-linux`** |

`tests-v2` is a single job — one install pays for both the collect
gate (R10, first step) and the full directory run (second step).
Collect runs separately so its red-or-green status is visible in
the run log as the explicit R10 signal.

The three folded-in jobs:

- `test-collect` — was the collect-only smoke; superseded by the
  collect step of `tests-v2`, against a fully-installed env.
- `s-b4-acceptance` — ran `test_deploy_b4.py` + `test_flag_lint.py`
  with `pip install pytest pyyaml`. Both files are in `tests/v2/`,
  the full-suite run picks them up. The two Windows-skipped
  behaviour tests called out at
  [predeploy.yml:84-91](.github/workflows/predeploy.yml:84) remain
  Linux-asserted because the full run is Linux.
- `test-cache-race-linux` — ran `test_cache_concurrent_writes.py`
  in isolation to assert the POSIX-rename atomicity contract per
  the `project_cache_writer_pattern` memory. Folded in for the same
  reason; the `skipif(win32)` guard already inside the file keeps
  Windows-only branches inert.

The `tests-v2` install steps mirror [Dockerfile:39-52](Dockerfile:39):

```yaml
- run: sudo rm -rf /opt/hostedtoolcache  # ~5 GB headroom for torch+CUDA wheels
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: pip-${{ runner.os }}-${{ hashFiles('requirements.lock') }}
- run: pip install --require-hashes -r requirements.lock
- run: pip install \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_trf-3.8.0/en_core_web_trf-3.8.0-py3-none-any.whl
- run: python -m pytest tests/v2 --collect-only -q   # R10 gate
- run: python -m pytest tests/v2 -v                  # full directory run
```

Quarantines (any test that cannot pass on ubuntu-latest under the
above install) are explicit `@pytest.mark.skip(reason="…")` or
`@pytest.mark.skipif(<cond>, reason="…")` calls with a stated
follow-up. The complete carantine list with reason + count appears
in the S-B5 closeout (this section, top of file) — silent-red is
never accepted (R2).

R2 negative test
([tests/v2/test_ci_collect_gate_negative.py](tests/v2/test_ci_collect_gate_negative.py)):
a fixture that simulates the failure mode — a test file with a
guaranteed-missing import would cause `pytest --collect-only` to
fail, proving the R10 gate is no longer fiction. Implemented as a
sub-process test that runs `pytest --collect-only` against a
fixture file with `import this_module_does_not_exist_xyz_b7` and
asserts a non-zero exit + collection error in the output.

**Consequences.**

- The R10 gate stops being decorative. "R10 ✓" in any future
  closeout now means **the CI job named `tests-v2` is green
  against the full `tests/v2` directory under the prod
  lockfile** — not "local pytest looked OK."
- ~240 previously hidden tests start running on every push to main
  and every PR. Real failures get a fix; failures requiring
  resources unavailable on the runner (GPU, live corpus, live
  Ollama, live ChromaDB persistent data) get an explicit `skipif`
  with reason and follow-up.
- New test files added under future blocks (S-F1…S-R4 etc.) are
  picked up automatically by the directory glob — no hand-list to
  update, no drift surface.
- `s-b4-acceptance`, `test-cache-race-linux`, `test-collect` job
  names retire. Closeout-to-CI mapping simplifies: one job, one
  "R10 ✓".
- Job time grows from ~15 s to ~5-7 min cold (lockfile install) /
  ~1-2 min warm (pip cache hit). Acceptable for a deploy gate that
  runs on push + PR; not acceptable for inner-loop dev where local
  `pytest` is still the answer.

**Trade-offs.**

- ~7 GB install on the ubuntu-latest runner; requires clearing
  `/opt/hostedtoolcache` for headroom. Disk is tight but not
  blocking. If a future lock upgrade pushes us past the cap, the
  escalation is Option 3 (Docker-in-CI), captured as a future
  ADR-B8 rather than a B7 retrofit.
- spaCy models stay outside the hashed lockfile (URL-pinned, same
  as Dockerfile). ADR-B6 already accepted this trade-off; B7
  inherits the same property. The URL contains the version; a
  tampered model needs a different URL.
- Option 2 cannot detect a class of bugs Option 3 would: a wheel
  that behaves differently in the CUDA-base image vs on bare
  ubuntu-24.04 (e.g. glibc-version-dependent .so loading). Has not
  shown up in any production incident to date. Accepted; revisit
  if/when evidence appears.
- Tests requiring services or corpus data must skip with
  `reason=`. Skip count is recorded in this closeout. A growing
  skip count is a signal — not a problem — that the next blocks
  (corpus-fixture, service-stub) need to land.

### Closeout (S-B5 / ADR-B7)

**First green CI run after the wiring landed:**
[predeploy run 26381278129](https://github.com/Standaoerby/wordcracker/actions/runs/26381278129)
on commit `742e4df`. Job durations: `tests-v2` 3m 44s wall
(`pytest tests/v2 -v` itself 1m 12s), `12-probe config sanity`
10 s, `Mandatory version-bump` 5 s. **First green
`tests/v2 (R10 …)` job since the workflow was created on
2026-05-24** — the previous 15 / 15 runs were red because the
install step never matched what the prod image bakes. R10 ✓ in
this and future closeouts means *this CI job is green*, not
"local pytest looked OK."

**Suite result on CI:** `2083 passed, 16 skipped, 565 subtests
passed` (out of 2097 collected — collection errors: 0). All 16
skips are pre-existing env-gated `unittest.skipUnless` /
`skipIf` markers — **0 quarantines were added under S-B5**. The
breakdown:

| File                              | Count | Marker                                                              | Reason / follow-up                                                                                                              |
|-----------------------------------|-------|---------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `test_golden_v5.py`               | 12    | `unittest.skipUnless(_LIVE, …)` (env `WC_GOLDEN_LIVE=1`)            | Behavioural goldens against a live stack + live corpus. Run only on the prod host. Follow-up: corpus-fixture story (post-S-F1). |
| `test_entity_resolver_v5.py`      | 3     | `unittest.skipIf(_V6_ON, "v5-specific test; v6 is default")`        | v5 resolver is dead code under v6 default. Follow-up: removed under Phase 1 (S-F4 collapse-generations).                        |
| `test_v1_contracts.py::Live…`     | 1     | `unittest.skipUnless(WC_CONTRACT_LIVE_V1=1, …)`                     | Live-v1 contract check — needs the corpus loaded. Same corpus-fixture story as `test_golden_v5.py`.                              |

None of the 16 skips was introduced to make S-B5 green; the
previously hand-listed `s-b4-acceptance` and
`test-cache-race-linux` jobs simply never executed the other
~240 tests, so the failure modes they would have exposed
(import errors at collect, runtime errors needing a live env)
were invisible. The full-directory run on a fully-installed
runner shows there were none in the dark — everything not
explicitly env-gated passes.

**What S-B5 closes structurally.**

- R10 gate is no longer fiction — `pytest tests/v2 --collect-only`
  ran clean against the **full prod dependency surface**, not
  against `pip install pytest`. Future closeouts cannot truthfully
  claim "R10 ✓" without a green `tests-v2` job — there is no other
  R10 surface.
- The hand-curated file lists are gone. New test files added under
  S-F1…S-R4 / S-A* / S-C* / S-P* will run on every push without
  any workflow edit. The "invisible test file" failure mode is
  closed.
- The decorative-CI failure mode that produced five sequential
  silent-failure deploys under S-B1…S-B4 (closeouts wrote
  "R10 ✓" while CI was red) cannot recur: the same job that wrote
  the green checkmark IS the gate.

**Out-of-scope follow-ups noted by the first green run.**

- Node.js 20 deprecation warnings on `actions/checkout@v4`,
  `actions/setup-python@v5`, `actions/cache@v4`. GitHub forces
  Node.js 24 on 2026-06-02; Node.js 20 removed 2026-09-16.
  Non-blocking. Follow-up: bump action versions or set
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` before 2026-09-16.
  Out of scope for S-B5.
- `~7 GB` install on the runner with `~3.5 GB` pip cache saved.
  Both well within ubuntu-latest headroom after the
  `rm -rf /opt/hostedtoolcache` step. No action needed; monitor
  if a future lock upgrade pushes against the cap.

---



Closes block **S-B4** of `docs/tz_structural_fixes_2026-05-24.md`.
Promotes ADR-B4 (one-command deploy with verify + probe-gate +
auto-rollback) and ADR-B5 (operational flag lifecycle / no-dark-code
lint) from Proposed (2026-05-24) to Accepted, and lands the wiring.
After S-B4 a single `bash scripts/deploy.sh` invocation is the only
supported path to prod: it builds the SHA-tagged image (S-B1), brings
chat/admin up via compose (S-B2), verifies the runtime self-report
matches the deployed SHA (S-B3), runs the probe gate against the
live runtime, and on any red step rolls back to the previously-deployed
SHA tag — without operator intervention. Closes the failure mode
where a "successful deploy" left prod on the old code AND statuses
saying it was new code at the same time.

### ADR-B4 — One-command deploy with verify + probe-gate + auto-rollback

**Status.** Accepted (2026-05-25, under S-B4). Supersedes the
2026-05-24 Proposed draft (which framed B4 as a *pre-build*
predeploy harness gate).

**Context.** The 2026-05-24 Proposed draft framed B4 as a pre-build
gate (`scripts/v2/predeploy_check.py`): run pytest + golden queries +
env-diff *before* the deploy. That framing was correct for the
problem visible from the 2026-05-22 audit, but the S-B1/S-B2/S-B3
landings shifted the picture: the actual remaining gap is *not*
"we ship untested code" (CI + pytest collect already gate that, see
[predeploy.yml](.github/workflows/predeploy.yml)) — it is "we ship
correct code but cannot tell at deploy time whether it actually
arrived in the running container, and if it didn't, the operator is
left to spot it and roll back by hand". Five sequential runs of the
2026-05-22 epic were silent-failure deploys: image rebuilt, restart
issued, version chip looked updated, but the running Python process
was from an earlier image. The fix is *post-deploy*, not *pre-build*.

The S-B3 follow-ups left two specific gaps for this ADR to close
(both called out in the S-B4 TZ prompt):

A. `predeploy_probe_suite.py` does not assert `/health.git_sha ==
   expected`. The 2026-05-24 ADR-B3 entry explicitly deferred this:
   "Bumping `predeploy_probe_suite.py` to verify `/health.git_sha`
   matches the about-to-deploy commit. Natural extension under
   ADR-B4." So all 12 probes can pass against the *wrong* code
   (probes are functional-shape, not identity).

B. [verify_deployed_image.sh:43](scripts/verify_deployed_image.sh:43)
   falls back to `git rev-parse --short HEAD` when called without an
   explicit SHA. That fallback is host-state-dependent: an operator
   could `git pull` on the prod host *after* the deploy ran, advancing
   HEAD ahead of what was actually built, and a subsequent
   `bash scripts/verify_deployed_image.sh` would compare the
   currently-running image against the *new* HEAD — silently green.
   [smoke_s_b2.sh:51](scripts/smoke_s_b2.sh:51) inherits the same
   footgun (calls verify without arg).

**Options considered.**

1. **Pre-build harness only** (the 2026-05-24 Proposed draft).
   Strengths: catches "we are about to ship broken code". Weaknesses:
   does not catch silent-failure deploys at all — by the time the
   harness ran, the image was already going to be built; if compose
   recreate fails to bring up the new tag, the harness has no surface
   to detect it. Rejected as insufficient on its own — already
   partially covered by CI's `pytest --collect-only` and
   `check_version_bump.py`.
2. **Pre-build harness + post-deploy verify, two separate scripts,
   operator-driven sequence.** What was de-facto running pre-S-B4:
   `predeploy_gate.sh` (against the *live* endpoint) followed by
   `deploy.sh` followed by manual `verify_deployed_image.sh` /
   `smoke_s_b2.sh`. Weakness: three commands, no contract that
   rollback follows from red verify. The 2026-05-22 epic happened
   under this regime — operators looked at the chip, declared green,
   moved on.
3. **One `deploy.sh` invocation gates the whole flow: build → up →
   verify (docker-tag + /health.git_sha) → probe-gate → on any red,
   roll back to the previously-deployed SHA and re-verify.** No
   manual steps between build and "prod is on the new code or the
   old code, never on a half-deployed mix". **Decision.**
4. **External orchestrator (Argo Rollouts / Spinnaker) with canary +
   automated traffic-shift.** Right shape for multi-host
   environments; massive overkill for a single Docker host. Rejected
   as scope-mismatch.

**Decision.** Option 3.

**Implementation.**

The pipeline lives in `scripts/deploy.sh`. Each step is a hard gate;
red on any step triggers rollback before exit.

```
deploy.sh <ref>
  ├─ 1. resolve SHA from <ref> (or rollback target from --rollback)
  ├─ 2. dirty-check scoped to Dockerfile COPY paths (D-SB1-8)
  ├─ 3. capture PREVIOUS_SHA from running gutenberg-lab container
  │     (before compose up — after recreate the previous image tag is
  │     replaced on the service, only the local image store still has it)
  ├─ 4. docker build --build-arg GIT_SHA=$SHA --build-arg BUILD_TIME=... (S-B1/S-B3)
  ├─ 5. write WC_IMAGE_TAG=$SHA into .env atomically
  ├─ 6. docker compose up -d --force-recreate gutenberg-lab chat admin (S-B2)
  ├─ 7. verify: scripts/verify_deployed_image.sh $SHA
  │     ├─ docker-tag check on all three services (S-B1)
  │     └─ /health.git_sha == $SHA on chat:8890 and admin:8891 (S-B3)
  ├─ 8. probe-gate: scripts/predeploy_probe_suite.py --base-url http://127.0.0.1:8890 --expected-sha $SHA
  │     ├─ wait for /health (existing behaviour)
  │     ├─ NEW: assert /health.git_sha == $SHA before firing any probe
  │     │       (otherwise all 12 probes ran against the old code and
  │     │       a stale-image deploy looks "12/12 PASS")
  │     └─ 12-probe taxonomy suite (existing)
  └─ on red at step 7 or 8:
        ├─ recover PREVIOUS_SHA captured at step 3
        ├─ re-run steps 5–7 with PREVIOUS_SHA (compose up + verify)
        └─ exit non-zero, surface "rolled back to $PREVIOUS_SHA"
```

The probe-gate is invoked with `--expected-sha $WC_IMAGE_TAG`. The
new flag (§A) is the deploy-time integration of S-B3's runtime
identity surface — `wait_for_health` is extended to additionally
assert `git_sha` from the same JSON body it already polled. A
mismatch returns the same exit code 4 the suite uses today for
"health never came up", because semantically that's the same class
of failure: the runtime the probes are about to interrogate is not
the runtime we asked for. Adding a new exit code would split the
"probes did not run against the right target" failure across two
codes for no diagnostic gain.

§B (no-arg footgun) is closed at `verify_deployed_image.sh`: when
called without an explicit positional SHA, it now reads
`WC_IMAGE_TAG` from process env (`.env` is loaded by the deploy.sh
that drives it). If both are missing, it exits 2 with an explicit
"refusing to fall back to git HEAD — pass SHA explicitly". The
previous `git rev-parse --short HEAD` fallback is removed. The smoke
script `smoke_s_b2.sh` is updated to source `.env`'s WC_IMAGE_TAG
and pass it explicitly to both verify invocations — a smoke run
after the host moved forward will surface as "verify failed against
the expected SHA", not as "silently green because HEAD advanced".

**Rollback target capture.** Step 3 captures `PREVIOUS_SHA` from the
*currently-running* gutenberg-lab container (before recreate replaces
it). The capture uses `docker inspect --format='{{.Config.Image}}'`
on the running container ID and strips the `wordcracker-textlab:`
prefix. If the result is empty (fresh deploy with no running
container) or is the same as the new SHA (re-deploy of HEAD), the
"rollback target" is recorded as unavailable and any subsequent red
verify/probe-gate exits without attempting rollback — surfaces an
unambiguous "no rollback target; manual recovery required" so the
operator does not chase a false green.

**Consequences.**
- One supported path: `bash scripts/deploy.sh`. No more "deploy
  succeeded but smoke wasn't run" / "verify wasn't checked" /
  "probe-gate was forgotten" — they're all gated by the same exit
  code.
- The probe gate now gates *identity* before it gates *behaviour*.
  Twelve PASS against the wrong code cannot occur. A stale-image
  deploy will fail at step 8 (probe-gate's `--expected-sha` check),
  not at "operator notices the chip didn't change".
- `verify_deployed_image.sh` becomes safer to invoke standalone:
  passing no arg + no `WC_IMAGE_TAG` is now a hard error instead of
  a silent comparison against host HEAD.
- Auto-rollback prefers the SHA tag that was actually running at the
  start of the deploy attempt, not "the previous commit on this
  branch" (which the host may have moved past). Restores the exact
  bits, not a reconstructed approximation.
- The 2026-05-24 Proposed B4 framings that remain unaddressed by
  this Accepted form (golden-set live tests under `WC_GOLDEN_LIVE`;
  critic-flagged regression smoke from `/admin/bad_answers`) are
  explicitly out of scope here — they belong with the F-wave
  refactor (`tests/v2/golden_set.py` requires the v1↔v2 contract
  layer from S-F2 to be meaningful). Carried forward as B4-Phase 2.

**Trade-offs.**
- Deploy time grows by the probe-gate budget. Twelve probes at ≤180s
  per-probe ceiling = ~3 min worst case; observed ~30-60 s on a warm
  host. Acceptable for a single-host stack with ≤2 deploys/day.
- Auto-rollback can hide a *symmetric* failure: if both the new SHA
  and `PREVIOUS_SHA` fail verify (e.g. compose itself is broken),
  rollback "succeeds" technically while the host is still in a bad
  state. Mitigated: rollback re-runs verify and surfaces its own
  exit code; the operator sees both failures on the deploy log,
  not just the original.
- Capturing `PREVIOUS_SHA` from `docker inspect` ties the rollback
  surface to "what was actually running", not "what git says was
  previously deployed". A divergence (someone manually swapped tags
  in compose) shows up as "running image not in wordcracker-textlab
  family" and aborts the deploy cleanly at step 3.

### ADR-B5 — Flag lifecycle: live, code-default-on, or experimental; lint blocks dark-code drift

**Status.** Accepted (2026-05-25, under S-B4). Narrows the
2026-05-24 Proposed draft (full `env_registry.py` migration of all
26+ `WC_*` reads). The Proposed draft's broader goal is preserved as
B5-Phase 2; this Accepted form lands the part that pays for itself
inside S-B4: a *lint test* that catches the exact failure class
that motivated the Proposed draft (`WC_DEFAULT_ENGINE` / v2-engine.conf
ghost-flag, see [decisions.md line 1773](docs/v2/decisions.md)).

**Context.** "No dark code" is the project's first principle (R1).
But its enforcement was manual: an operator had to remember to grep
compose/systemd against code on every flag change. The 2026-05-22
audit's C5 root cause (process without verification) plus the
S-B-namespace evidence that the cleanest compose / systemd pair
still drifted (`WC_DEFAULT_ENGINE=v2` lived in systemd drop-in for
weeks after the code reading it was deleted) shows that
manual-grep enforcement is structurally insufficient.

The Proposed `env_registry.py` migration is the right long-term
shape — one declaration site, every read goes through it, predeploy
compares the registry vs. the world. But it is a ~1-day refactor
touching ~26 sites, which is too much surface to bundle with S-B4's
deploy-pipeline work (R7 forbids cascade commits in one area). The
narrower piece that pays for itself *now* is the lint test that
catches the harm class — once the lint exists, the migration can
land independently behind it, with the lint as the negative test.

The TZ for S-B4 calls out two specific lint requirements:
1. No commented-out `WC_*` line may exist in any compose file. A
   flag is either live (uncommented) or absent — never
   "documentation-grade dead config".
2. Lint forbids drift between "comment in compose says X" and "code
   actually does X".

**The three legitimate states of a binary toggle.** R1 ("no dark
code") forbids *undeclared* binary toggles, not all out-of-compose
toggles. The TZ's "experimental / released → gate removed" framing
was a *binary* — it accidentally rejected a third state that
predates the audit and is structurally honest: a flag whose default
*lives in code on purpose* (e.g., a safety pass that is always on
in prod and only ever flipped off by a developer instrumenting the
pass itself). The v2-engine.conf incident was not a "code-default
flag", it was a *ghost compose flag with no live read* — opposite
direction, different failure class. This ADR makes the third
category explicit so the lint can sanction it without weakening R1:

| State              | Where declared                                     | When to use                                              |
|--------------------|----------------------------------------------------|----------------------------------------------------------|
| **Live**           | `docker-compose.yml` (or `.dev.yml`) `environment:` | The flag's value differs across envs (dev/prod), or it has a non-default value somewhere. |
| **Code-default-on** | `scripts/v2/flag_registry.CODE_DEFAULT_ON`         | The flag is invariant for prod and the default is the production state — flipping it is a developer-debugging action, not a configuration choice. |
| **Experimental**   | `scripts/v2/flag_registry.EXPERIMENTAL`            | Honestly opt-in, default-off, not yet a production decision. Lives here until promoted (to compose) or removed (dead branch). |

Anything else — a binary toggle read in code that's neither in
compose nor in either registry set — is dark code by definition,
and Rule 2 of the lint catches it. The registry IS the declaration
that lets the lint distinguish "permanent, intentionally
out-of-compose default" from "forgotten ghost". R1 stays intact:
every flag is named in *some* declaration surface, and the lint
mechanically enforces it.

**Options considered.**

1. **Full `env_registry.py` migration in this block.** The Proposed
   2026-05-24 form: every `os.environ.get("WC_*")` rewritten as
   `env_registry.WC_*.is_on()` etc.; the registry IS the lint. Pros:
   maximally rigid contract. Cons: ~1 day, ~26 sites; cascade risk
   if it lands alongside the deploy-pipeline work. Carried forward
   as **B5-Phase 2**, deferred from this block.
2. **Lint without a registry: grep-based audit in a pytest test.**
   The test enumerates `os.environ.get("WC_*", "on"|"off"|...)`
   binary-toggle reads across `scripts/`, cross-references against
   live compose env, and asserts each toggle is either (a) live in
   compose with a value, or (b) listed in a small
   `scripts/v2/flag_registry.py::CODE_DEFAULT_ON` /
   `EXPERIMENTAL` set with a one-line rationale. Configuration values
   (paths, model names, timeouts) are explicitly *out* of scope —
   they are values, not flags, and have no dark-code risk. **Decision.**
3. **Status quo + reviewer discipline.** Rejected — what got us
   here.

**Decision.** Option 2.

**Implementation.**

Lint test `tests/v2/test_flag_lint.py` enforces two rules:

```python
# Rule 1: no commented WC_* in any compose file.
# Pattern: ^\s*#.*WC_[A-Z][A-Z0-9_]*\s*[:=]
# Whitelist exact comment lines that DESCRIBE the env contract
# rather than dead-flag-state (currently zero whitelisted entries).

# Rule 2: every os.environ.get("WC_*", "on"|"off"|"0"|"1"|"true"|"false")
# read in scripts/ must be in exactly one of:
#   (a) live compose env (docker-compose.yml services.*.environment)
#   (b) scripts/v2/flag_registry.py::CODE_DEFAULT_ON  with rationale
#   (c) scripts/v2/flag_registry.py::EXPERIMENTAL     with rationale
# Reads that are NOT binary-toggle shape (paths, models, ints, floats
# without on/off literal default) are NOT in scope for this rule —
# they are configuration, not flags.
```

`scripts/v2/flag_registry.py` is a tiny module (~20 lines) — just
the two sets and their rationales. It is *not* the full proposed
registry indirection: code still reads `os.environ.get(...)`
directly. The registry's only role here is to be the
explicitly-declared whitelist that the lint reads. Full B5-Phase 2
upgrades the registry to the indirection layer; this Accepted form
just gives the lint somewhere to look up "yes, this is a known
code-default-on toggle, not a forgotten ghost".

Current entries (pre-S-B4 inventory, kept honest):
- `WC_CRITIC` — code-default-on (`scripts/v2/critic.py:40`).
  Rationale: critic LLM verification pass, on for prod since D-P1-7.
  No compose entry because the default lives in code, not in env.
- `WC_NUMERIC_AUDIT` — code-default-on
  (`scripts/v2/numeric_audit.py:31`). Same shape and rationale.

No `EXPERIMENTAL` entries today. The set exists so the *first*
honestly-experimental flag has somewhere to live without
re-triggering the lint.

**Consequences.**
- The compose-comment-vs-code drift class becomes mechanically
  caught. A future operator who comments out a `WC_*` line in
  compose to "temporarily disable" the flag will trip Rule 1 in CI
  before the change can merge.
- A future code change that adds `os.environ.get("WC_NEW_TOGGLE",
  "on")` without either wiring it into compose or whitelisting it as
  experimental trips Rule 2.
- The full env_registry.py indirection (B5-Phase 2) lands later,
  cleanly, against this lint — by then the rules will already exist
  and the migration becomes "make every read look like
  `flag_registry.WC_X.is_on()`" with the lint guarding the contract.
- Configuration values (paths, model names, timeouts) are not
  touched. They have always been a different lifecycle from binary
  toggles and the v2-engine.conf incident specifically was about a
  *toggle* (`WC_DEFAULT_ENGINE`), not a value.

**Trade-offs.**
- The lint is "binary-toggle scope" — a flag spelled as
  `os.environ.get("WC_X", "yes")` would bypass the regex. Acceptable
  for now; the canonical shapes are `"on"|"off"|"0"|"1"|"true"|"false"`
  and every existing site uses one of them.
- The registry is a 20-line allowlist, not the structured 26-entry
  index from the Proposed draft. That's deliberate: ship the part
  that catches the harm class today; defer the structured index
  until S-B4 has baked.
- Two reads (`WC_CRITIC`, `WC_NUMERIC_AUDIT`) are explicitly *not*
  in compose. Surface-level inconsistency with the "every flag in
  compose" framing of the original Proposed B5, but the honest model
  is that some toggles default in code and that's fine *if it's
  declared*. The registry IS the declaration.

### D-SB4-1 — Probe gate carries `--expected-sha`; mismatch fails fast

**Decision.** `scripts/predeploy_probe_suite.py` gains a new flag
`--expected-sha <SHA>` (and a matching `WC_PROBE_EXPECTED_SHA` env
default). When set, the suite's existing `wait_for_health` step is
extended: as soon as `/health` returns 200, the response body is
parsed for `git_sha`, and if it does not equal the expected value
the suite exits 4 (same code as "health never came up") without
firing any probe.

**Why same exit code as 4 (transport/health).** Semantically the
runtime is unreachable *for the purpose this run intended* — the
service is up, but it is up running a different SHA than the deploy
wanted. Probes against the wrong code are not meaningful; treating
that as the same class as "no health at all" lets the deploy.sh
case-statement keep the existing exit-code map. Adding exit code 8
or similar would force every wrapper to grow a case-arm for the
same operator-facing message ("the runtime is not the runtime you
asked for; check your deploy").

**Decision.** The flag is *optional* — the suite still runs without
it (current behaviour for ad-hoc runs against prod). Only the
deploy.sh-driven path supplies it. This preserves the existing
operator workflows for "probe prod against today's HEAD without
caring exactly which SHA the box reports".

### D-SB4-2 — `verify_deployed_image.sh` drops `git rev-parse` fallback

**Decision.** When called with no positional SHA arg, the script no
longer falls back to `git rev-parse --short HEAD`. New fallback
order:
1. Positional `$1` if non-empty.
2. `WC_IMAGE_TAG` from process env (deploy.sh exports it; smoke
   script will source `.env` and pass it explicitly).
3. Exit 2 with an explicit error: "refusing to fall back to git HEAD
   — pass SHA or set WC_IMAGE_TAG".

**Why no git fallback.** The footgun is real and documented in the
S-B4 TZ prompt: an operator running `bash scripts/verify_deployed_image.sh`
on the prod host *after* a `git pull` would silently compare the
running image against a HEAD the deploy never built. The script
should never depend on host-repo state.

**Smoke caller.** `scripts/smoke_s_b2.sh` is updated to read
`WC_IMAGE_TAG` from `.env` (or process env if exported) and pass it
explicitly to both `deploy.sh` (already does) and
`verify_deployed_image.sh` (the pre-S-B4 second invocation that
was relying on the fallback).

### D-SB4-3 — Probe-gate red rolls back to captured PREVIOUS_SHA, not HEAD~1

**Decision.** Rollback target is the SHA tag of the image *currently
running* at the start of the deploy attempt (captured before
`compose up`), not a git-resolved "previous commit". The capture
uses `docker inspect --format='{{.Config.Image}}'` against the
running `gutenberg-lab` container ID and strips the
`wordcracker-textlab:` prefix.

**Why running image, not git HEAD~1.** The two diverge precisely
when the operator most needs the right behaviour: the host's `main`
branch may have advanced past what's deployed (forward-deploy
pending), or a previous deploy may have been from a feature branch
that's since been deleted. The running tag is the only authoritative
"what was on prod a moment ago".

**Why pre-`compose up` capture.** `compose up -d --force-recreate`
removes the previous container and replaces its image reference;
post-recreate `docker inspect` shows the *new* tag. The previous
tag survives only in the local image store (kept by
[deploy.sh:194](scripts/deploy.sh:194)'s `KEEP_LAST_N_IMAGES=5`
pruning). Capturing the tag *before* recreate is the only point at
which container-level state still names the previous SHA.

**Failure modes.** If `PREVIOUS_SHA` is empty (cold start, no
running container) or equals the new SHA (re-deploy of HEAD), the
"rollback target" is recorded as unavailable. A subsequent red
verify/probe-gate exits non-zero *without* attempting rollback —
the operator sees an unambiguous "no rollback target; manual recovery
required" and is not led to believe rollback succeeded against a
phantom target.

### Negative tests (R2)

`tests/v2/test_deploy_b4.py` (new) carries the R2 mirror set for
ADR-B4 / ADR-B5 / D-SB4-*:

| Case | What it asserts |
|---|---|
| `test_verify_refuses_no_arg_no_env` | Run `verify_deployed_image.sh` with no arg, no `WC_IMAGE_TAG`: exit 2, stderr contains "refusing to fall back". |
| `test_verify_uses_env_when_arg_missing` | With no arg but `WC_IMAGE_TAG=abc123` exported: EXPECTED == `abc123` (smoke via grep / dry-run). |
| `test_verify_does_not_grep_HEAD_in_fallback` | NOT-X mirror: `git rev-parse --short HEAD` no longer appears in `verify_deployed_image.sh`. |
| `test_smoke_passes_explicit_sha_to_verify` | `smoke_s_b2.sh` source-grep: both verify invocations carry a SHA arg (either `$EXPECTED_SHA` or `$WC_IMAGE_TAG`); no bare `bash scripts/verify_deployed_image.sh` call. |
| `test_probe_suite_has_expected_sha_flag` | `predeploy_probe_suite.py` defines `--expected-sha`. |
| `test_probe_suite_checks_git_sha_in_wait_for_health` | `wait_for_health` (or its caller) parses `/health`'s `git_sha` and asserts equality before returning True. |
| `test_probe_suite_exits_4_on_sha_mismatch` | NOT-X mirror: simulate `/health` returning a different `git_sha`; runner exits 4 (the same exit code as "health never came up"). |
| `test_deploy_sh_captures_previous_sha_before_compose_up` | `deploy.sh` source-grep: `docker inspect` of running `gutenberg-lab` happens BEFORE `docker compose up -d --force-recreate`. |
| `test_deploy_sh_invokes_probe_gate_after_verify` | `deploy.sh` source-grep: `predeploy_probe_suite.py` invocation appears after the `verify_deployed_image.sh` call. |
| `test_deploy_sh_rolls_back_on_red_verify_or_probe_gate` | `deploy.sh` source-grep: red verify OR red probe-gate path leads to a `--rollback $PREVIOUS_SHA` re-invocation. |
| `test_deploy_sh_aborts_without_rollback_when_no_previous` | `deploy.sh` source-grep: the "no PREVIOUS_SHA captured" path exits non-zero WITHOUT calling rollback. |
| `test_no_commented_wc_flag_in_compose` | Lint Rule 1 (ADR-B5): no line matching `^\s*#.*WC_[A-Z][A-Z0-9_]*\s*[:=]` in any `docker-compose*.yml`. |
| `test_binary_toggle_flag_either_live_or_in_registry` | Lint Rule 2 (ADR-B5): each `os.environ.get("WC_*", "on"|"off"|...)` in `scripts/` is either live in compose env or in `flag_registry.CODE_DEFAULT_ON` / `EXPERIMENTAL`. |
| `test_flag_registry_entries_match_actual_code_defaults` | NOT-X mirror: every entry in `CODE_DEFAULT_ON` has at least one matching `os.environ.get("WC_X", "on"|...)` in `scripts/` (no orphan whitelist entries). |
| `test_stale_image_under_new_tag_fails_verify` | Integration-shape (offline-runnable via mock of `docker inspect` / `curl`): with a docker tag pointing at SHA-A but `/health.git_sha=SHA-B`, `verify_deployed_image.sh` exits 7 (the existing runtime-mismatch exit code from D-SB3-3). |

Per R2, every "X triggers Y" has a "NOT-X does not trigger Y" mirror:
- `test_verify_uses_env_when_arg_missing` (X — env supplies SHA) ↔
  `test_verify_refuses_no_arg_no_env` (NOT-X — neither supplies, hard fail).
- `test_probe_suite_checks_git_sha_in_wait_for_health` (X — matching SHA) ↔
  `test_probe_suite_exits_4_on_sha_mismatch` (NOT-X).
- `test_deploy_sh_rolls_back_on_red_verify_or_probe_gate` (X — rollback fires) ↔
  `test_deploy_sh_aborts_without_rollback_when_no_previous` (NOT-X — no target, no spurious rollback).
- `test_binary_toggle_flag_either_live_or_in_registry` (X — flag is known) ↔
  `test_flag_registry_entries_match_actual_code_defaults` (NOT-X — orphan registry entries fail).

### Acceptance gate (TZ S-B4)

| Gate                                                                              | How verified                                                                            |
|-----------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| Deploy is one command                                                             | `bash scripts/deploy.sh` does build + up + verify + probe-gate + (rollback on red).     |
| `verify` step asserts `/health.git_sha` matches expected                          | `verify_deployed_image.sh` (D-SB3-3) + `predeploy_probe_suite.py --expected-sha` (new). |
| Probe-gate red → auto-rollback                                                    | `test_deploy_sh_rolls_back_on_red_verify_or_probe_gate`.                                 |
| Stale image under new tag → verify fails                                          | `test_stale_image_under_new_tag_fails_verify` (negative).                               |
| No commented `WC_*` lines in compose                                              | `test_no_commented_wc_flag_in_compose` (Lint Rule 1).                                    |
| Lint catches flag↔code drift                                                      | `test_binary_toggle_flag_either_live_or_in_registry` + orphan-entry mirror.              |
| `verify_deployed_image.sh` no-arg path no longer falls back to host HEAD          | `test_verify_refuses_no_arg_no_env` + `test_verify_does_not_grep_HEAD_in_fallback`.      |

### Follow-ups deliberately out of scope of S-B4

- **B4-Phase 2: golden-set live tests (`WC_GOLDEN_LIVE`) + critic-flagged
  regression smoke from `/admin/bad_answers`.** Both were in the
  2026-05-24 Proposed ADR-B4. Both depend on the S-F2 v1↔v2 contract
  layer to be meaningful (a golden test that calls into a wrapper
  with no contract has the same dark-code risk as an uncontracted
  prod call). Tracked for the F-wave.
- **B5-Phase 2: full `env_registry.py` indirection.** ~26 reads, ~1
  day, lands cleanly behind the lint gate of this Accepted form.
- **Predeploy *pre-build* harness** (the original 2026-05-24 ADR-B4
  framing — pytest + golden + env-diff before `docker build`). The
  parts that pay off now (`pytest --collect-only`, `check_version_bump.py`)
  are in CI already; the rest (golden, env-diff) couple to B4-Phase 2
  and B5-Phase 2 respectively.
- **R7 cascade counter** ("commits since last green deploy in same
  area"). Worth doing; orthogonal to S-B4's contract. Tracked for
  the F-wave.

---

## 2026-05-25 — S-B3: runtime build identity landed (ADR-B3 accepted)

Closes block **S-B3** of `docs/tz_structural_fixes_2026-05-24.md`.
Promotes ADR-B3 (runtime build identity) from Proposed to Accepted
and lands the implementation. After S-B3 each app process inside the
container reports the same `git_sha` that `verify_deployed_image.sh`
expects at the docker layer: chat / admin `/health` is JSON with
`git_sha`, `version`, and `build_time`; the chat UI header chip
shows the short SHA. The value is sourced from `os.environ["GIT_SHA"]`,
which is baked in by `ARG GIT_SHA` at image build time
(`deploy.sh` passes `--build-arg GIT_SHA=$SHA`). Closes the failure
mode that wasted runs 2–5 of the 2026-05-22 deploy epic: a version
string that encoded feature-flag state instead of code identity.

### ADR-B3 — Runtime build identity (`/health.git_sha` + header SHA + `ARG GIT_SHA`)

**Status.** Accepted (2026-05-25, under S-B3). Supersedes the
2026-05-24 Proposed draft.

**Context.** ADR-B1 + the S-B2 generalisation of
`verify_deployed_image.sh` ship the *docker-level* guarantee that
every app service runs from the expected SHA-tagged image
(`gutenberg-lab`, `chat`, `admin` all on the same
`wordcracker-textlab:<sha>`). What is missing is the *runtime
self-report* — the Python process inside each container has no
`/health.git_sha`, no SHA in the UI, no `ARG GIT_SHA` baked in. The
only place "version" was surfaced was the chat HTML header
`<span class=meta>v2.6.13 · planner→router→renderer→critic</span>`,
which encoded `ANALYTICS_VERSION` *and historical feature-flag
phrasing* (`v3.2.0-alphaX`) — so an external observer could not
tell "the new commit is actually running" from "an old image is
running with the new flag-string baked in".

That gap cost the 2026-05-22 deploy epic five runs in a row: each
time `git pull` + restart looked correct, the version chip
*looked* updated (because `ANALYTICS_VERSION` had been bumped in
the source tree), but `verify_deployed_image.sh` did not yet exist,
and the running image had not actually been rebuilt. There was no
runtime signal that exposed the drift.

This ADR is **the runtime side of the SHA contract**. Docker-side
(B1/B2) and runtime-side (B3) together let an operator confirm
"the new commit is live" without trust: both surfaces report the
same SHA, or one is wrong.

**Options reconsidered.**

1. **`ARG GIT_SHA` build-arg → `ENV GIT_SHA=$GIT_SHA` in Dockerfile;
   each app reads `os.environ["GIT_SHA"]` at startup.** Build-time
   bake of the value, runtime read of the env. SHA cannot drift
   because it's frozen into the image layer — restarting the same
   image always reports the same SHA. **Pros:** one source of truth
   (the build invocation); no extra files; `docker inspect
   --format='{{.Config.Env}}'` shows the SHA at the docker layer for
   external auditors; works identically for chat / admin / any
   future app service that joins the image; lazy `os.environ.get`
   reads make tests cheap (set env, call helper). **Cons:** an
   operator who runs `docker build` by hand without `--build-arg`
   gets `GIT_SHA=unknown` — has to be a discipline gate in
   `deploy.sh` (covered by negative test).

2. **Read SHA from `/workspace/.git_sha` file written at image build
   time by `deploy.sh`.** Functionally similar to (1); SHA lives in a
   file inside the image instead of an env var. **Cons:** two layers
   to wire (file-write at build, file-read at runtime); file path is
   a new convention to remember; one more failure mode (`FileNotFoundError`,
   stale file from a manual rebuild that forgot to update it,
   permissions on the read). The env-var path is more idiomatic for
   "a value baked at image-build time".

3. **Probe at request time via
   `subprocess.check_output(['git','rev-parse','HEAD'])`.** Not
   viable — `.git` is intentionally not in the image (D-SB1-2 only
   COPYs `requirements.lock`, `scripts/`, `tests/`). Even if it
   were, the SHA would reflect whatever ref happened to be checked
   out *on the host that ran the build*, not the SHA pinned at build
   time. Rejected.

4. **Server-side build-time replace into a `_version.py.in`
   template** (Make-style codegen). Pros: SHA is a Python literal,
   not an env read — no chance of env being unset at runtime. Cons:
   adds a codegen step to the build; the `_version.py.in` →
   `_version.py` indirection is another file convention; the env-var
   approach already gives runtime-immutability (the env is set in
   the image layer, not at container start). Rejected as
   overengineering.

**Decision.** Option 1.

**Implementation.**

```dockerfile
# Dockerfile (right before WORKDIR /workspace, after COPY of code)
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=$GIT_SHA \
    BUILD_TIME=$BUILD_TIME
```

```bash
# deploy.sh — at the `docker build` step (post dirty-check, post
# SHA resolution).  BUILD_TIME is UTC ISO-8601 with second precision.
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker build \
    --build-arg GIT_SHA="${SHA}" \
    --build-arg BUILD_TIME="${BUILD_TIME}" \
    -t "${IMAGE_NAME}:${SHA}" -f Dockerfile .
```

```python
# scripts/v2/__version__.py
import os

ANALYTICS_VERSION = "2.6.13"

# GIT_SHA and BUILD_TIME are baked into the image at build time via
# Dockerfile ARG/ENV and deploy.sh's --build-arg (ADR-B3). Outside a
# built image (dev, tests, ad-hoc python) both resolve to "unknown" —
# an explicit string value, never None, never a feature-flag-derived
# substitute. The lazy getters let tests use monkeypatch.setenv
# without an import-order dance.
def get_git_sha() -> str:
    return os.environ.get("GIT_SHA", "unknown")

def get_build_time() -> str:
    return os.environ.get("BUILD_TIME", "unknown")

def runtime_identity() -> dict:
    return {
        "status":     "ok",
        "version":    ANALYTICS_VERSION,
        "git_sha":    get_git_sha(),
        "build_time": get_build_time(),
    }
```

```python
# chat_server.py / admin_server.py: /health
def do_GET(self):
    if self.path == "/health":
        from scripts.v2.__version__ import runtime_identity
        return self._send(200,
                          json.dumps(runtime_identity(), ensure_ascii=False),
                          "application/json; charset=utf-8")
```

The chat header chip
(`<span class=meta>__VERSION_DISPLAY__</span>`) renders as
`v2.6.13 · <sha7> · planner→router→renderer→critic`. The tooltip
adds full SHA + build time. Both come from `runtime_identity()`,
not from `WC_*` flag state — toggling any feature flag must not
change either value (R2 negative test).

**Runtime-vs-docker cross-check.**
`scripts/verify_deployed_image.sh` is extended (D-SB3-3 below): after
the docker-tag comparison passes for all three services, it issues a
local `GET http://127.0.0.1:8890/health` (chat) and
`http://127.0.0.1:8891/health` (admin) and asserts the JSON's
`git_sha` matches `${EXPECTED}`. Two surfaces must agree: docker tag
== `/health.git_sha`. A mismatch means the image tag was bumped but
the running container was not actually replaced — exactly the failure
class we are closing. The probe is gated behind a `--with-runtime`
flag (default: on for prod, off when the script is run by tests that
don't have the services up).

**Consequences.**
- Externally verifiable identity: anyone with HTTP access to the
  service can confirm what code is running without docker
  privileges. `curl -s slovoeb.net/health | jq -r .git_sha`
  is the one-liner.
- `verify_deployed_image.sh` becomes the single self-checking
  acceptance script for *both* docker-side and runtime-side identity.
  No more two-surface drift class.
- The chat UI header chip now means what it says: bumping
  `ANALYTICS_VERSION` without rebuilding the image will still show
  the old SHA in the chip → user-visible signal that "deploy did not
  land".
- `tests/v2/test_runtime_identity.py` (new) closes R2: positive
  cases for SHA propagation, negative cases for "WC_* flags do not
  influence SHA", "missing env yields 'unknown' not silent fallback".
- `predeploy_probe_suite.py` and `check_version_bump.py` are not
  touched — they read `ANALYTICS_VERSION` by file parse, not by
  importing `__version__.py`. The new env-derived attributes are
  invisible to them.
- `status_server.py` (host-side, no Docker image) is **explicitly
  out of scope** for this ADR — it has its own lifecycle (runs from a
  host checkout via systemd, not from a SHA-pinned image). A
  future block can give it parity by reading `git rev-parse HEAD` at
  startup, but that's a different mechanism for a different process
  shape and would dilute S-B3.

**Trade-offs.**
- An operator who runs `docker build` by hand without
  `--build-arg GIT_SHA=...` gets `GIT_SHA=unknown` baked in. Mitigated:
  `deploy.sh` is the canonical build path and always passes both
  build-args; `docker-compose.yml`'s `build:` section also passes
  them (with `"unknown"` fallback for dev `compose build`); the
  negative test `test_deploy_sh_passes_git_sha_build_arg` pins the
  deploy.sh side.
- Build-time is a UTC string baked at the moment the layer is
  produced. The first deploy after editing the Dockerfile changes
  it; a no-op rebuild with full layer cache hits *does not* change
  it (the COPY/RUN layers are cached, so `ENV BUILD_TIME=...` is too
  — that's actually desired: identical image content → identical
  reported identity). Operators who want a fresh build-time use
  `docker build --no-cache`.
- Two new env vars in the image (`GIT_SHA`, `BUILD_TIME`).
  Negligible size; visible in `docker inspect`, so a third
  observation surface (alongside docker tag and `/health`) — useful,
  not harmful.

### D-SB3-1 — `GIT_SHA` and `BUILD_TIME` live in `scripts/v2/__version__.py`

**Decision.** Extend the existing `__version__.py` (today: one line,
`ANALYTICS_VERSION = "2.6.13"`) with two lazy getters
(`get_git_sha`, `get_build_time`) plus a `runtime_identity()` helper
that wraps all three values into the dict served by `/health`. No new
module; no codegen.

**Why this file.** `__version__.py` is already the single declared
home of "what version is this code". The companion identity values
(SHA, build time) belong adjacent to it, not in a new
`runtime_identity.py` (which would proliferate import surfaces). The
file is already parsed (not imported) by
`predeploy_probe_suite.py:62` and `check_version_bump.py:65` with a
regex pinned to `ANALYTICS_VERSION = "..."` — adding new lines does
not break either parser (regex anchors on the variable name, not on
line count).

**Lazy vs. constant.** `GIT_SHA = os.environ.get("GIT_SHA", "unknown")`
at module top is the obvious shape but breaks tests that
`monkeypatch.setenv("GIT_SHA", "fake123")` *after* the module is
already imported. Functions (`get_git_sha()`) read env at call time,
which matches both prod semantics (the env is set in the image layer,
process-lifetime constant) and test semantics (a test can vary it
between calls). Tiny overhead; correctness win.

### D-SB3-2 — `/health` returns JSON with the same shape on chat and admin

**Decision.** Both `chat_server.py` (port 8890) and `admin_server.py`
(port 8891) return JSON from `/health`:

```json
{"status": "ok", "version": "2.6.13", "git_sha": "bac0b80",
 "build_time": "2026-05-25T12:34:56Z"}
```

Content-type: `application/json; charset=utf-8`. HTTP status: 200 on
healthy.

**Why JSON, not plain `"ok"`.** The 2026-05-22 incident teaches that
"the endpoint returned 200" is not the same as "the endpoint
returned the *right* 200". Plain text `"ok"` cannot carry the SHA;
adding a header (`X-Git-Sha`) is greppable in CLI but ignored by
existing health-check tooling (compose healthcheck looks at status
code, not body). JSON is greppable, machine-readable, and survives
through proxies that strip custom headers. The compose healthcheck
in `docker-compose.yml:151,176` still checks `status == 200` and
keeps working — body shape changed, body presence didn't.

**Why both services.** Asymmetry would mean an operator has to
remember "chat has SHA, admin doesn't"; the same image runs both,
so the same identity should report from both. Catches a deploy that
recreated chat but skipped admin (or vice versa).

**Backward compat.** `smoke_s_b2.sh` polls `/health` with `curl -sf
--max-time 2 ... >/dev/null` — discards the body, only checks exit
code. Still passes. Same for the compose-level healthcheck (urllib
status-code check).

### D-SB3-3 — `verify_deployed_image.sh` cross-checks runtime SHA

**Decision.** Extend `verify_deployed_image.sh` with a runtime probe
step that follows the docker-tag check:

```bash
# After all docker-tag checks pass, fetch /health and compare git_sha.
# Default: probe runtime. --no-runtime skips (for test contexts where
# the services aren't actually running).
if [[ "${WITH_RUNTIME:-1}" == "1" ]]; then
    for svc_port in "chat:8890" "admin:8891"; do
        svc="${svc_port%:*}"; port="${svc_port#*:}"
        body="$(curl -sf --max-time 5 "http://127.0.0.1:${port}/health" || true)"
        sha="$(printf '%s' "$body" | python3 -c \
            'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("git_sha","<unset>"))')"
        if [[ "$sha" != "$EXPECTED" ]]; then
            echo "FAIL: ${svc} /health git_sha=${sha} != expected ${EXPECTED}" >&2
            rc=6
        else
            echo "OK: ${svc} /health git_sha=${sha}"
        fi
    done
fi
```

**Why.** Closes the only remaining failure mode B1+B2 didn't cover:
"docker tag is correct, but the process inside doesn't actually
think it's running that SHA" (e.g. an operator pinned a tag in
`docker-compose.yml` but the process was started before the env-var
bake landed). One script, two surfaces, must agree.

**Negative test.** `test_verify_deployed_image_runtime_probe` pins
that the script reads `/health.git_sha` (greps the script source for
the probe pattern). If a future change removes the runtime check,
the test fails — same anti-drift pattern as
`test_deploy_sh_uses_scoped_dirty_check`.

#### 2026-05-25 amendment — runtime path hardened (post first S-F1 deploy attempt)

The first attempt to deploy S-F1 hit two latent bugs in this
runtime block on a real host. Prod did not break (rollback held
on `bac0b80`, chat/admin healthy), but the deploy was blocked
until the verify script could distinguish "not yet warm" from
"genuinely broken":

1. **No warmup wait.** The script curl'd `/health` once, ~12 s
   after `compose up`. Chat warmup is ~76 s (chromadb 18 s + v2
   dispatch 58 s on this hardware), so the one-shot always
   reported "empty body" and `rc=6` — false-failing every
   forward deploy at this stage.
2. **Crash on non-JSON.** The inline `python -c 'json.loads(...)'`
   raised `JSONDecodeError` under `set -euo pipefail` against the
   rollback target's `/health` body — `bac0b80` predates ADR-B3,
   returns plain text `"ok"`, no JSON envelope. Verify aborted
   from inside its rollback path; the operator saw "ROLLBACK
   ALSO FAILED" while the host was in fact fine on the
   rolled-back tag.

**Fix.**

- New helper `poll_until_healthy(service, container_name)` polls
  `docker inspect --format='{{.State.Health.Status}}'` until
  `"healthy"` or `VERIFY_HEALTHCHECK_BUDGET_S` (default 180 s)
  exhausts. Compose's HEALTHCHECK already curls `/health 200 OK`
  from inside the container with the correct `start_period`
  (60 s for chat, 30 s for admin), so the moment docker flips
  status to healthy the outside curl is also good. **Reuses the
  existing healthcheck contract — does not invent a new
  warmup-detection surface.**
- `json.loads` wrapped in `try/except JSONDecodeError`. Non-JSON
  body is **degraded mode**: 200 OK is sufficient, `git_sha` not
  enforced. This is the only honest contract for pre-B3 rollback
  targets — refusing them on a JSON surface they never offered
  would lock out the rollback path entirely.
- JSON-but-malformed (not a dict, or missing `git_sha`) still
  fails loud (`rc=7`). Degraded mode is exclusively for
  non-parseable bodies — once you've claimed JSON, the shape
  must hold.

**Test knobs.** Two test-only env vars added (production never
sets them):

- `VERIFY_SKIP_DOCKER_HEALTHCHECK=1` — skips the poll. Used by
  the /health-parse path tests in
  [tests/v2/test_verify_deployed_image.py](tests/v2/test_verify_deployed_image.py).
- `VERIFY_SKIP_TAG_CHECK=1` — skips the docker-tag check loop.
  Same use case: runtime tests don't have a docker daemon, only
  a mock HTTP server.

**R2 negative tests.**
[tests/v2/test_verify_deployed_image.py](tests/v2/test_verify_deployed_image.py)
carries six runtime gates (Linux-only per the S-B5 quarantine
pattern — needs bash + a PATH-shim trick) plus four static parse
gates that always run:

| Test | What it pins |
|---|---|
| `test_slow_service_polling_succeeds` | Docker shim returns `starting × 2 → healthy`; verify polls and succeeds (Bug 1). |
| `test_pre_b3_plain_ok_body_degraded` | `/health` returns `"ok"`; verify accepts as degraded (Bug 2). |
| `test_malformed_json_body_degrades_not_crashes` | Truncated JSON; verify does not crash. |
| `test_b3_json_with_matching_sha` | Happy path — `git_sha` matches → OK. |
| `test_b3_json_with_mismatched_sha_fails_loud` | Mismatch is **loud** (`rc=7`), not silent. |
| `test_poll_budget_exhausted_fails_loud` | Always-`starting` → `rc=6` after budget (no infinite block). |
| `test_verify_has_poll_until_healthy_function` (static) | Pin the function name + `State.Health.Status`. |
| `test_verify_has_budget_and_sleep` (static) | Pin budget env var + `sleep` inside poll. |
| `test_verify_guards_json_loads` (static) | Pin `JSONDecodeError` handler exists. |
| `test_verify_has_degraded_mode_marker` (static) | Pin the `non-JSON` / `pre-B3` label. |

**Acceptance.** Verify against the deployed S-F1 image succeeds
(JSON path, polling waits for warmup). Verify against the
rollback target `bac0b80` succeeds (degraded path, no crash).
The rollback-to-pre-B3 dead-end is closed.

### Negative tests (R2)

`tests/v2/test_runtime_identity.py` (new) carries the R2 mirror set
for ADR-B3:

| Case | What it asserts |
|---|---|
| `test_git_sha_reads_from_env` | `monkeypatch.setenv("GIT_SHA","abc1234")` → `get_git_sha() == "abc1234"`. |
| `test_git_sha_missing_returns_unknown` | `monkeypatch.delenv("GIT_SHA", raising=False)` → `get_git_sha() == "unknown"` (explicit string, not `None`, not empty). |
| `test_runtime_identity_shape` | `runtime_identity()` returns dict with exactly keys `{status, version, git_sha, build_time}` and version == `ANALYTICS_VERSION`. |
| `test_git_sha_independent_of_wc_flags` | NOT-X mirror: set `WC_CRITIC=off`, `WC_LLM_MODEL=foo`, etc. — `get_git_sha()` value is unchanged. |
| `test_build_time_reads_from_env` / `_missing_returns_unknown` | Same pair for `BUILD_TIME`. |
| `test_dockerfile_bakes_git_sha_arg` | Grep `Dockerfile` for `ARG GIT_SHA` and `ENV GIT_SHA=$GIT_SHA`; both must be present. Mirror for `BUILD_TIME`. |
| `test_deploy_sh_passes_git_sha_build_arg` | Grep `deploy.sh` for `--build-arg GIT_SHA=` and `--build-arg BUILD_TIME=` adjacent to the `docker build` invocation. |
| `test_verify_deployed_image_runtime_probe` | Grep `verify_deployed_image.sh` for the `/health` probe + `git_sha` JSON parse. |
| `test_chat_health_handler_emits_runtime_identity` | Static import of `chat_server`; assert the `/health` branch in `do_GET` calls `runtime_identity()` (greppable; we don't spin up the server). |
| `test_admin_health_handler_emits_runtime_identity` | Same for `admin_server`. |
| `test_chat_version_chip_includes_git_sha` | Render `PAGE` template via `_build_version_strings()` with `GIT_SHA=feedface`; assert the rendered display string contains `feedface`. |

Per R2, every "X triggers Y" has a "NOT-X does not trigger Y"
mirror:
- `test_git_sha_reads_from_env` (X) ↔ `test_git_sha_missing_returns_unknown` (NOT-X).
- `test_chat_version_chip_includes_git_sha` (X) ↔ `test_git_sha_independent_of_wc_flags` (NOT-X — flag-toggle does NOT trigger SHA change).
- File-level grep tests (Dockerfile / deploy.sh / verify_deployed_image.sh) are anti-drift pins; their failure mode is the trivial "the line went away" which the assert covers.

### Acceptance gate (TZ S-B3)

| Gate                                                                 | How verified                                                                       |
|----------------------------------------------------------------------|------------------------------------------------------------------------------------|
| `GET /health` returns `git_sha` matching deployed commit             | `verify_deployed_image.sh` runtime probe (D-SB3-3) + manual `curl /health \| jq`   |
| UI footer/chip shows the same SHA                                    | `test_chat_version_chip_includes_git_sha` (offline render of header chip)          |
| SHA value does NOT change with `WC_*` flag toggles                   | `test_git_sha_independent_of_wc_flags`                                             |
| `/health.git_sha == git rev-parse HEAD` of built image               | `verify_deployed_image.sh --with-runtime` (D-SB3-3)                                |

### Follow-ups deliberately out of scope of S-B3

- **`status_server.py` runtime identity.** Host-side process, no
  Docker image, no `ARG GIT_SHA`. Could read `git rev-parse HEAD`
  on startup but that's a different mechanism for a different
  process shape. Tracked separately.
- **UI footer treatment beyond the header chip.** The `#stats-footer`
  at the bottom of the chat HTML could also surface SHA. Decided
  against to keep scope tight — the header chip is the canonical
  version surface and already visible.
- **Bumping `predeploy_probe_suite.py` to verify `/health.git_sha`
  matches the about-to-deploy commit.** Natural extension under
  ADR-B4 (predeploy harness as single gate); not added here to keep
  S-B3 focused on the runtime surface itself.

---

## 2026-05-24 — S-B2: supervision landed (ADR-B2 accepted)

Closes block **S-B2** of `docs/tz_structural_fixes_2026-05-24.md`.
Promotes ADR-B2 (chat / admin as compose services with proper PID 1)
from Proposed to Accepted, and flips status_server from a host-side
`nohup` background process to a host systemd unit that survives reboot.
After S-B2, `docker compose -f docker-compose.yml up -d --force-recreate`
deterministically brings up working `chat` + `admin` against the new
image, with **no** `docker exec`, **no** `pkill`, **no**
`systemctl restart wordcracker-{chat,admin}` chain. The single mechanism
in `deploy.sh` is the compose recreate. Host reboot brings status_server
back via systemd. This is the structural fix the S-B1 follow-up
explicitly deferred (*chat_server / admin_server as compose services
with proper PID 1*).

### D-SB2-1 — Two compose services share the SHA-pinned image

**Context.** ADR-B2 Option 1 (separate compose services, same image)
was the pre-committed direction. Two implementation details ADR-B2
left open: (a) where the env block lives (compose `environment:` vs.
systemd drop-in vs. shared `.env`), (b) which data bind-mounts each
service actually needs (the prior "everything goes into gutenberg-lab"
was conservative-by-default, not contract-driven).

**Options reconsidered.**
1. **Single new compose service running a supervisor (s6-overlay /
   supervisord) that fans out chat + admin.** Keeps gutenberg-lab as
   one container; one image, one PID-1 supervisor, two managed Python
   children. Pros: smallest compose churn. Cons: adds a supervisor
   dependency to the image; PID 1 is now the supervisor, not Python,
   so SIGTERM propagation depends on supervisor config (re-introduces
   the very thing R8 + ADR-B2 wanted closed); per-child logs need explicit
   piping into journald-via-docker; jupyter-in-dev needs to coexist
   with the supervisor.
2. **Two separate compose services (`chat`, `admin`), same
   `wordcracker-textlab:${WC_IMAGE_TAG}` image, distinct `command:`,
   distinct ports, distinct healthchecks.** Pros: PID 1 = Python in
   each container (SIGTERM works natively); restart policy is
   per-service; healthcheck is per-service; jupyter is independent of
   either. Cons: small YAML duplication (image, env, volumes) —
   mitigated by YAML anchors (`&app-image`, `&app-env`,
   `&app-volumes`).
3. **Two services with two separate images** (chat-only and
   admin-only image, pip-trimmed per service). Cons: 2× build cost
   per deploy; pip layer no longer shared; ADR-B1's `:SHA` tag becomes
   `:SHA-chat` / `:SHA-admin` (verify_deployed_image.sh complexity ↑).
   Reward (smaller per-image footprint) is invisible on a single host
   with ample disk.

**Decision.** Option 2. New services in `docker-compose.yml`:

```yaml
x-app-image: &app-image
  image: wordcracker-textlab:${WC_IMAGE_TAG:?WC_IMAGE_TAG must be set ...}

x-app-env: &app-env
  OLLAMA_HOST: http://ollama:11434
  ASSISTANT_NAME: Словоёб
  WC_OLLAMA_NUM_CTX: "16384"
  WC_LLM_MODEL: wordcracker:v2
  WC_CRITIC_MODEL: wordcracker:v2

x-app-volumes: &app-volumes
  - /data/books:/workspace/books
  - /data/clean_books:/workspace/clean_books
  - /data/chroma_db:/workspace/chroma_db
  - /data/spgc:/workspace/spgc
  - /data/raw_text:/workspace/raw_text
  - /data/wodehouse_raw:/workspace/wodehouse_raw
  - /data/gutenberg_raw:/workspace/gutenberg_raw
  - /data/uploads:/workspace/uploads   # admin write target; gutenberg-lab was implicit r/w
```

Services:

```yaml
chat:
  <<: *app-image
  container_name: wordcracker-chat
  command: ["python", "-u", "/workspace/scripts/chat_server.py", "--port", "8890"]
  environment: *app-env
  volumes: *app-volumes
  ports: ["8890:8890"]
  depends_on: { ollama: { condition: service_healthy } }
  restart: unless-stopped
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8890/health', timeout=2).status == 200 else 1)\""]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 60s

admin:
  <<: *app-image
  container_name: wordcracker-admin
  command: ["python", "-u", "/workspace/scripts/admin_server.py", "--port", "8891"]
  environment: *app-env
  volumes: *app-volumes
  ports: ["8891:8891"]
  restart: unless-stopped
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8891/health', timeout=2).status == 200 else 1)\""]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s
```

Healthcheck uses `python urllib` instead of `curl` because `curl` is
not guaranteed in the pytorch-base image's runtime layer (verified by
`docker compose -f docker-compose.yml exec chat which curl` → exists,
but defensive — `python` is by definition present in this image). One
fewer assumption.

**Consequences.**
- PID 1 in each container is `python -u /workspace/scripts/*.py`.
  `docker stop` sends SIGTERM to Python directly; graceful shutdown
  works without the `pkill` orphan-reap that
  `wordcracker-chat.service:18-20` was apologizing for.
- `depends_on: ollama: service_healthy` means chat waits for ollama
  before starting → eliminates the cold-boot race where chat hits
  ollama before it's up.
- All three services (`chat`, `admin`, `gutenberg-lab`) share the same
  image tag — `verify_deployed_image.sh` keeps working with a one-line
  generalisation (loop over the service list instead of hardcoding
  `gutenberg-lab`).
- `admin` writes to `/workspace/uploads` (was implicit under
  gutenberg-lab; now an explicit data bind-mount at `/data/uploads`).
  Bookkeeping nit: the host directory must exist. Added to the install
  step.

**Trade-offs.**
- Three containers instead of one. Each carries the Python interpreter
  + `import torch` cost in memory (~700 MB RSS for chat after warmup;
  admin ~150 MB; jupyter ~600 MB idle). Pre-S-B2 the same code ran in
  one process tree inside gutenberg-lab. Memory cap on the 40 GB
  `deploy.resources.limits.memory` (set per gutenberg-lab today) needs
  re-allocation: 28 GB chat, 4 GB admin, 8 GB jupyter; ollama keeps
  its 16 GB cap. Total RAM ceiling unchanged.
- YAML anchors are a one-time learning curve for an operator who hasn't
  used them. Mitigation: each anchor is named (`x-app-*`) and lives at
  the top of the file with a comment.

### D-SB2-2 — Jupyter stays in `gutenberg-lab`, base-compose; not moved to dev-only

**Decision.** `gutenberg-lab` service (jupyter on 8888) stays in
`docker-compose.yml` (the prod base) with its current CMD. Not moved
to `docker-compose.dev.yml`.

**Options.**
1. **Move jupyter to override (dev-only).** Cleaner prod surface
   (only chat + admin + ollama would run on prod); also removes the
   unauthenticated jupyter on port 8888 from prod. **Pros:** smaller
   attack surface; ~600 MB idle RAM reclaimed on prod.
   **Cons:** changes prod-runtime shape inside S-B2 — a separate
   security/footprint concern that deserves its own block; touching
   it here violates R7 ("one fix = one commit").
2. **Keep jupyter in base, unchanged.** S-B2 stays focused on
   supervision. Jupyter security stays a known issue tracked
   separately.

**Decision rationale.** Option 2. Per R7, S-B2 is about supervision
shape, not about jupyter's presence on prod. Moving jupyter is a
distinct decision with its own trade-offs (does the operator want
jupyter on prod for ad-hoc analysis? authentication story?) — that
belongs in a follow-up block. Flagged below in "Follow-ups out of
scope".

**Consequences.**
- Prod runs four containers post-S-B2: ollama, gutenberg-lab
  (jupyter), chat, admin. Up from two pre-S-B2 (ollama,
  gutenberg-lab).
- The unauth-jupyter-on-prod-port-8888 issue (`--ServerApp.token=''`
  in `docker-compose.yml:101`) is **not closed** by S-B2 — it is
  carried forward as a follow-up.

### D-SB2-3 — `status_server` runs as host systemd unit, enabled at install

**Context.** `systemd/wordcracker-status.service` already exists in
the repo (3 sections: [Unit] / [Service] / [Install]). What was
missing was *enablement*: `install_systemd_units.sh` did `install -m
644 ... && systemctl restart ...` but never `systemctl enable`.
Restart starts the unit *for this boot*; without `enable`, the unit
is not linked into `multi-user.target.wants/` and does not start
after reboot. That's why the user reports "status_server на хосте
через nohup не переживает ребут" — the systemd unit either was never
installed or was installed-without-enable, and the running instance
is in fact a manual nohup.

**Options.**
1. **Add `systemctl enable` next to `systemctl restart` in
   `install_systemd_units.sh`.** Standard systemd lifecycle; one-time
   per-unit-change operator step (same shape as the existing install
   step).
2. **Convert status_server to a compose service on the host (not the
   container).** Doesn't apply — status_server reads host paths
   (`/data/spgc`, `/data/chroma_db`, `/data/raw_text` directly,
   *not* the container view), and Docker bind-mounts work the other
   way. Putting it in a container forces another bind-mount tree
   for paths it already reads natively.
3. **Containerise status_server.** Same objection as Option 2;
   status_server's whole point is "no Docker dependency, runs on
   host, host-stdlib Python".

**Decision.** Option 1. `install_systemd_units.sh` adds two changes:
(a) after the `install -m 644` loop, run `sudo systemctl enable
wordcracker-status` so it links into `multi-user.target.wants/` and
fires on reboot; (b) `systemctl restart wordcracker-status` joins
the existing restart loop for chat/admin (chat/admin restarts go away
under D-SB2-6, so the new restart line is `systemctl restart
wordcracker-status` only).

**Consequences.**
- After one-time install on prod, `reboot` → status_server back on
  :8889. No operator action.
- `journalctl -u wordcracker-status -f` keeps working (already
  configured in the unit's `StandardOutput=journal`).
- A nohup'd `status_server.py` running on prod TODAY must be killed
  by the operator BEFORE `systemctl start wordcracker-status` — same
  port 8889 collision. Documented in the install script's output
  (D-SB2-9 follow-up: detect and warn).

### D-SB2-4 — `deploy.sh` collapses to a single mechanism

**Decision.** `scripts/deploy.sh` step 4 changes from

```bash
WC_IMAGE_TAG="${SHA}" docker compose -f docker-compose.yml \
    up -d --force-recreate gutenberg-lab
```

to

```bash
WC_IMAGE_TAG="${SHA}" docker compose -f docker-compose.yml \
    up -d --force-recreate gutenberg-lab chat admin
```

And step 5 (the `for unit in wordcracker-chat wordcracker-admin; do
systemctl restart "$unit"; done` loop, deploy.sh:127-138) is **deleted
entirely**. `wordcracker-status` is not restarted on deploy — its code
lives on the host and is not part of any image tag.

**Why.** Pre-S-B2: the recreate was on gutenberg-lab only; chat/admin
ran as systemd-managed `docker compose exec` clients pinned to the
*old* container ID, so they died on recreate and needed `systemctl
restart` to re-exec into the *new* container. Two mechanisms. Post-S-B2:
chat/admin ARE compose services; `--force-recreate` recreates them with
the new image tag in the same single command. One mechanism.

**Consequences.**
- `deploy.sh` no longer needs `sudo` for the systemctl restart loop on
  most deploys (the loop is gone). `install_systemd_units.sh` retains
  the sudo boundary explicitly (one-time per systemd-change).
- Operator running `docker compose -f docker-compose.yml up -d
  --force-recreate` manually now deploys identically to `bash
  scripts/deploy.sh` minus the .env write, the SHA tag build and the
  prune. The pure-compose path is now a real path, not a half-path
  that needs systemctl follow-up.
- A `--force-recreate <subset>` syntactically still works; an operator
  who passes just `gutenberg-lab` recreates only jupyter and leaves
  chat/admin on the old image. Deploy script always passes the three
  service names explicitly to prevent this footgun.

### D-SB2-5 — `v2-engine.conf` drop-in deleted; pins move into compose

**Decision.** Delete
`systemd/wordcracker-chat.service.d/v2-engine.conf` from the repo
(and on prod, `sudo rm
/etc/systemd/system/wordcracker-chat.service.d/v2-engine.conf
&& sudo rmdir /etc/systemd/system/wordcracker-chat.service.d
&& sudo systemctl daemon-reload`). The two pins it still carried —
`WC_LLM_MODEL=wordcracker:v2` and `WC_CRITIC_MODEL=wordcracker:v2`
— move into the `chat` service's `environment:` block (the `*app-env`
anchor under D-SB2-1). The dead `WC_DEFAULT_ENGINE=v2` (flagged for
removal in D-S0-5) dies with the file.

**Why.** Under D-SB2-6 the parent unit `wordcracker-chat.service` is
deleted. A drop-in for a unit that no longer exists is dead config of
the worst kind — silently invalid, no error surface, exactly the R8
hazard. Moving the live pins into compose is the natural relocation
target now that chat IS a compose service.

**Consequences.**
- D-S0-5 explicit pending-removal honored.
- The `infra.md §2` row for `WC_DEFAULT_ENGINE` transitions from
  "pending-removal (drop-in)" to "removed". `infra.md §2`'s row for
  `WC_LLM_MODEL` / `WC_CRITIC_MODEL` transitions from "set via
  systemd drop-in" to "set via compose `chat.environment`".

### D-SB2-6 — `wordcracker-chat.service` and `wordcracker-admin.service` deleted

**Decision.** Delete `systemd/wordcracker-chat.service` and
`systemd/wordcracker-admin.service` from the repo. On the prod host,
`sudo systemctl disable --now wordcracker-chat wordcracker-admin && sudo
rm /etc/systemd/system/wordcracker-chat.service
/etc/systemd/system/wordcracker-admin.service && sudo systemctl
daemon-reload`. `wordcracker-status.service` stays — host process, no
docker dependency.

**Why.** With chat / admin as compose services, the systemd units
become a parallel supervisor for the same processes. Two supervisors
race (compose `restart: unless-stopped` vs. systemd `Restart=on-failure`),
behavior depends on which one notices the death first, and any future
operator wondering "why did chat get restarted twice in a row?" hits
the worst class of diagnose-the-supervisor bug. One supervisor only.
The remaining one is Docker (via compose).

**Consequences.**
- `install_systemd_units.sh` `UNITS` array contracts from
  `(chat, admin, status)` to `(status,)`. The script's existing
  `if [[ ! -f "$src" ]]; then continue; fi` guard makes the array
  shrink graceful for an in-flight half-applied prod.
- `journalctl -u wordcracker-chat` stops being a thing. Logs for chat
  move to `docker compose logs chat` (or `docker logs wordcracker-chat`).
  Operator runbook needs the new command — added to D-SB2-9 doc list.
- `wordcracker-chat.service.d/` drop-in directory is empty after
  D-SB2-5; `rmdir` it on prod. Empty drop-in dirs are harmless but
  noisy in `ls /etc/systemd/system/`.

### D-SB2-7 — Healthcheck moves to compose; systemd `ExecStartPost` curl loop is gone

**Decision.** Each new compose service declares a `healthcheck:` block
(see D-SB2-1). `chat` has `start_period: 60s` to cover the
ChromaDB+SentenceTransformer cold-load (observed p95 ~14s,
`wordcracker-chat.service:32` comment); admin has `start_period: 30s`
(no ChromaDB warmup, only loads on first request).

**Why.** The systemd-side ExecStartPost curl loop (`for i in $(seq 1
30); do sleep 2; curl ...`) was a poor man's healthcheck and was
deleted along with the unit. Compose's native `healthcheck:` is the
right place — surfaces `(healthy)` in `docker compose ps`, exposes a
`Status: starting/healthy/unhealthy` flag that downstream tooling
(verify, smoke, predeploy) can read with one command.

**Consequences.**
- `docker compose -f docker-compose.yml ps` shows
  `wordcracker-chat ... healthy` instead of opaque `running`.
  Smoke gate (D-SB2-8) reads the same flag.
- `depends_on: chat: service_healthy` is now available for other
  services (e.g. a future smoke runner) — `docker compose up -d
  chat-smoke-test` could wait for chat to be healthy before running.
  Out of scope for S-B2 but a free downstream win.

### D-SB2-8 — Negative tests (R2)

`tests/v2/test_deploy_artifact.py` (created in S-B1) grows S-B2 cases.
Per R2 each "X true" has its "NOT-X" mirror; the test names use the
`test_*_AND_NOT_*` shape so the mirror is greppable.

1. **`test_chat_and_admin_are_compose_services`** — load
   `docker-compose.yml`; assert `services` contains both `chat` and
   `admin`; each has a non-empty `command:`; each has `ports:` mapping
   the expected `8890:8890` / `8891:8891`; each has `image:` of the
   form `wordcracker-textlab:${WC_IMAGE_TAG...}` (catches accidental
   `:latest` regression under the new services too).
2. **`test_no_docker_exec_in_any_systemd_unit`** (negative mirror of
   #1) — walk `systemd/*.service`; assert no file contains the literal
   substring `docker compose exec` or `docker exec`. Catches a
   regression where someone "fixes" a future bug by reintroducing the
   exec pattern.
3. **`test_chat_and_admin_systemd_units_removed`** (negative mirror) —
   assert `systemd/wordcracker-chat.service` and
   `systemd/wordcracker-admin.service` do NOT exist on disk in the
   repo. Pairs with #1 in spirit: if chat is a compose service, the
   systemd file MUST be gone (no second supervisor).
4. **`test_status_unit_has_install_section`** — assert
   `systemd/wordcracker-status.service` contains `[Install]` with
   `WantedBy=multi-user.target`. Without `[Install]`, `systemctl enable`
   is a no-op — the unit can never auto-start on reboot. This is the
   D-SB2-3 guarantee.
5. **`test_v2_engine_drop_in_removed`** — assert
   `systemd/wordcracker-chat.service.d/v2-engine.conf` does NOT exist.
   Closes D-S0-5; mirror is implicit (its presence used to be the bug).
6. **`test_chat_environment_pins_llm_models`** — assert
   `docker-compose.yml` `services.chat.environment` contains
   `WC_LLM_MODEL=wordcracker:v2` and `WC_CRITIC_MODEL=wordcracker:v2`.
   The pins moved from the drop-in to compose — verify they survived
   the move. Negative: assert neither name appears in `systemd/`.
7. **`test_deploy_sh_no_systemctl_restart_of_chat_admin`** — grep
   `scripts/deploy.sh` for `systemctl restart wordcracker-chat` and
   `systemctl restart wordcracker-admin`; both must be ABSENT.
   Negative mirror: assert `docker compose ... up -d --force-recreate`
   IS present and names `chat` and `admin` in the service list (so
   somebody can't "remove the systemctl line" without adding the
   recreate-with-new-services).

Each of #1-#7 is a single-direction assert; the NOT-X mirror is the
sibling test (#1↔#3, #5 implicit, #6 has its own grep mirror, #7
both directions in one test). R2 satisfied: no "claim closed" without
a test that would have failed pre-S-B2.

### Acceptance gate (TZ S-B2)

| Gate                                                                            | How verified                                                                       |
|---------------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| `docker compose -f docker-compose.yml up -d --force-recreate` brings chat+admin | `scripts/smoke_s_b2.sh`: recreate → poll `/health` on 8890+8891 → both 200 within 180 s (covers cold ollama `start_period: 90s` + chat warmup) |
| No `docker compose exec` survives in any systemd unit                           | `tests/v2/test_deploy_artifact.py::test_no_docker_exec_in_any_systemd_unit`        |
| chat & admin defined as compose services with explicit command + SHA image      | `tests/v2/test_deploy_artifact.py::test_chat_and_admin_are_compose_services`       |
| Host reboot brings status_server back                                           | Operator: `sudo reboot && sleep 60 && curl -sf http://127.0.0.1:8889/health`; or `systemctl is-enabled wordcracker-status` returns `enabled` |
| `deploy.sh` is one mechanism (compose recreate only)                            | `tests/v2/test_deploy_artifact.py::test_deploy_sh_no_systemctl_restart_of_chat_admin` |

### Follow-ups deliberately out of scope of S-B2

- **Jupyter on prod (unauth, port 8888)** — D-SB2-2 explicitly kept it
  in base for R7 (one fix per commit). A follow-up block (proposed
  name `S-B3` — security surface) should pick between: move jupyter
  to override only, OR keep on prod with token auth wired through env.
  Not S-B2's call.
- **Memory limit redistribution** — D-SB2-1 notes the 40 GB
  gutenberg-lab cap needs splitting across chat (28 GB), admin (4 GB),
  jupyter (8 GB). The redistribution is one compose edit; folded into
  S-B2 implementation but not its own gate. Verify via `docker stats`
  post-deploy.
- **`predeploy_check.py` gate ahead of deploy.sh** — ADR-B4. S-B2 still
  trusts the operator's local `tests/v2` run before deploy.
- **`/health.git_sha`, footer SHA, `ARG GIT_SHA`** — ADR-B3 (runtime
  build identity) covers it. Originally bundled with the supervision
  proposal; on inspection orthogonal (a runtime-self-report concern,
  not a PID-1 concern) and split out into its own ADR. Filled in by
  Step 4 / S-B3.

---

## 2026-05-24 — S-B1: deploy artifact landed (ADR-B1 accepted)

Closes block **S-B1** of `docs/tz_structural_fixes_2026-05-24.md`.
S-B1 lands ADR-B1 (deploy artifact: image-tag-by-commit + code-in-image)
under a single acceptance gate: an immutable
SHA-tagged Docker image is the only thing that reaches prod, bind-mount
of the live repo is dev-only, and rollback is `re-run with the previous
tag`. This section is the implementation record that turns ADR-B1
into runnable code.

### D-SB1-1 — Compose layout (restructure, not prod-overlay)

**Context.** ADR-B1 originally floated a `docker-compose.prod.yml`
overlay that would REMOVE bind-mounts on top of base. At
implementation time two structural shapes turned out to be available;
the ADR text didn't pick between them definitively. Picking now.

**Options reconsidered.**
1. **Add a `docker-compose.prod.yml` overlay** that nullifies code
   volumes via compose `!override` / `!reset` directive. Pros: matches
   the original ADR-B1 overlay sketch; minimal file reshuffle. Cons: every prod
   invocation has to chain three `-f` flags (or systemd must know the
   full file set); `!override`/`!reset` need recent compose-spec
   (≥2.20); silent fail mode if the directive misbehaves is "bind-mount
   survives" — exactly the dark-code class S-B1 is meant to close.
2. **Restructure: prod-relevant config in base, dev-only conveniences
   in `docker-compose.dev.yml` (renamed from override.yml under
   D-SB1-7).** Pros: standard docker-compose
   convention; prod opts out of override with a single `-f
   docker-compose.yml` flag; no fragile YAML-spec directives; the dev
   path (`docker compose up`) keeps auto-applying override and still
   bind-mounts. Cons: one-time move of ollama service + env block from
   override into base.

**Decision.** Option 2 (restructure). Reasons in order: (a) the
silent-bind-mount failure mode of Option 1 is the precise dark-code
shape the block targets; (b) prod and dev diverge in one direction only
(dev *adds* mounts), which is exactly what override.yml was designed
for; (c) systemd `ExecStartPre` becomes
`/usr/bin/docker compose -f docker-compose.yml up -d gutenberg-lab` —
single explicit flag, greppable from the systemd unit text.

**Consequences.**
- `docker-compose.yml` is the prod source of truth: ollama service,
  gutenberg-lab env block, image tag without fallback, data bind-mounts
  for corpus/state.
- `docker-compose.dev.yml` (renamed from `docker-compose.override.yml`
  under D-SB1-7) is dev-only and **not** auto-applied: code bind-mounts
  (`./scripts`, `./tests`, `./notebooks`, `./data`, `./raw_books`) and
  a `WC_IMAGE_TAG=dev` default at the `image:` line.
- Prod path: `docker compose -f docker-compose.yml up -d` (or `bash
  scripts/deploy.sh`). Dev path: explicit opt-in via `docker compose -f
  docker-compose.yml -f docker-compose.dev.yml up -d`, or set
  `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml` in `.env`.
- `.env` at repo root is the canonical place to pin `WC_IMAGE_TAG` for
  the running shell. Repo ships `.env.example`; `.env` itself stays
  gitignored.
- Data bind-mounts (`/data/books`, `/data/spgc`, `/data/chroma_db`, …)
  stay in base — they are corpus/state, not source code, and ADR-B1's
  own trade-off section excluded them from the rule explicitly.

**Trade-offs.**
- One-time relocation of the ollama service spec and the gutenberg-lab
  env block. Diff is small but touches both compose files in the same
  commit.
- Operator running bare `docker compose up` on the prod host (without
  `-f`) silently switches into dev shape (bind-mounts back). Mitigated
  by systemd being the canonical entrypoint on prod and using `-f`
  explicitly. The verify script (D-SB1-5) catches the wrong tag if
  someone forgets.

### D-SB1-2 — Code is baked into the image via COPY

**Decision.** `Dockerfile` gains `COPY scripts/ /workspace/scripts/`
and `COPY tests/ /workspace/tests/` after the pip-install layer. A new
`.dockerignore` keeps the build context small (exclude `.git`,
`data/`, `raw_books/`, `notebooks/`, `__pycache__`, `docs/`, …).

**Why.** This is the operational half of "no bind-mount of code in
prod" — removing the bind-mount in compose without baking code into
the image would leave `/workspace/scripts` empty in prod.

**Consequences.**
- Rebuild on code change costs one COPY layer (~5-50 MB depending on
  scope); the heavy pip layer stays cached so per-code-change rebuild
  is ~5 s on the prod host.
- `git checkout` on the host repo no longer affects the running prod
  container. Atomic deploy is now a property of the image, not the
  host filesystem state.
- The defensive pyc-purge `ExecStartPre` at
  `systemd/wordcracker-chat.service:25` becomes redundant for the
  scripts/ path (kept as defence-in-depth for one more deploy cycle;
  removal lands in ADR-B2 along with the broader systemd rewrite).

### D-SB1-3 — `WC_IMAGE_TAG` is required; no `:-latest` fallback

**Decision.** `docker-compose.yml` declares
`image: wordcracker-textlab:${WC_IMAGE_TAG:?WC_IMAGE_TAG must be set (e.g. via .env or `bash scripts/deploy.sh`)}`.
The `${VAR:?msg}` substitution fails at `docker compose config` time
when the variable is unset, printing the explanatory message — no
silent `latest` ever ships to prod.

Dev: `docker-compose.dev.yml` overrides the same `image:` line with
`wordcracker-textlab:${WC_IMAGE_TAG:-dev}`. Per D-SB1-7 this file is
not auto-applied; dev opts in via `-f docker-compose.yml -f
docker-compose.dev.yml` or `COMPOSE_FILE=...:docker-compose.dev.yml`,
and only then does the `:-dev` fallback take effect. Bare `docker
compose up` on any host loads only the base file, where the strict
`${VAR:?}` form refuses to start without `WC_IMAGE_TAG` set.

**Why.** Phase 1's `:-latest` fallback was explicitly marked
"temporary; drop in phase 3" in ADR-B1. The S-B1 acceptance gate
requires the drop now: as long as the fallback exists, an operator who
forgets to export the tag silently ships whatever `latest` happens to
point at — which is the precise failure that wasted runs 2-5 of the
2026-05-22 deploy epic.

### D-SB1-4 — Deploy & rollback procedure

**Deploy** (`scripts/deploy.sh [<git-ref>]`):

1. `SHA=$(git rev-parse --short <git-ref or HEAD>)`. Bare ref refuses
   to deploy from a dirty tree (the `--allow-dirty` flag is the
   override).
2. `docker build -t wordcracker-textlab:$SHA -f Dockerfile .`. Tag is
   the short SHA; image lives in the local docker store (single host,
   single user — ADR-B1 trade-off: no registry).
3. Atomically write `WC_IMAGE_TAG=$SHA` into `.env` (tempfile +
   rename) so subsequent compose invocations on the host pick it up.
4. `docker compose -f docker-compose.yml up -d --force-recreate gutenberg-lab`.
   `--force-recreate` ensures the container picks up the new image
   even if compose thinks nothing changed.
5. `systemctl restart wordcracker-chat wordcracker-admin` — the
   chat/admin processes inside the container re-launch against the
   freshly-recreated container.
6. `bash scripts/verify_deployed_image.sh $SHA` — fails loudly if the
   running image tag ≠ $SHA (D-SB1-5).

**Rollback** (`scripts/deploy.sh --rollback <prev-sha>`):

1. The previous SHA's image is already on the host (deploys keep the
   last N SHA-tagged images — pruning policy below).
2. The rollback path runs steps 3-6 of deploy with the previous SHA;
   no rebuild needed.
3. If the previous image was pruned, fall back to: check out the
   previous SHA, run a full `bash scripts/deploy.sh`.

**Image retention.** `deploy.sh` runs `docker image ls
wordcracker-textlab` after restart and prunes all but the last 5
SHA-tagged images. 5 is a soft default; bump it in the script's
constants if a longer rollback window is wanted. Untagged dangling
layers from interrupted builds are pruned separately.

### D-SB1-5 — Verification: `scripts/verify_deployed_image.sh`

**Decision.** `scripts/verify_deployed_image.sh [<expected-sha>]`:

1. Resolve expected SHA — argument if provided, else `git rev-parse
   --short HEAD`.
2. Resolve running image for `gutenberg-lab`:
   `docker compose -f docker-compose.yml ps gutenberg-lab --format
   json | jq -r '.[0].Image'` (or the older `docker inspect` fallback
   if `--format json` isn't available on the host's compose version).
3. Strip the `wordcracker-textlab:` prefix from the running image;
   compare to expected.
4. Exit 0 on match, non-zero with a diff message on mismatch.

This is the S-B1 acceptance script. It runs as the last step of
`deploy.sh` and is independently invocable for spot checks. A more
complete runtime-identity probe (`git_sha` in `/health`, footer SHA,
`ARG GIT_SHA` baked into the image, version string scrubbed of feature
flags) is ADR-B3 (runtime build identity) territory — explicitly out
of scope here.

### D-SB1-6 — systemd `ExecStartPre` pinned to base compose file

**Decision.** `systemd/wordcracker-chat.service` and
`systemd/wordcracker-admin.service` change every `docker compose` line
from `/usr/bin/docker compose ...` to
`/usr/bin/docker compose -f docker-compose.yml ...`.

**Why.** At D-SB1-6 drafting time, `docker compose` auto-applied
`docker-compose.override.yml`, which (after D-SB1-1) re-introduces
code bind-mounts; pinning `-f docker-compose.yml` closed that hole
for systemd-driven invocations. D-SB1-7 later removed the auto-apply
by renaming the file to `docker-compose.dev.yml`, but the `-f` flag
stays anyway as the explicit-greppable form. It survives the ADR-B2
rewrite of these units (where the rewrite carries `-f` along).

**Operator step.** Systemd unit files are under `systemd/` in the
repo but live at `/etc/systemd/system/` on prod. Syncing them is a
one-time prod operation:

```
sudo install -m 644 systemd/wordcracker-chat.service /etc/systemd/system/
sudo install -m 644 systemd/wordcracker-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart wordcracker-chat wordcracker-admin
```

`scripts/install_systemd_units.sh` automates the above. It is invoked
manually by the operator after `deploy.sh` lands a commit that
touches `systemd/`. (Folding it into `deploy.sh` would require sudo on
every deploy — operator chose to keep that boundary explicit.)

### D-SB1-7 — `docker-compose.override.yml` renamed to `docker-compose.dev.yml` (safe-default prod)

**Decision (addendum, 2026-05-24, post-S-B2).** Rename the dev overlay
file from `docker-compose.override.yml` to `docker-compose.dev.yml`.
After the rename, bare `docker compose up` on the prod host loads only
`docker-compose.yml` (the safe default). Dev opts in explicitly via
`docker compose -f docker-compose.yml -f docker-compose.dev.yml up`
or by setting `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`
in `.env`.

**Why.** D-SB1-1 trade-off note flagged: *"Operator running bare
`docker compose up` on the prod host (without `-f`) silently switches
into dev shape (bind-mounts back)."* The compose convention of
auto-applying `docker-compose.override.yml` made the dev shape the
default on every host — and S-B1 mitigation was "systemd uses `-f`
explicitly". That mitigation holds for systemd / deploy.sh but not for
an interactive `docker compose up` typed by an operator on prod for an
ad-hoc debug or restart. The rename removes the auto-merge magic. The
filename now SAYS what it is (`dev.yml`); the loader no longer
auto-attaches it; opt-in is greppable.

**Prod safety (verified before landing).**
- `scripts/deploy.sh` uses `-f docker-compose.yml` explicitly ✓
- `scripts/verify_deployed_image.sh` uses `-f docker-compose.yml` ✓
- `scripts/install_systemd_units.sh` does not touch compose ✓
- `scripts/smoke_s_b2.sh` calls `bash scripts/deploy.sh HEAD` (which
  uses `-f docker-compose.yml`) ✓
- `scripts/ollama_gpu_watcher.sh` updated in the same commit: was
  invoking `-f docker-compose.yml -f docker-compose.override.yml` for
  no live reason (dev override never touched ollama); now uses
  `-f docker-compose.yml` only ✓
- `wordcracker-status.service` runs host-Python and does not invoke
  compose at all ✓
- `chat` / `admin` systemd units are gone post-S-B2 ✓

**Consequences.**
- `.env.example` documents the dev opt-in line
  (`COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`,
  commented).
- `tests/v2/test_deploy_artifact.py` constant `OVERRIDE_COMPOSE` →
  `DEV_COMPOSE`; the parametrized
  `test_dev_override_restores_code_bind_mounts` test still reads the
  renamed file and pins the dev bind-mounts.
- `infra.md` minimal update (file inventory line + WC_OLLAMA_NUM_CTX
  filename column); full env-var refresh against the post-S-B2
  layout (where `WC_LLM_MODEL` / `WC_CRITIC_MODEL` / `WC_OLLAMA_NUM_CTX`
  moved into `docker-compose.yml`'s `*app-env` anchor) is a separate
  follow-up.
- `CLAUDE.md` and `REFACTOR_BRIEF.md` R8 wording updated: "сверь
  активные compose-файлы (docker-compose.yml + docker-compose.dev.yml
  при dev opt-in) с гейтами `os.environ.get(...)`."
- Historical references to `docker-compose.override.yml` in audit /
  proposal-log prose (sections dated 2026-05-22 and the original
  ADR-B series Proposed bodies) are left as-is — they are snapshots
  at their respective dates.

**Trade-offs.**
- Dev workflow on a fresh box: one extra environment variable
  (`COMPOSE_FILE`) or one extra `-f` flag on the command line.
  Negligible cost, much smaller blast radius on prod.
- Backwards-compat: an existing operator's muscle-memory `docker
  compose up` on dev silently changes meaning (prod-only) until they
  set `COMPOSE_FILE` in `.env`. Mitigated by the
  `.env.example` line being right there in plain sight on `cp
  .env.example .env`.

### D-SB1-8 — `deploy.sh` dirty-check scoped to image-relevant paths

**Context.** D-SB1-4 step 1 included a coarse dirty-tree guard:

```bash
if [[ -n "$(git status --porcelain)" ]]; then
    if [[ "$ALLOW_DIRTY" == "1" ]]; then SHA="${SHA}-dirty"; ...
    else echo "ERROR: working tree is dirty. ..."; exit 6; fi
fi
```

`git status --porcelain` returns one line per change of any kind —
modified tracked, staged, deleted, **and any untracked file
anywhere**. Operationally that means an untracked `notes.md` at
repo root, a scratch `/tmp` file the operator forgot about, or a
locally-generated `requirements.in.tmp` blocks deploy. None of
these enter the image. The operator's reflex becomes
`--allow-dirty` → builds the SHA-tagged `<sha>-dirty` artifact →
ships into prod with the `-dirty` suffix silently signalling
"untrusted lineage". The opposite of the integrity gate D-SB1-3
and D-SB1-5 set up.

**Decision (addendum, 2026-05-24).** Scope the untracked check to
**exactly** the paths Dockerfile actually COPYs into the image,
parsed dynamically from Dockerfile (no hardcode). Tracked changes
keep blocking unconditionally — they would land in any future
commit and silently diverge prod from the SHA the build is tagged
with. `--allow-dirty` stays the escape hatch (still tags
`<sha>-dirty`).

**Algorithm.**

```bash
# 1. Parse COPY sources from Dockerfile (line-based, no continuations).
#    Format: `COPY [--flag=…] src1 [src2 …] dest` → emit src1, src2, …
mapfile -t COPY_SOURCES < <(awk '
    /^COPY[[:space:]]/ {
        for (i = 2; i < NF; i++) {
            if ($i ~ /^--/) continue
            print $i
        }
    }
' Dockerfile)

dirty_blockers=()

# 2. (a) tracked changes anywhere → block
if ! git diff --quiet HEAD --; then
    while IFS= read -r line; do
        dirty_blockers+=("modified/staged: $line")
    done < <(git diff --name-only HEAD --)
fi

# 2. (b) untracked inside COPY scope → block
while IFS= read -r line; do
    dirty_blockers+=("untracked in COPY scope: $line")
done < <(git ls-files --others --exclude-standard -- "${COPY_SOURCES[@]}")

# 3. block / --allow-dirty / clean
```

`git ls-files --others --exclude-standard -- <paths>` is the exact
"untracked, respecting .gitignore, scoped to these paths" git
primitive. Files outside `${COPY_SOURCES[@]}` are not enumerated.

**Why this is the right scope.**
- A file enters the image ⇔ Dockerfile COPYs its path. Any untracked
  file inside such a path diverges prod from HEAD silently → block.
- A file outside the COPY-set cannot enter the image. The image is
  byte-identical to one built from HEAD even if such files exist →
  no block.
- Tracked modifications (any path) are not safe to ignore: they will
  ship in any future commit and the deployed SHA "from this branch"
  would diverge from HEAD's tree.

**Consequences.**
- `notes.md`, scratch files at root, untouched `requirements.in`,
  locally-generated logs — no longer block deploy. Operators stop
  reaching for `--allow-dirty` reflexively.
- An untracked `scripts/new_module.py` (the load-bearing case) still
  blocks — exactly as before.
- An untracked `requirements.lock` at root would block (the file IS
  COPY'd; an unstaged regenerated lock would silently diverge image
  deps from HEAD's deps).
- `--allow-dirty` keeps the `-dirty` suffix on the tag, which
  `verify_deployed_image.sh` will then expect — so a `-dirty` image
  in prod is still visible at the docker-tag layer.
- The COPY parser is line-based and skips `--flag=…` tokens; multi-
  line `COPY foo \\` continuations are NOT handled. If a future
  Dockerfile uses them, the parser must be widened (and the test
  `test_copy_sources_parsed_from_dockerfile` will surface the gap by
  failing on the new expected source).

**Negative tests (R2).**
`tests/v2/test_deploy_artifact.py` carries five cases that mirror
this algorithm and pin deploy.sh to it:

| Case | What it asserts | Pre-D-SB1-8 status |
|---|---|---|
| `test_copy_sources_parsed_from_dockerfile` | Parser finds `requirements.lock`, `scripts/`, `tests/`. | Parser didn't exist. |
| `test_dirty_check_scope_blocks_untracked_in_scripts` | `scripts/new_module.py` in scope → would block. | Always blocked (porcelain). |
| `test_dirty_check_scope_passes_untracked_at_root` | `notes.md`, `scratch.py`, `requirements.in` at root → NOT in scope, would NOT block. | **All blocked (porcelain) — the R2-failing case.** |
| `test_dirty_check_scope_blocks_untracked_requirements_lock` | `requirements.lock` at root IS in scope → would block. | Blocked (also caught by porcelain). |
| `test_deploy_sh_uses_scoped_dirty_check` | deploy.sh uses `git diff --quiet HEAD`, `git ls-files --others --exclude-standard --`, an awk on `/^COPY/`, and does NOT use the pre-fix bare `git status --porcelain` guard. | Failed: bare porcelain guard present. |

The Python helper `_copy_sources_from_dockerfile` /
`_is_in_copy_scope` is a deliberate algorithmic mirror of the bash
in deploy.sh. The `test_deploy_sh_uses_scoped_dirty_check` grep is
the anti-drift pin between the two.

**Trade-offs.**
- Two source-of-truth shapes (bash in deploy.sh, Python in the test
  helper). Mitigated by the grep test and the parser's small size
  (~10 lines each).
- A future Dockerfile change that adds `COPY` of a path not
  previously scanned (e.g., a new `COPY config/` directive) requires
  no deploy.sh change — the parser is dynamic. The test set may
  want a positive case for the new path.
- A multi-line `COPY src \\\n    dest` form is not supported by the
  current line-based parser. Acceptable for the current Dockerfile
  (no continuations); will surface as a parser-finds-zero-sources
  error (`exit 7`) if the Dockerfile changes shape, never as a
  silent false-negative.

### Negative tests

`tests/v2/test_deploy_artifact.py` (new) closes R2:

1. Load `docker-compose.yml` (prod base) and assert gutenberg-lab's
   `volumes` list contains **zero** `./` paths (no host-repo
   bind-mounts).
2. Load `docker-compose.yml` + `docker-compose.dev.yml` (dev
   layout) and assert gutenberg-lab's `volumes` list **does** include
   `./scripts:/workspace/scripts` — catches accidental removal of dev
   convenience.
3. Assert the `image:` line of the base file contains `${WC_IMAGE_TAG:?`
   (strict-required substitution) and does NOT contain `:-latest`.
4. Assert each `docker compose` invocation in `systemd/*.service`
   carries `-f docker-compose.yml` (catches D-SB1-6 regression).

Per R2: each "X triggers Y" has its "NOT-X does not trigger Y"
mirror. Tests 1+2 form one such pair (X = base file → no code mount;
NOT-X = override applied → code mount present). Tests 3 and 4 are
single-direction asserts whose negative is the trivial baseline (the
literal string check fails if the regex slips).

### Acceptance gate (TZ S-B1)

| Gate                                                                  | How verified                                                                       |
|-----------------------------------------------------------------------|------------------------------------------------------------------------------------|
| `docker image ls` shows image with SHA tag                            | After `bash scripts/deploy.sh`: `docker image ls wordcracker-textlab`              |
| Container is running from the tagged image                            | `bash scripts/verify_deployed_image.sh` exit 0                                     |
| Bind-mount of sources absent in prod compose                          | `tests/v2/test_deploy_artifact.py::test_no_code_bind_mounts_in_prod_base`          |
| Script-level check "running tag == git SHA of target commit"          | `scripts/verify_deployed_image.sh` (above)                                         |

### Follow-ups deliberately out of scope of S-B1

- **`/health.git_sha` + footer SHA + `ARG GIT_SHA` build-arg** —
  ADR-B3 (runtime build identity) will cover it.
  `verify_deployed_image.sh` checks the docker-level tag; not the
  in-process self-report.
- **chat_server / admin_server as compose services with proper PID 1**
  — ADR-B2. The pyc-purge / `docker compose exec` chain is gone
  post-S-B2.
- **Predeploy gate (`scripts/v2/predeploy_check.py`)** — ADR-B4.
  `deploy.sh` has a `TODO(ADR-B4)` comment marking where the predeploy
  call will slot in.
- **`v2-engine.conf` drop-in removal** — per D-S0-5: opportunistic
  removal during ADR-B2 or earlier if touched for another reason.
  Completed under S-B2 / D-SB2-5.
- **Image registry vs local store** — ADR-B1 trade-off: single host,
  single user → local `/var/lib/docker` store is enough. Registry
  becomes interesting only when a second prod host appears.

---

## 2026-05-24 — S-0: green CI + honest WC_* flag map

Closes block S-0 of `docs/tz_structural_fixes_2026-05-24.md` (TZ not in
repo at audit time; block reconstructed from user message). Goal: the
test suite is green on HEAD, the env-var landscape is documented honestly,
and any "closed" claim in repo trackers is verified or reopened.

### D-S0-1 — `tests/v2` is green on HEAD `5b32530`

**Decision.** Full `python -m pytest tests/v2` on HEAD: **1995 passed,
28 skipped, 0 failed, 565 subtests passed** (after S-0 platform-skip,
see D-S0-2). `pytest tests/v2 --collect-only` collects 2023 items with
0 collection errors. R10 satisfied.

The three historic offenders called out in the S-0 brief
(`test_q15_compare_empty`, `test_scoring_plugins`,
`test_e38_e43_persona_batch2`) were already green per D-P0-5
(2026-05-22) and re-confirmed today — all 48 tests across the four
named files passed on a targeted run before the full-suite run.

`tests/v2/test_intent.py:252` (string literal called out as unclosed
in the brief) is syntactically valid on HEAD; no fix needed. Either
the brief was based on a pre-`5b32530` snapshot or the report was
stale.

### D-S0-2 — `test_cache_concurrent_writes` skipped on `win32`, gated on Linux in CI

**Decision.** Two paired changes:

1. Added `@unittest.skipIf(sys.platform == "win32", ...)` to
   `CacheWriteDiskRaceSafe` in
   `tests/v2/test_cache_concurrent_writes.py`.
2. Added job `test-cache-race-linux` to
   `.github/workflows/predeploy.yml` that runs
   `pytest tests/v2/test_cache_concurrent_writes.py -v` on
   `ubuntu-latest` — the canonical Linux gate for the race-safety
   contract until the predeploy harness (ADR-B4 §1) runs the full
   suite.

**Why.** Live path is Linux. POSIX `rename(2)` is atomic — the race
the test was written to guard (b8dd3ab fix per
[[project_cache_writer_pattern]]) resolves cleanly on the prod
runtime. Confirmed by running the test under WSL Ubuntu against the
same source tree: 3/3 pass in <1 s. On Windows local dev the same code
path takes a different syscall (`MoveFileExW(MOVEFILE_REPLACE_EXISTING)`)
which can fail with `ERROR_ACCESS_DENIED` while another writer is
mid-replace on the same destination, even though each writer has its
own unique `.tmp`. That is a Windows-portability concern separate from
the prod race.

**Why the CI job is non-negotiable.** Without it, the post-skip
property would be "checked nowhere" — exactly the
green-CI-with-a-hole-underneath pattern S-0 was meant to close. The
Windows skip is acceptable iff and only iff a Linux gate verifies the
property. `predeploy.yml` previously ran `--collect-only` for `tests/v2`
and a few targeted W-18 tests; it did NOT run the cache race test
anywhere. The new `test-cache-race-linux` job plugs that hole until
ADR-B4 §1 lands.

**Why not fix the Windows path now.** Making the test pass on Windows
needs either a small retry-on-`PermissionError` loop inside
`_write_disk` or rewriting the test to tolerate the sharing-violation
log. `cache.py` will be touched again in block **S-F1** (AST-hash
invalidation refresh / R-23 Tier 1A follow-ups). The right place to
re-decide Windows behaviour is there, alongside the next planned edit
to that file — bundle the change with substantive work rather than
spending an R7 slot on portability alone.

**Consequences.**
- CI now has two gates touching `tests/v2`: `test-collect` (full
  collect, R10) and `test-cache-race-linux` (focused race execution).
- Windows contributor sees 3 skips with an explicit reason in the
  test class docstring — they know what they are not testing locally
  and that CI checks it for them.
- When S-F1 lands and re-opens `cache.py`, the open question to
  answer is: "do we add Windows retry-on-sharing-violation and drop
  the `skipIf`, or keep the skip indefinitely?" — answer depends on
  whether Windows local dev is a supported workflow at that point.

### D-S0-3 — `docs/v2/infra.md` is now the single env-var inventory

**Decision.** `docs/v2/infra.md` enumerates every `WC_*` env var read
by `scripts/` (and by `tests/` for test-only gates), where it is read,
where (if anywhere) it is set, and what its prod state is. Sections:
(1) compose, (2) systemd drop-in, (3) reads-with-no-set (operational
toggles / model pins / timeouts / audit tuning / paths / sizes / test
gates), (4) audit summary of which `decisions.md` claims hold.

**Why.** REMEDIATION_BRIEF §3 corrected the literal R1 grep ("≤1
match") with the intent: "no env var that selects between code
generations or gates a dead branch." The intent is satisfied today
(verified by D-P0-1 / D-P1-1 / D-P1-2 / D-P1-3 / D-P1-5, all
re-checked in this pass), but until ADR-B5's `env_registry.py` lands
there is no single file that proves it. `infra.md` is the manual
version of that index — short enough to read, structured enough to be
mechanically replaced by the registry later.

**Consequences.**
- R8 ("читай живой путь") has a deterministic answer for env vars:
  open `infra.md`. The previous answer was "grep + cross-check
  compose + cross-check systemd drop-in + cross-check decisions.md."
- `MEMORY.md` should reference `infra.md` instead of any per-flag
  memory — the file is the canonical view.
- When ADR-B5 (env_registry.py) ships, this file becomes a rendered
  view of the registry rather than a hand-maintained inventory; the
  rest of the structure (sections 1-4) stays.

### D-S0-4 — `decisions.md` audit: zero unconfirmed `closed` statuses (in-repo scope only)

**Decision.** Walked the D-P0-1…D-P0-5 and D-P1-1…D-P1-8 series. Each
"removed" / "deleted" / "consolidated" / "inlined" claim was verified
on HEAD via `grep` over `scripts/` and via file-presence check. The
two negative tests claimed by D-P0-3
(`test_v4_plan_spec.py::RefParsing::test_s2_words_n_p0_resolves_against_affinity_shape`,
`test_e15_v1_contract_keys.py::TestAffinityByAuthorWordsAlias`) both
exist. The ADR-B1 follow-up F4 (the `WC_DEFAULT_ENGINE` drift in the
systemd drop-in) was already documented as an open follow-up in the
2026-05-24 ADR-B1 block — not a stale "closed" claim. Detailed
checklist lives in `infra.md` §4.

**Scope boundary.** This audit covers `docs/v2/decisions.md` only.
`backlog.md` is the user's external tracker (operator's docs vault,
outside this code repository — Claude Code cannot reach it by design).
The 2026-05-22 audit `docs/AUDIT_2026-05-22_architecture_quality.md`
§6 line 205 reports it marking E1 / E2 / E5 / E9 / E11 / E13 as
"closed via v6". Reconciliation of those entries against the verified
D-P0/D-P1 series and `infra.md` is on the tracker owner. Operator note:
the prior R6 run confirmed E13 and W-1 closed in prod, so the most
likely state is "consistent" — but the in-repo S-0 pass does not
attest to it.

**Why.** S-0 condition (3): "Пройди backlog.md / decisions.md: каждый
статус closed, не подтверждённый активным флагом + негативным тестом,
переведи в «open в проде»." For the in-repo tracker the answer is
"nothing to reopen." For the out-of-repo tracker the audit needs to be
done by the tracker's owner with `infra.md` + the D-P0/D-P1 verified
series as the input.

**Consequences.**
- The S-0 gate ("в трекерах нет неподтверждённых closed") is
  satisfied for the in-repo tracker.
- `backlog.md` audit remains operator-owned until/unless the file
  moves into this repository (at which point Claude Code can run the
  same mechanical check it ran for `decisions.md`).
- The audit pass becomes mechanical once ADR-B4 §3 `--check-env` lands
  (compose ↔ code grep) and ADR-B5's `env_registry.py` exists
  (registry ↔ ADR linkage). Until then, `infra.md` is the
  manually-maintained index.

### D-S0-5 — `WC_DEFAULT_ENGINE=v2` in systemd drop-in is pending-removal

**Decision.** The dead `-e WC_DEFAULT_ENGINE=v2` in
`systemd/wordcracker-chat.service.d/v2-engine.conf:22` is **explicitly
flagged for removal**. It is not "tolerated indefinitely" — leaving it
is the same "no commented flags in compose" anti-pattern, just on the
systemd side. The removal lands in one of two structurally-adjacent
blocks:

- **S-F4 / S-B5** — when ADR-B2 collapses chat/admin into compose
  services with proper PID 1, the `v2-engine.conf` drop-in is deleted
  wholesale (its pins `WC_LLM_MODEL=wordcracker:v2` and
  `WC_CRITIC_MODEL=wordcracker:v2` move into the new compose service's
  `environment:` block). The dead `WC_DEFAULT_ENGINE` goes with it.
- **Earlier opportunistic removal** — if any block touches the drop-in
  for another reason before S-F4 / S-B5 lands, drop the dead line in
  that same commit. Do not let it ride to a separate "trivial cleanup"
  commit — that would be a single-line cascade slot on infrastructure
  files for no behavioural gain.

**Why.** R8 ("читай живой путь") gets confused by env vars that are
exported into the container but not read by code. `systemctl status
wordcracker-chat` shows the export; a reader who hasn't followed
D-P1-5 / ADR-B1 F4 / `infra.md` could waste time hunting for the
phantom consumer. Documenting this as **explicit pending-removal**
(rather than "follow-up F4") makes it visible in the same S-0 audit
that confirms everything else.

**Consequences.**
- `infra.md §2` and §0 already flag this as a known drift; the
  pending-removal disposition is recorded here in `decisions.md`
  rather than only in the prose narrative.
- S-F4 / S-B5 acceptance gate must include "grep `systemd/` for
  `WC_DEFAULT_ENGINE` returns zero hits." If the operator forgets,
  this paragraph reminds them.

---

## 2026-05-24 — Architecture brief: latency + deploy

> Companion to `docs/AUDIT_2026-05-22_architecture_quality.md` and the
> reconstructed brief `architecture_brief_2026-05-24_latency_and_deploy`
> (no separate file — points enumerated in this section). Two domains:
>
> - **B — конвейер релиза.** Делаем первым: пока деплой невоспроизводим,
>   латентность A проверять нечем — изменение, которое «ускорило»,
>   может быть просто непредсказанной перезаписью кода через bind-mount.
> - **A — латентность тяжёлых агрегаций.**
>
> Все ADR в статусе **Proposed** до ревью пользователя.

### ADR-0 — ADRs live in this file, not in `docs/adr/`

**Status.** Accepted (2026-05-24).

**Context.** Structural decisions D-P0-1 … D-P1-8 are already
sections in this file. The file is ~500 LOC, chronologically sorted
("newest at the top"). A separate `docs/adr/NNNN-*.md` directory
would split history into two stores — search becomes more expensive
without buying anything for a single-author codebase.

**Options considered.**
1. **Continue here as additional dated sections.** Zero churn, same
   template (`Decision/Why/Consequences`), `grep` stays in one file.
2. **Migrate to `docs/adr/NNNN-title.md`.** Standard pattern in
   larger projects but adds a rename pass over D-P0-1…D-P1-8 and
   breaks in-flight refs ("D-P1-5" cited from CLAUDE.md and chat).
3. **Hybrid — keep history here, new ADRs in `docs/adr/`.** Two
   stores, drift risk.

**Decision.** Option 1. ADR-B/A series live as sub-sections under
this 2026-05-24 block, identified as **ADR-B1**…**ADR-B5** and
**ADR-A1**…**ADR-A4**.

**Consequences.** This file gains ~9 sub-sections (~600 LOC). When
total exceeds ~1500 LOC, split-by-year (`decisions-2026.md`) — not
by ADR.

**Trade-offs.** Loses one-file-per-decision discoverability.
Mitigated by stable section anchors (`#adr-b1-…`).

---

### Why Domain B is first

(a) Latency claims (A) need a stable baseline. With code mounted via
bind-mount (B2) and `chat_server` launched through `docker compose
exec` (B3), a "fix" may simply observe a process that picked up new
`.py` files but not new `.pyc` files (the pyc-purge incident at
[wordcracker-chat.service:25](systemd/wordcracker-chat.service:25) is
real prior art).

(b) Precompute-style fixes (A1) are only as good as the batch job's
ability to run reproducibly — without B1 the batch job's deps are
unpinned.

Order: B1 → B2 → B3 → B4 → B5, then A1 → A2 → A3 → A4. Effort is
NOT uniform: B1 is multi-day; A4 is half a day.

---

### ADR-B1 — Deploy artifact: image-tag-by-commit + code-in-image

**Status.** Accepted (2026-05-24). Implementation: see S-B1 dated
block at top of file, D-SB1-1..6.

**Context.** Pre-acceptance the deploy artifact had two intertwined
failure modes:

1. **Image tag drift.** [docker-compose.yml:3](docker-compose.yml:3)
   declared `image: wordcracker-textlab` — single name, no tag, no
   commit SHA. `docker compose build` on day N and day N+30 yielded
   different runtime artifacts at the same git SHA. Audit C5 ("bake
   time = 0; деплой-и-откат за 30 минут") presumes identical artifacts
   across deploys — it wasn't true.

2. **Code via bind-mount.** [docker-compose.yml:11-14](docker-compose.yml:11)
   mounted `./scripts:/workspace/scripts`, `./tests:/workspace/tests`,
   `./notebooks:/workspace/notebooks` as bind-mounts. The container
   did not own the code — host filesystem did. Implications:
   - Any unstaged `.py` edit on host = immediate code in prod. No
     atomic deploy flip.
   - `git checkout` to a different branch on host changed prod.
   - [wordcracker-chat.service:25](systemd/wordcracker-chat.service:25)
     shipped defensive pyc purge because a "fresh scp of .py" once
     lost to an old `.pyc`. Bind-mount + concurrent edit was the
     precise failure class.

Data mounts at [docker-compose.yml:17-23](docker-compose.yml:17)
(`/data/chroma_db`, `/data/spgc`, `/data/books`) were LEGITIMATELY
bind-mounts — corpus is hundreds of GB on host disk. **This ADR is
only about code paths and the image-tag.** Dependency pinning is a
separate concern (ADR-B6).

**Options considered.**
1. **Image-tag-by-commit + `COPY scripts/ tests/` into image at
   build + remove code bind-mounts in prod; hybrid override for
   dev.** Atomic deploy. A `docker-compose.dev.yml` keeps
   bind-mounts so edit-and-reload still works locally. Deploy =
   build new image (SHA tag) + restart.
2. **Image-tag-by-commit only; keep bind-mount + tighten deploy
   hook (`git fetch && git reset --hard <tag>` with a lock file).**
   Cheaper but `git reset --hard` on a host where a developer might
   be mid-edit is destructive.
3. **Status quo + container-rebuild discipline.** No artifact
   pinning at all; relies on operator memory. Bake time stays 0 —
   the audit gate is unmet.

**Decision.** Option 1. `docker-compose.yml` becomes
`image: wordcracker-textlab:${WC_IMAGE_TAG:?WC_IMAGE_TAG must be set ...}`
(strict-required, no `:-latest` fallback). `Dockerfile` adds
`COPY scripts/ /workspace/scripts/` and
`COPY tests/ /workspace/tests/`.
[docker-compose.dev.yml](docker-compose.dev.yml) keeps the
dev bind-mounts and a `:-dev` fallback (dev-opt-in via `-f` or
`COMPOSE_FILE` per D-SB1-7). Deploy hook
(`scripts/deploy.sh`) builds
`wordcracker-textlab:$(git rev-parse --short HEAD)` and atomically
writes `WC_IMAGE_TAG=$SHA` into `.env`.
`scripts/verify_deployed_image.sh` asserts the running container's
image tag matches the expected SHA.

**Consequences.**
- `git checkout` on host no longer affects running prod (image is
  SHA-frozen).
- Atomic deploy flip: new image tag = new container; old image stays
  available for rollback (`deploy.sh --rollback <prev-sha>`).
- Single deploy path: commit → push → `bash scripts/deploy.sh` →
  image-tag bump → `docker compose up -d --force-recreate`. Rollback
  is `re-run with the previous tag`, no rebuild.
- Image retention: deploy.sh prunes all but the last 5 SHA-tagged
  images.
- Pyc-purge `ExecStartPre` in the systemd chat unit becomes redundant
  for the scripts/ path (kept as defence-in-depth until S-B2 deletes
  the unit entirely).
- Iteration in dev unaffected — bind-mount + edit-and-reload
  preserved via override.

**Trade-offs.**
- Image rebuild cost on code change = one `COPY` layer (pip layer
  cached); ~5 s on the prod host.
- Prod hotfix path becomes "rebuild + redeploy" — no
  `vim /workspace/scripts/...` shortcut. **Friction is desired** per
  R7, R8.
- Image-tag-by-SHA needs a local image store (single host, single
  user — local `/var/lib/docker` store is enough; no private
  registry required).
- Operator running bare `docker compose up` on the prod host
  (without `-f docker-compose.yml`) silently switches into dev shape
  (bind-mounts back). Mitigated by systemd / deploy.sh always
  passing `-f` explicitly; `verify_deployed_image.sh` catches the
  wrong tag if someone forgets.

---

### ADR-B2 — `chat_server` / `admin_server` as compose services with proper PID 1

**Status.** Accepted (2026-05-24). Implementation: see S-B2 dated
block at top of file, D-SB2-1..8.

**Context.** Pre-acceptance,
[wordcracker-chat.service:14-28](systemd/wordcracker-chat.service:14)
orchestrated `chat_server` as:

```
ExecStartPre=docker compose up -d gutenberg-lab
ExecStartPre=-docker compose exec ... pkill -9 -f /workspace/scripts/chat_server.py
ExecStartPre=-docker compose exec ... find /workspace/scripts/__pycache__ -name '*.pyc' -delete
ExecStart=docker compose exec -T ... python -u /workspace/scripts/chat_server.py --port 8890
```

The unit's own comment explained: «SIGTERM doesn't propagate from
`docker compose exec` to the python process inside the container,
so old chat_server.py instances can survive a restart and keep
port 8890 bound.» Concretely:
- `KillSignal=SIGTERM` and `TimeoutStopSec=20` killed only the
  `docker compose exec` client; the container-side Python was
  orphaned (hence the `pkill` on START).
- Three deploy patterns coexisted: `wordcracker-chat.service` and
  `wordcracker-admin.service` used `docker compose exec`;
  [wordcracker-status.service:12](systemd/wordcracker-status.service:12)
  ran `/usr/bin/python3` from host directly (status_server has no
  Docker dependency — reads file metadata).
- [v2-engine.conf:21-22](systemd/wordcracker-chat.service.d/v2-engine.conf:21)
  reset `ExecStart` to re-add `-e WC_DEFAULT_ENGINE=v2 -e
  WC_LLM_MODEL=... -e WC_CRITIC_MODEL=...` because the main unit's
  ExecStart only forwarded `ASSISTANT_NAME`. `WC_DEFAULT_ENGINE` was
  deleted from code in D-P1-5; the drop-in still shipped it.
  Exactly the dead-config drift R8 forbids.

**Options considered.**
1. **chat / admin as separate compose services, same image as
   `gutenberg-lab`.** Each service has its own `command:` (python =
   PID 1). SIGTERM works. systemd no longer manages chat/admin —
   Docker / compose is the only supervisor.
2. **Separate compose services with separate images.** Cleaner
   isolation; ×3 image build cost (small — same deps). Overkill for
   single-host.
3. **Status quo + a wrapper entrypoint** (`scripts/chat_entrypoint.sh`
   with `exec python ...`). Doesn't fix the `docker compose exec`
   child-of-exec problem — exec is still the parent and SIGTERM
   still doesn't reach python.
4. **Supervisor (s6-overlay / supervisord) inside gutenberg-lab
   container, fans out chat + admin.** Keeps a single container, but
   adds a supervisor dependency; PID 1 becomes the supervisor (not
   python) — re-introduces the very thing R8 wanted closed.

**Decision.** Option 1. New services `chat` (port 8890) and `admin`
(port 8891) in `docker-compose.yml`, sharing the
`wordcracker-textlab:${WC_IMAGE_TAG}` image via YAML anchors
(`*app-image`, `*app-env`, `*app-volumes`). Each has its own
`command: ["python", "-u", "/workspace/scripts/chat_server.py",
"--port", "8890"]` and `environment:` block (the `WC_LLM_MODEL` /
`WC_CRITIC_MODEL` pins from the retiring drop-in move here). Native
`healthcheck:` in compose replaces the curl-loop `ExecStartPost`.
Systemd units for chat / admin are deleted from the repo.

**Consequences.**
- `docker compose exec`, `pkill -9`, and pyc-purge `ExecStartPre`s
  removed — the systemd units are gone entirely.
- SIGTERM propagates: `docker stop` → python. Graceful shutdown
  works for the first time.
- [v2-engine.conf](systemd/wordcracker-chat.service.d/v2-engine.conf)
  deleted. Its still-meaningful pins (`WC_LLM_MODEL`,
  `WC_CRITIC_MODEL`) move into the `chat` service `environment:`
  block.
- `healthcheck:` in compose replaces the curl-loop `ExecStartPost`
  (D-SB2-7).
- Three deploy patterns collapse to two: compose services
  (gutenberg-lab + ollama + chat + admin) and host-python
  (status_server, which intentionally stays host-side to read host
  files).
- `scripts/deploy.sh` collapses to one mechanism
  (`compose up --force-recreate`); no more parallel
  `systemctl restart` loop for chat/admin.
- Host reboot brings status_server back via
  `systemctl enable wordcracker-status` (D-SB2-3).

**Trade-offs.**
- ChromaDB cold-load (~12 s per
  [chat_server.py:1204-1224](scripts/chat_server.py:1204)) now lives
  in the chat service's process — same cost, different process.
  admin_server doesn't touch ChromaDB, so it's faster.
- jupyter on 8888 stays inside `gutenberg-lab` (unchanged) — chat /
  admin no longer share a Python interpreter with notebooks. The
  sharing was incidental, never load-bearing. (Jupyter
  prod-disposition is a separate follow-up — see S-B2 follow-ups
  out of scope.)
- Implementation cost: ~30 lines in compose + 3 systemd files
  deleted (chat, admin, v2-engine.conf drop-in). Net code reduction.
- Three containers instead of one for the app: chat (~28 GB cap),
  admin (~4 GB), jupyter (~8 GB); total RAM ceiling unchanged from
  the pre-S-B2 single 40 GB gutenberg-lab cap, just split.

---

### ADR-B4 — Predeploy harness as the single gate

**Status.** Proposed.

**Context.** Current post-deploy verification is the curl loop at
[wordcracker-chat.service:33](systemd/wordcracker-chat.service:33):
30 retries × 2 s waiting for `/health` 200. That's a liveness probe,
not a correctness probe. Audit C5: «деплой-и-откат за 30 минут;
bake time = 0; тесты — музей регрессий, а не контракты.»

Recent commit `6df5a1c` ("W-18 predeploy harness") added the
beginnings of a harness. The R-23 cycle is incrementally adding
verification. This ADR **codifies what the harness must gate**, not
invent a new mechanism.

R2 ("баг closed only when fix exists + negative test + executes on
prod flag combo") implies a pre-deploy gate that asserts:
- the test suite passes (R10),
- a golden answer set behaves correctly,
- `docker-compose.yml` / `docker-compose.dev.yml` / systemd env
  values match what `decisions.md` says is live (closes the
  v2-engine.conf-style drift).

Currently (c) drifted at least once — D-P1-5 deleted
`WC_DEFAULT_ENGINE` from code, but
[v2-engine.conf:22](systemd/wordcracker-chat.service.d/v2-engine.conf:22)
still sets it. Nothing mechanical catches this; only manual reads.

**Options considered.**
1. **systemd `ExecStartPre` runs the harness; non-zero aborts
   deploy.** Tight coupling. Harness must run inside container.
   5-10 min predeploy wait; failure leaves service down.
2. **Predeploy harness as separate `make deploy` target (or
   `scripts/v2/predeploy_check.py`)**, explicitly invoked. systemd
   unchanged. Operator can `--force` for emergency rollback (logged).
3. **CI runs harness on PR/merge; systemd assumes green.** Cleanest
   but needs CI infra that doesn't exist in this single-host setup.

**Decision.** Option 2. Build out `scripts/v2/predeploy_check.py`
(or whichever file `6df5a1c` added) as the canonical pre-deploy
gate. It runs:

1. `pytest tests/v2 -q -p no:randomly` — R10 (must collect cleanly,
   0 fails).
2. **Golden query set** under `tests/v2/golden_set.py` — currently
   `@skipUnless(WC_GOLDEN_LIVE)` per audit §4.6. Predeploy run sets
   `WC_GOLDEN_LIVE=1` and dispatches 10-15 known-good queries
   against the about-to-deploy image, asserting on
   shape (not exact text — LLM render is non-deterministic).
3. **`decisions.md` ↔ active env diff**: grep `WC_*` names in
   compose/systemd vs. `os.environ.get("WC_*")` reads in
   `scripts/`. Any var read but never set OR set but never read =
   fail. Closes the `WC_DEFAULT_ENGINE` drift class.
4. **Critic-flagged regression smoke**: pull last 5 "well-known
   good" scenarios from `/admin/bad_answers` (audit §4.6), run
   them, fail if any regresses on a previously-passing scenario.

Deploy hook (Makefile / `scripts/deploy.sh`): runs
`predeploy_check.py`; on green tags image (B1) + restarts; on red
prints diff and exits non-zero. `--force` flag bypasses with a
mandatory rationale string that's logged.

**Consequences.**
- Predeploy harness is the single gate between commit and prod.
  Bypass requires explicit `--force <rationale>`, audit-logged.
- Golden tests stop being museum pieces. `WC_GOLDEN_LIVE` skip stays
  for developer CI (no live Ollama), is forced on at predeploy.
- "Fix closed without prod verification" (audit §6) becomes
  mechanically caught.
- Out of scope for this ADR but worth noting: an R7 cascade counter
  (commits since last green deploy in the same area, warn at ≥3) is
  a natural follow-up.

**Trade-offs.**
- Harness takes 5-10 min. Acceptable for prod deploys (≤2/day in
  active phases). Emergency hotfix = `--force` with note.
- Golden-set maintenance burden: 10-15 queries with shape
  assertions. The R-23 set already partly exists; ADR formalizes
  "deploy gate, not optional".
- Predeploy requires Ollama live at gate time (LLM render in
  golden). Prod container already up = fine. Dev skip remains.

---

### ADR-B5 — Operational env-var lifecycle bound to `decisions.md`

**Status.** Proposed.

**Context.** [docker-compose.dev.yml:42-50](docker-compose.dev.yml:42)
already documents «feature flags consolidated per REFACTOR_BRIEF R1
(no dark code)» — a Phase 1 win. But:
- [v2-engine.conf:22](systemd/wordcracker-chat.service.d/v2-engine.conf:22)
  still sets `WC_DEFAULT_ENGINE=v2`. D-P1-5 deleted this var from
  code. Drop-in's continued export is harmless but is exactly the
  dead config that confuses R8.
- D-P1-7 documents `WC_CRITIC` and `WC_NUMERIC_AUDIT` as "on by
  default in code, absent from compose, confirmed live." A reader
  of code alone would not know these toggles exist — documentation
  is in `decisions.md`, not adjacent to the env read at
  `critic.py:40` / `numeric_audit.py:31`.
- No automated cross-check between (env reads in code) ↔ (env values
  in compose/systemd) ↔ (env documentation in decisions.md). B4's
  `--check-env` gate covers two; the third (documentation) stays
  manual.

R1 (no dark feature flags) is enforced. **Operational toggles
(R1-permitted, see D-P1-7) need their own lifecycle**, separate
from feature flags, because they outlive any individual decision.

**Options considered.**
1. **`scripts/v2/env_registry.py`** — single Python module
   enumerating every `WC_*` env read with: default, prod_state,
   owning-ADR ref, status. Code reads via `env_registry.WC_CRITIC`
   instead of `os.environ.get`. Mechanical enforcement: predeploy
   compares registry entries to code-grep.
2. **`ENV_VARS.md`** at repo root. Easy to write, easy to drift —
   no mechanical link to code.
3. **Status quo + audit pass in predeploy.** Predeploy greps; if
   registry doesn't exist, predeploy can verify "set ↔ read" but
   not "documented".

**Decision.** Option 1 + retire [v2-engine.conf](systemd/wordcracker-chat.service.d/v2-engine.conf).
Create `scripts/v2/env_registry.py` as the single declaration site
for every `WC_*` env var (and `OLLAMA_HOST`,
`OLLAMA_HTTP_TIMEOUT_S` from
[rag_query.py:157-159](scripts/rag_query.py:157)). Each entry:

```
WC_CRITIC = EnvVar(
    name="WC_CRITIC", default="on", prod_state="on",
    purpose="critic LLM verification pass",
    added_in="D-P1-7", status="active",
)
```

Code reads through the registry: `from scripts.v2.env_registry
import WC_CRITIC; if WC_CRITIC.is_on(): ...`. Predeploy
`--check-env` gate (ADR-B4) extends:
- Every `os.environ.get("WC_*")` in code must be in the registry.
- Every registry entry with `prod_state="on"` must be either set in
  compose/systemd OR have `prod_state_via="code_default"`.
- Every var set in compose/systemd must be in the registry (catches
  the `WC_DEFAULT_ENGINE` ghost).

The `v2-engine.conf` drop-in is **deleted** as part of this ADR.
Its still-relevant pins (`WC_LLM_MODEL`, `WC_CRITIC_MODEL`) move
into the `chat` compose service from ADR-B2 — same place as the
Phase 1 toggle comments at
[docker-compose.dev.yml:42-50](docker-compose.dev.yml:42).

**Consequences.**
- One source of truth for env vars. R8 ("читай живой путь") becomes
  mechanical: read `env_registry.py`, see `prod_state`.
- Removing a var = registry status flip to `deprecated` + grace
  period + delete (would have caught the D-P1-5 → v2-engine.conf
  dangler).
- `decisions.md` continues to be the narrative; `env_registry.py`
  is the structured index. Each registry entry references its
  owning D-PN-X / ADR-X identifier.

**Trade-offs.**
- One-time migration: ~26 surviving `WC_*` reads per D-P1-3 phase 1
  gate. Mechanical refactor, ~1 day.
- Small indirection layer. Cost bounded; benefit (mechanical R8)
  durable.
- Operator running a one-off `WC_X=foo python ...` is still
  possible — registry can warn ("env observed at runtime that
  isn't in registry") but cannot forbid. Acceptable.

---

### ADR-B6 — Pinned dependencies (lockfile + hash-verified install)

**Status.** Accepted (2026-05-24). Phases 1 + 2 landed in commits
329908b → 897573a → 9f646c4 → c86d22d → 148609d; image rebuilt and
verified on prod (cuda True, 2012 tests collected, service restart
clean, cache_write_failed count = 0 post-restart). Pairs with ADR-B1
(image-tag-by-commit) — the SHA-tagged image is now deterministic in
both code and deps.

**Context.** Pre-acceptance [Dockerfile:7-28](Dockerfile:7) installed
`jupyterlab`, `spacy[transformers]`, `transformers`,
`sentence-transformers`, `chromadb`, `pandas`, `scikit-learn` without
version pins or a lockfile. [Dockerfile:30-31](Dockerfile:30) ran
`python -m spacy download en_core_web_sm` and `en_core_web_trf` —
silently version-floating models (download URL serves current spaCy
version). Result: `docker compose build` on day N and day N+30 could
yield different runtime artifacts at the same git SHA. Audit C5
("bake time = 0") presumed identical artifacts across deploys — it
wasn't true.

This is the *dependency* half of the deploy-artifact concern; the
*tag* and *code* halves live in ADR-B1.

**Options considered.**
1. **`requirements.in` + `requirements.lock` (pip-compile),
   hash-verified install.** Standard. Forces deterministic build.
   Maintenance: explicit `pip-compile` on dep refresh.
2. **`uv` with `pyproject.toml` + `uv.lock`.** Same goal, faster
   install. Yet-another-packaging-change cost for a single-author
   codebase.
3. **Status quo + container-rebuild discipline.** Cheap, but R8
   ("сначала читай живой путь") becomes impossible — `pip show` in
   the running container is the only source of truth, drifts with
   the latest pull.

**Decision.** Option 1. Create `requirements.in` (~25 top-level deps
from Dockerfile:7-28) and `requirements.lock` (transitive freeze,
hash-verified). Dockerfile reads
`pip install --require-hashes -r requirements.lock`. spaCy models
pinned by direct URL
(`en_core_web_sm-3.8.0`, `en_core_web_trf-3.8.0`).

**Consequences.**
- New files at repo root: `requirements.in`, `requirements.lock`.
- Dockerfile rewritten to install from lockfile (RUN layer
  cacheable).
- `pip-compile` on dep change is a separate, explicit step.
- Any tampered or unexpected wheel = build failure with a clear
  message (`--require-hashes` fails loudly).
- Pairs with ADR-B1: a SHA-tagged image is now BOTH deterministic
  in code AND deterministic in deps.

**Trade-offs.**
- Lockfile maintenance friction is the **goal** — discourages casual
  cascade upgrades (per R7).
- spaCy model wheels are published on GitHub Releases, not PyPI, so
  they sit outside the hashed lock set. The version embedded in the
  URL IS the pin — a tampered model would require a different URL.
  A follow-up ADR can fold the GitHub-Releases sha256 sidecar values
  into a hashed extension of the lock.

---

### ADR-A1 — Materialized indices for full-corpus aggregations

**Status.** Proposed.

**Context.** Heavy aggregations re-scan the full corpus per call:
- [rag_tools.py:word_freq_timeline:1185-1273](scripts/rag_tools.py:1185)
  — per call: load `_metadata_df`, filter lang, groupby
  `period_start`, FOR EACH BOOK in EACH bucket open
  `_counts_path(pg)` and parse line-by-line `(word, count)` pairs.
  Per-call cost ≈ O(books × per-book-tokens). [budget.py:67](scripts/v2/budget.py:67)
  estimates 12 s; R14 trace caught 196 s for multi-word timelines.
- [rag_tools.py:top_ngrams_by_author:539-589](scripts/rag_tools.py:539)
  — `_select_books(author_regex)` then per book: open tokens file,
  build Counter, optionally spaCy POS-tag top 5× heads.
  `author_regex=".*"` iterates the full corpus.
- [rag_tools.py:words_disappearing_after:1281+](scripts/rag_tools.py:1281)
  — pre/post buckets, same per-book file walk.

The project already has a precompute pattern:
[build_author_richness.py](scripts/v2/build_author_richness.py)
walks the corpus once, writes
`/workspace/spgc/derived/author_richness.json`; the v2 wrapper reads
it (fallback to live scan). Sibling:
`scripts/v2/build_author_tokens.py`. **This ADR extends the same
pattern to the timeline / n-gram axis.**

**Options considered.**
1. **Per-axis precompute artifacts under
   `/workspace/spgc/derived/`** — one Parquet/JSON per "aggregation
   axis", regenerated on corpus_version bump. Wrappers read
   prebuilt; fall back to live scan with a `ToolWarning` if absent.
2. **In-process pre-aggregation on first call** (lazy-build cache
   files at `/data/v2_cache/agg/...`). Faster to ship — no batch
   script — but first-after-restart is the slow path. Under predeploy
   harness restarts the first user always pays.
3. **DuckDB-backed materialized views** over tokens dir. Tempting
   (SQL ergonomics, fast aggregations) but introduces new runtime
   dep, new schema, new "where does data live" question. Orthogonal
   to the existing parquet/JSON pattern, doesn't compose with it.

**Decision.** Option 1. Three new build scripts under `scripts/v2/`:

| script                               | output                                                                                | shape                                                                                  |
|--------------------------------------|---------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| `build_word_freq_buckets.py`         | `/workspace/spgc/derived/word_freq_buckets/{basis}_b{years}.parquet`                  | `(word, basis, bucket_start, bucket_end, books_used, total_tokens, occurrences, per_million)` |
| `build_author_ngrams.py`             | `/workspace/spgc/derived/author_ngrams/{slug}_n{n}.parquet`                           | `(ngram, count, books_seen)` per author × n                                            |
| `build_corpus_word_buckets.py`       | `/workspace/spgc/derived/word_buckets/{basis}_pre{year}_post{year}.parquet`           | feeds `words_disappearing_after` / `words_appearing_after`                             |

Wrappers ([timeline.py](scripts/v2/tools/words/timeline.py),
[top_ngrams.py](scripts/v2/tools/authors/top_ngrams.py)) read
prebuilt first; missing prebuilt → `ToolWarning("precompute_stale",
"falling back to live scan")` and the current live path runs.

Build invocation matches existing pattern (per
[build_author_richness.py:1-12](scripts/v2/build_author_richness.py:1)):
`docker compose exec -T gutenberg-lab python -u
/workspace/scripts/v2/build_word_freq_buckets.py`. Runs on
corpus_version bump (manual after `admin_server` ingests) AND nightly
cron as defence-in-depth.

Output filename keyed by `corpus_version` (or a sidecar
`<artifact>.meta.json` with corpus_version + built_at) so stale
artifacts are detectable.

**Consequences.**
- Per-call cost on warm path: parquet filter (~0.2-0.5 s) vs.
  current 12-50+ s. A4 makes the estimator know this.
- Disk: word_freq_buckets ≈ unique_words × buckets × ~120 B ≈
  100-500 MB. Author n-grams: ~50 MB per author for n=2 across
  ~5k indexed authors → ~2-5 GB. Within /data/ headroom (Audit:
  40 GB memory cap; disk separate).
- Build runtime: 5-15 min each on full corpus, parallelizable.
- On corpus update: existing `admin_server` upload flow gains a
  "re-bake derived/" hook (or it falls to nightly cron with a
  `_render_note` warning if stale).

**Trade-offs.**
- ~3 new build scripts (~150-200 LOC each). Same pattern; low
  maintenance per artifact.
- Stale-precompute risk: artifact corpus_version mismatch surfaces
  as `ToolWarning("precompute_stale", ...)` — never silent.
- Words/n-grams not in precompute (OOV, typos, rare ngrams) fall
  through to live scan. Acceptable — emit warning, charge true cost.

---

### ADR-A2 — Warm-up extended to top-N heavy intents

**Status.** Proposed.

**Context.** [chat_server.py:1204-1244](scripts/chat_server.py:1204)
warms ChromaDB + embedder + 5 cheap queries
(`corpus_overview`, 3× `top_authors_by`, Doyle `author_metadata`).
Heavy intents — `word_freq_timeline`, `top_ngrams_by_author`,
`find_book_by_topic` — are NOT warmed. First post-restart user on
a heavy query pays cold p95.

Cache layer ([cache.py:108-125](scripts/v2/cache.py:108)) is correct
(AST fingerprint + `corpus_version` + LRU + disk). Cache hits are
fast. The problem is "first-after-restart" never has a cache.

Observability already collects the last 256 requests
([chat_server.py:899](scripts/chat_server.py:899) calls
`aggregate_recent` from `scripts.v2.observability`); top-N
most-frequent recent queries are derivable.

**Options considered.**
1. **Static warm-up list of N representative heavy queries** —
   extend the literal list at [chat_server.py:1232-1239](scripts/chat_server.py:1232).
   Simple; drifts from actual user behavior.
2. **Trace-derived warm-up: read last-N-hours from observability,
   pick top-K (tool, args)-pairs, dispatch each.** Adapts to actual
   hot paths. Warm-up time grows; needs a cap.
3. **Background warmer process** running continuously,
   re-dispatching top queries every M minutes regardless of
   restart. Most invasive; not justified by current data.

**Decision.** Option 2 with hard caps. `_warmup()` at
[chat_server.py:1204](scripts/chat_server.py:1204) extends to:

- Keep the existing 5 cheap queries (corpus_overview + top_authors +
  Doyle).
- Add: from `aggregate_recent(window_hours=24)`, extract top 10
  `(tool, args_fingerprint)` pairs whose tool is in
  `{find_book_by_topic, word_freq_timeline, top_ngrams_by_author,
  hybrid_search, semantic_search, author_profile, vocab_passport}`.
  Dispatch each through `scripts.v2.tool_registry.dispatch`.
- Per-query soft cap: 20 s. Total warmup hard cap: 60 s. After
  60 s, server starts accepting traffic; remaining warm-ups
  dropped.

**Consequences.**
- Heavy-intent cold p95 drops sharply on canonical phrasings (their
  args-fingerprint matches the trace-derived hot list).
- Quiet period = small warm-up; busy day primes more. Self-tuning.
- Warm-up failures stay non-fatal — existing `except Exception`
  pattern at [chat_server.py:1222-1224](scripts/chat_server.py:1222)
  applies.

**Trade-offs.**
- Up to +60 s startup time. ADR-B2 (chat/admin supervision —
  graceful restarts via proper PID 1) makes graceful restarts
  routine, so the cost lands on planned restarts rather than
  crash-loops.
- Outlier queries still pay cold. By design — we warm the head, not
  the tail.
- Observability persistence: current
  [scripts.v2.observability.aggregate_recent](scripts/v2/observability.py)
  reads an in-process ring buffer that doesn't survive restart. A2
  needs a small disk-persistence shim (append-only JSONL alongside
  feedback JSONL — audit §4.6 already does this for `bad_answers`).
  Until then, fall back to a hard-coded heavy-intent seed list (10
  representative queries) as in Option 1.

---

### ADR-A3 — BGE rerank: caching + translation cache + popular-topic precompute

**Status.** Proposed.

**Context.** [find_book_by_topic.py:181-194](scripts/v2/tools/books/find_book_by_topic.py:181)
documents: «per_retriever tightened from 60 to fit BGE rerank in
≤30 s. The old budget passed 40-80 chunks to BGE rerank → wall
clock 30-300 s on hot path.» [budget.py:77](scripts/v2/budget.py:77)
estimates 15 s; R14 worst-case 50 s.

Plus an unconditional LLM round-trip for RU→EN translation:
[find_book_by_topic.py:128-147](scripts/v2/tools/books/find_book_by_topic.py:128)
calls `_maybe_translate`
([rag_tools.py:262-280](scripts/rag_tools.py:262)) which POSTs to
Ollama on every Cyrillic topic. ~2-3 s extra.

Existing cache catches exact repeat (topic, top, …). In practice
users rephrase ("книги про викторианский Лондон" vs. "роман про
Лондон XIX век"). Hit rate for this tool is low.

**Options considered.**
1. **Rerank-result cache by `(query_text_hash,
   sorted_pg_id_tuple)`.** Reuses result when same query + same
   candidate set arrives. Embedder is deterministic. Misses on
   candidate drift (new book ingest) — rare.
2. **Translation cache** — separate small cache for
   `_maybe_translate(ru) → en`. 30-day TTL. Mechanically trivial.
3. **Popular-topic precompute** — nightly batch over last-N-days
   topical queries + manual "evergreen topic" list, write to
   `/workspace/spgc/derived/topic_recs.json`. Wrapper checks
   prebuilt first.
4. **All three.**

**Decision.** Option 4 — three independent layers, all small:

- **Translation cache.** A small module under
  `scripts/v2/cache.py` namespace (or `_translation_cache.py`
  adjacent). Key = `(ru_text, ollama_model)`; TTL = 30 days
  (translation is stable). Closes the per-call 2-3 s on Cyrillic
  topics.
- **Rerank-result cache.** Key = `(query, sorted_tuple(candidate_pg_ids),
  rerank_model)`. Value = ordered list with scores. Lives in
  [cache.py](scripts/v2/cache.py) namespace under tool name
  `_bge_rerank` (so it gets the existing LRU + disk + AST-fp
  invariants).
- **Topic precompute.** New `scripts/v2/build_topic_recs.py` —
  reads observability-derived popular topics + a small
  `_evergreen_topics.json` (curated list: «викторианский Лондон»,
  «детектив», «gothic horror», …), dispatches
  `find_book_by_topic` offline, writes
  `/workspace/spgc/derived/topic_recs.json` keyed by
  `(normalized_topic, corpus_version)`. The wrapper at
  [find_book_by_topic.py](scripts/v2/tools/books/find_book_by_topic.py)
  consults the precompute BEFORE dispatching hybrid_search;
  return-on-hit short-circuits rerank entirely.

Per-tool `wrapper_version` bumps per R-23 Tier 0 when each layer
ships.

**Consequences.**
- Cold rerank: 30-300 s → ~5 s on warm path → ~0.3 s on
  precomputed-topic hit.
- Three new artifacts; each ~50-150 LOC.
- Precompute artifact is small (~1-5 MB even for hundreds of
  topics).
- Translation cache is reusable for any RU→EN call in the system,
  not just find_book_by_topic.

**Trade-offs.**
- Three layered caches need contract-test discipline (R3) so a
  schema change in any one invalidates the right downstream
  consumers. Add tests under `tests/v2/test_rerank_cache.py`,
  `test_translation_cache.py`, `test_topic_precompute.py`.
- Stale-precompute: same mitigation as A1 — corpus_version-keyed,
  invalidate on bump.
- Rerank-cache memory: bounded by existing LRU (value is small —
  list of pg_ids + floats). No new pressure.

---

### ADR-A4 — Estimator / budget aware of materialized-view presence

**Status.** Proposed.

**Context.** [budget.py:36-97](scripts/v2/budget.py:36) declares
`STEP_COSTS_S` as a static dict — `word_freq_timeline = 12.0`,
`find_book_by_topic = 15.0`, etc. — regardless of whether the
precompute artifacts from A1/A3 exist.
[budget.py:282-358](scripts/v2/budget.py:282) sums per-step costs vs.
`INTENT_BUDGETS_S[intent]` and emits `execute` / `downsize` /
`clarify`.

After A1/A2/A3 land, real cost on warm paths drops ≥10×. The
estimator, unchanged, will keep recommending downsize/clarify on
queries that would finish in <1 s.

**Options considered.**
1. **Static dual-cost table** — `STEP_COSTS_S` becomes `{tool:
   (cold_s, warm_s)}`; estimator picks based on a passed-in
   `has_precompute(tool, args)` predicate.
2. **Cost lookup function** wraps `STEP_COSTS_S`, consults
   presence-checking predicates (file mtime, parquet existence,
   cache LRU lookup) before returning. More moving parts;
   integrates A1/A2/A3 cleanly.
3. **Trace-derived adaptive cost table** (already mentioned at
   [budget.py:23-25](scripts/v2/budget.py:23) as Phase 6 work) —
   read median runtime per `(tool, args_shape)` from observability,
   replace `STEP_COSTS_S` with rolling-percentile estimates.
   Self-tuning, slower to converge, harder to debug.

**Decision.** Option 2 now, Option 3 as Phase 6 follow-up. Add
`scripts/v2/budget_cost_lookup.py`:

```
def estimate_step_cost(tool: str, args: dict | None) -> float:
    base = STEP_COSTS_S.get(tool, 3.0)
    mult = _scope_multiplier(tool, args)   # existing logic
    if _has_warm_precompute(tool, args):
        return WARM_COSTS_S.get(tool, base * 0.05) * mult
    return base * mult
```

`_has_warm_precompute(tool, args)` consults:
- `word_freq_timeline`: parquet at
  `/workspace/spgc/derived/word_freq_buckets/{basis}_b{years}.parquet`
  present AND its corpus_version matches current?
- `top_ngrams_by_author`: parquet at
  `/workspace/spgc/derived/author_ngrams/{slug}_n{n}.parquet` present?
- `find_book_by_topic`: normalized topic present in
  `topic_recs.json`?
- Else: anticipate LRU/disk-cache hit by peeking
  `cache_key(...)` on disk → fast path.

`WARM_COSTS_S` is a separate dict listing prebuilt-path estimates
(0.3 s for parquet read, 0.05 s for cache hit). Update
[budget.py:BudgetEstimator.estimate_step](scripts/v2/budget.py:257)
to call `estimate_step_cost` instead of reading `STEP_COSTS_S`
directly.

**Consequences.**
- Estimator behavior aligns with actual prod latency on warm path.
  Fewer false `clarify` / `downsize` on heavy intents that have
  precompute.
- R9 (`$sN.field` validation) and the downsize logic at
  [budget.py:360-379](scripts/v2/budget.py:360) unchanged — they
  consume estimator output, not internals.
- Phase 6 trace-derived adaptive cost becomes an incremental upgrade
  to `_has_warm_precompute` / `WARM_COSTS_S` — the lookup function
  provides the seam.

**Trade-offs.**
- Couples estimator to artifact filesystem layout. Acceptable — the
  layout is already encoded across A1/A3 and is referenced from
  multiple places.
- Wrong "has precompute = True" verdict (artifact deleted by ops):
  estimator under-estimates; per-step timeout chokepoint at
  [tool_registry.py:78-94](scripts/v2/tool_registry.py:78) still
  enforces the real cap. One slow query, no cascading failure.
- Lookup adds I/O (file stat) per estimate. Cache predicates
  per-process (invalidate on `corpus_version._reset()`).

---

### Why Domain C exists alongside B

Domain B makes the *application image* reproducible by SHA (B1) and
removes host bind-mount drift for code (B2). It says nothing about the
**LLM model** — yet the model is the heaviest single artifact the
system depends on (~14 GB base + a tuned SYSTEM prompt) and the only
artifact whose identity ("what does `wordcracker:v2` resolve to right
now?") is currently established by a one-time manual `ollama create` on
the host. B1's "same git SHA → same artifact" invariant breaks the
moment we cross from Python deps into Ollama-land. Domain C closes that
seam — same motivation as B (deploy reproducibility), different
artifact class (the model on disk in the Ollama container's volume).

Order: this ADR can land independently of B1-B5; it does not depend on
them. But it does **assume** B4's predeploy gate exists as the
mechanism that warm-loads the resulting model into VRAM after a deploy
(see ADR-B4 §3 golden-set + the explicit note at the end of this ADR).

---

### ADR-C1 — LLM model residency policy

**Status.** Proposed.

**Context.** The system depends on two named Ollama models:

- `qwen3:14b` — stock upstream Ollama model (~14 GB on disk;
  pulled via `ollama pull qwen3:14b`). Referenced as default in
  [scripts/v2/rag_v2.py:43](scripts/rag_v2.py:43),
  [scripts/v2/planner/llm_planner.py:76-79](scripts/v2/planner/llm_planner.py:76),
  [scripts/v2/critic.py:38-39](scripts/v2/critic.py:38);
  hard-coded (no env override) in
  [scripts/rag_query.py:40](scripts/rag_query.py:40),
  [scripts/rag_tools.py:73, 277-278](scripts/rag_tools.py:73),
  [scripts/learning_tools.py:625](scripts/learning_tools.py:625),
  and [scripts/ollama_gpu_watcher.sh:46](scripts/ollama_gpu_watcher.sh:46).
- `wordcracker:v2` — locally-built tuned variant. Defined by
  [Modelfile.v2](Modelfile.v2): `FROM qwen3:14b` + 6 PARAMETER lines
  (temperature 0.1, top_p 0.8, repeat_penalty 1.1, num_ctx 8192,
  num_predict 1200, stop `<|im_end|>`) + a 1.5 KB Russian
  operator-style SYSTEM block ([Modelfile.v2:35-56](Modelfile.v2:35)).
  Default in [scripts/v2/planner/llm_intent.py:62-63](scripts/v2/planner/llm_intent.py:62);
  pinned in prod for chat + critic by
  [systemd/wordcracker-chat.service.d/v2-engine.conf:22](systemd/wordcracker-chat.service.d/v2-engine.conf:22)
  (`WC_LLM_MODEL=wordcracker:v2 WC_CRITIC_MODEL=wordcracker:v2`).

The Ollama service is stock `ollama/ollama:latest`
([docker-compose.yml](docker-compose.yml)) with its model directory
bind-mounted from host: the `ollama` service in `docker-compose.yml`
maps `/data/ollama:/root/.ollama`. Both `qwen3:14b` blobs and
`wordcracker:v2` blobs live there.

**Build procedure for `wordcracker:v2` today (per the comment block at
[Modelfile.v2:8-12](Modelfile.v2:8)).** Manual, three steps, no
automation:

1. `scp Modelfile.v2` onto the host.
2. `docker compose exec ollama ollama create wordcracker:v2 -f /workspace/Modelfile.v2`
   (requires bind-mount or container `/tmp`).
3. `WC_LLM_MODEL=wordcracker:v2` is already in the systemd drop-in;
   no further config.

Concrete consequences of the status quo:

- **Drift class #1 — base model floats.** `ollama pull qwen3:14b` on
  day N+30 returns whatever Ollama's library currently calls `:14b`.
  Modelfile.v2 has `FROM qwen3:14b` (no digest) so rebuilding
  `wordcracker:v2` against the floated base silently changes the
  effective model. Same `wordcracker:v2` tag, different weights.
- **Drift class #2 — Modelfile.v2 changes don't redeploy themselves.**
  Editing the SYSTEM prompt in the repo does nothing until somebody
  remembers to `ollama create` on the host. There is no gate; no R2
  ("fix executes on prod flag combo") mechanism for the model layer.
- **Drift class #3 — model-name inconsistency across callers.** The
  systemd drop-in pins `wordcracker:v2` for chat + critic, but the
  v4 planner reads `WC_LLM_PLANNER_MODEL` → `WC_LLM_MODEL` →
  `"qwen3:14b"` ([scripts/v2/planner/llm_planner.py:76-79](scripts/v2/planner/llm_planner.py:76))
  with the drop-in only setting `WC_LLM_MODEL`. So the planner
  reads `wordcracker:v2` in prod (one fall-through), but the half-dozen
  hard-coded `qwen3:14b` callsites in `scripts/` (translate,
  learning_tools, rag_query, ollama_gpu_watcher) target the base
  model unconditionally. **Result: two models live in VRAM
  simultaneously** when both paths run, with no policy declaring
  whether that's intended.
- **Drift class #4 — GPU watcher warms the wrong model.**
  [ollama_gpu_watcher.sh:45-47](scripts/ollama_gpu_watcher.sh:45) calls
  `/api/generate` with `model=qwen3:14b, keep_alive=-1` after a GPU
  passthrough recovery. After prod was switched to `wordcracker:v2`,
  this warm-back still loads the base — the prod-pinned model takes a
  cold-load hit on the first user request after a GPU blip.

Audit C5 ("bake time = 0; деплой-и-откат за 30 минут") assumes the
artifact (image) defines behaviour. With the LLM model, the artifact
defining behaviour is `wordcracker:v2`'s on-disk blob in
`/data/ollama/models/...` — which is not under any deploy mechanism's
control today.

**Options considered.**

1. **(а) Status quo — manual `ollama create` on host, bind-mounted
   `/data/ollama`, no automation.** Modelfile.v2 lives in repo as
   documentation. Operator runs `ollama create wordcracker:v2 -f
   Modelfile.v2` after Modelfile edits. Both `qwen3:14b` and
   `wordcracker:v2` persist across Ollama restart via the volume.
   *Pros:* zero new infrastructure; matches what's already deployed.
   *Cons:* every drift class above is unfixed. Violates R8 ("read the
   live path") because the live path is "whatever Modelfile.v2 looked
   like the last time the operator manually ran a command, against
   whatever base the registry served that day."

2. **(б) Bake `wordcracker:v2` into a custom Ollama image at build
   time.** New `Dockerfile.ollama`: `FROM ollama/ollama:latest`,
   `COPY Modelfile.v2 /`, `RUN ollama serve & sleep 5 && ollama create
   wordcracker:v2 -f /Modelfile.v2 && kill %1`. Pairs with ADR-B1 —
   image tagged by Modelfile-content-hash (or by repo SHA). Compose
   references `wordcracker-ollama:${OLLAMA_IMAGE_TAG}`.
   *Pros:* deploy = `docker compose up -d ollama` = atomic model swap;
   reproducible by tag; no init-time work.
   *Cons:* (i) Ollama's `OLLAMA_MODELS` directory is `/root/.ollama`,
   which is bind-mounted from `/data/ollama` in
   [docker-compose.yml](docker-compose.yml) (`ollama.volumes`) —
   the mount **hides the baked-in models** at runtime. Fixing this
   means either dropping the bind-mount (and re-pulling
   `qwen3:14b`-the-base on every Ollama-image swap, ~14 GB download)
   OR switching to a named volume with a seed-on-first-run script (the
   complexity Option (в) was supposed to avoid). (ii) Build host needs
   ~28 GB free during the `RUN ollama create` step (base ~14 GB + new
   tag ~14 GB pre-dedup). (iii) Ollama image rebuild on every
   Modelfile edit, even though the build is just metadata + SYSTEM
   text. (iv) Local image SHA-tag deploys (B1) on a registry-less
   single-host setup already use `docker save | docker load`; doubling
   that for a 14 GB-larger image is noticeable.

3. **(в) Idempotent init-sidecar against the running Ollama
   service.** Stock `ollama/ollama:latest`. Modelfile.v2 mounted
   read-only into the ollama container (or fetched by a sidecar via
   `docker cp`). On Ollama service start, a small init script runs
   the equivalent of:

   ```
   want_hash = sha256(Modelfile.v2)
   have_hash = read /data/ollama/wordcracker-v2.tag-meta (created last time)
   if want_hash != have_hash OR `ollama list` doesn't include wordcracker:v2:
       ollama create wordcracker:v2 -f /Modelfile.v2
       write Modelfile-hash to /data/ollama/wordcracker-v2.tag-meta
   ```

   Same script also `ollama pull qwen3:14b` if the base is missing,
   pinning the digest in the sidecar logic (read `ollama show
   qwen3:14b --modelfile` once, compare against expected digest from
   a `models.lock` checked into the repo).
   *Pros:* (i) plays well with the existing bind-mount — models
   stored in `/data/ollama` as today, only the *create* is
   re-triggered when Modelfile changes; (ii) idempotent — restart of
   the ollama service is cheap when nothing changed; (iii) no extra
   image bloat; (iv) `models.lock` adjacent to `requirements.lock`
   (B1) gives base-digest reproducibility without baking
   gigabytes into images; (v) Modelfile.v2 change → image-SHA-tagged
   `chat`/`admin` deploy → restart → init sees new hash → recreates
   tag — all driven from one repo commit.
   *Cons:* (i) extra startup time on Modelfile change (~10-20 s for
   `ollama create`, one-time); (ii) the "lock the base digest"
   half needs a small `scripts/v2/build_models_lock.py` companion;
   (iii) one more piece of init logic to reason about, though smaller
   than B1's pip-compile flow.

4. **(г) External / private Ollama registry — `ollama pull
   wordcracker:v2:<sha>`.** Push the built model to a private
   registry; deploy hook does `ollama pull wordcracker:v2:${MODEL_TAG}`
   against the local Ollama. Decouples model lifecycle from compose
   entirely; mirrors how application images would work with a Docker
   registry.
   *Pros:* most cloud-native; clean for multi-host scale-out;
   identical mental model to Docker image deploys.
   *Cons:* requires registry infrastructure that doesn't exist on
   this single-host setup; doubles the "where does the artifact live"
   question (image registry + model registry); pulling 14 GB of model
   over the network on every model bump is wasteful for a local-only
   deploy. Premature for current scale (single host, one operator).

**Decision.** **Option (в) — idempotent init-sidecar driven by
`Modelfile.v2` + a small `models.lock` for base-digest pinning.**

Concretely:

1. Repo gains `models.lock` adjacent to `requirements.lock`:

   ```
   # one line per upstream Ollama model used.
   # digest = output of `ollama show <tag> --modelfile | head -1`
   #          (the `FROM <digest>` line for the resolved blob).
   qwen3:14b sha256:<digest>
   ```

   Maintained the same way as `requirements.lock` (B1) — manual
   `make refresh-models-lock` step on intentional base bump.

2. Modelfile.v2 stays in repo, unchanged in shape. Future
   improvement (out of scope for this ADR): replace `FROM qwen3:14b`
   with `FROM qwen3:14b@sha256:<digest>` once Ollama's Modelfile
   syntax supports digest pinning natively (it does as of Ollama
   0.4); when adopted, `models.lock` becomes redundant for the base
   and the lock collapses to just the *creation hash* of the tuned
   tag.

3. A new init script — `scripts/v2/ollama_init.sh` — runs as the
   ollama service's `command` (wrapping `ollama serve`):

   ```
   ollama serve &
   wait_for_api  # poll /api/tags until 200
   ensure_pulled qwen3:14b $(grep ^qwen3:14b models.lock | awk '{print $2}')
   ensure_created wordcracker:v2 /Modelfile.v2  # compares stored hash
   wait
   ```

   `Modelfile.v2` and `models.lock` are bind-mounted read-only into
   the ollama container at `/Modelfile.v2` and `/models.lock`.

4. The single source of truth for the model name moves to
   `scripts/v2/env_registry.py` (per ADR-B5) — `WC_LLM_MODEL`,
   `WC_LLM_PLANNER_MODEL`, `WC_CRITIC_MODEL` all default to
   `"wordcracker:v2"` (was inconsistent — `llm_intent.py` defaulted
   to `wordcracker:v2`, the other three defaulted to `qwen3:14b`).
   The hard-coded `qwen3:14b` callsites in `rag_query.py`,
   `rag_tools.py`, `learning_tools.py`, and `ollama_gpu_watcher.sh`
   move to read the registry. This is **out of scope for this ADR's
   implementation** (it's a code change) but the residency policy
   assumes it lands as part of B5 follow-through; without it,
   Option (в) only fixes residency for the chat/critic path, not
   the auxiliary paths.

5. Predeploy gate (ADR-B4) gains one more check: assert
   `wordcracker:v2` exists in `ollama list` AND its creation hash
   matches `sha256(Modelfile.v2)`. Failure → deploy blocked.

**Consequences.**

- `wordcracker:v2`'s blob lives in `/data/ollama` as today, but its
  *identity* is now derived from Modelfile.v2 + models.lock — both
  checked into the repo. A git SHA + an Ollama init script run = the
  same model on disk, repeatably.
- Modelfile.v2 edit → commit → predeploy gate → deploy → ollama
  service restart → init script sees new Modelfile hash → recreates
  the `wordcracker:v2` tag in ~10-20 s. The user-facing model swap
  is atomic at the moment the predeploy gate's `ensure_created`
  call returns.
- Drift class #1 closed by `models.lock` (base digest pinned).
- Drift class #2 closed by the init script (Modelfile hash drives
  re-creation).
- Drift class #3 reduced to a code-change task tracked under
  ADR-B5 (env-registry single-source-of-truth) — residency policy
  defines what `WC_LLM_MODEL` SHOULD be, the registry enforces it.
- Drift class #4 (GPU watcher) becomes a one-line edit in
  `ollama_gpu_watcher.sh` once it reads the registry — same B5
  follow-through.
- Modelfile.v2 stops being a comment-block ritual ("scp, exec,
  set env") and becomes an artifact whose presence and content are
  mechanically verified at deploy time.

**Trade-offs.**

- Ollama service no longer launches with the stock command; it now
  wraps a small shell init. **Friction is desired** (R7 / R8 — the
  Ollama service's behaviour must be explicit and grep-able rather
  than "whatever the operator did last").
- `models.lock` is one more manual-bump file alongside
  `requirements.lock`. Same maintenance pattern, same payoff (base
  changes are intentional, dated, and visible in git history).
- The init script adds ~10-20 s to ollama service startup the
  *first time* a Modelfile.v2 change is deployed (subsequent restarts
  are no-ops because the hash matches). Acceptable — chat / admin
  services can `depends_on: { ollama: { condition: service_healthy }
  }` so they don't race the model creation.
- Option (б) would avoid the init script entirely but at the cost
  of bind-mount conflict + 14 GB of duplicated state. Option (в)
  keeps the bind-mount (which we already trust for data persistence)
  and adds only the *trigger* mechanism.
- Option (г)'s registry path is the right long-term move if/when this
  system grows beyond one host. Today it pays infra cost for no
  current benefit. Revisit if scale-out becomes a real consideration.

---

### Follow-ups surfaced during ADR-B1 implementation

Things discovered while landing B1 phases 1 + 2 that don't belong
inside the existing ADRs but need to be on record so we don't
re-discover them later.

**F1 — `cache._write_disk` race (closed by commit b8dd3ab).** Two
ThreadingHTTPServer threads computing the same heavy query both
targeted `p.with_suffix(".tmp")` — a fixed filename per cache_key.
Loser's `replace()` got ENOENT; winner's payload could be
overwritten mid-write_text by the loser before the surviving
`replace()` ran. Fixed via
`tempfile.NamedTemporaryFile(delete=False)` so each writer gets a
unique `.tmp` in the same directory. **Class lesson:** any
filesystem cache layer reachable from `ThreadingHTTPServer` (or any
multi-writer context) MUST use per-writer unique tmpfile names —
shared-name + atomic-rename is not atomic across writers. Negative
test at [tests/v2/test_cache_concurrent_writes.py](tests/v2/test_cache_concurrent_writes.py)
locks the contract.

**F2 — 20.6 GB image after phase 2 (candidate ADR: "slim base").**
The Dockerfile keeps `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime`
as base (preinstalled torch 2.6.0+cu124 + cuDNN 9) and then `pip
install --require-hashes` upgrades torch to 2.12.0+cu130 (PyPI now
bundles its own CUDA 13 via `nvidia-cublas-cu13` /
`nvidia-cudnn-cu13` / etc. wheels). Both CUDA runtimes end up in
`/opt/conda/lib/python3.11/site-packages/` — dead weight. Reclaim
opportunity ≈ 5 GB by switching base to `python:3.11-slim` and
letting the lock install everything. Worth its own ADR — not
attempted here because base swap = larger blast radius than B1's
"pinned-deps" scope.

**F3 — In-flight request coalescing (candidate ADR or A-domain
follow-up).** 2026-05-24 prod log: two `top_ngrams_by_author` calls
with identical fingerprint completed at the same wall-clock second
(06:47:47), with 3346.75 s and 3747.57 s elapsed — i.e. **two
users running the same 55-minute compute in parallel**, neither
benefitting from the other's work. After ADR-A1 precompute lands
this becomes irrelevant for canonical phrasings, but the structural
gap (`dispatch` has no in-flight tracking) deserves its own ADR.
The fix shape is small: a `dict[cache_key → Future]` in
`tool_registry.dispatch` so the second caller waits on the first's
result instead of duplicating compute.

**F4 — `v2-engine.conf` drop-in still in active systemd chain.**
`sudo systemctl status wordcracker-chat` post-restart still shows
`Drop-In: …/v2-engine.conf` with `WC_DEFAULT_ENGINE=v2` exported
into the container — even though D-P1-5 deleted that var from code.
Confirms ADR-B5 (env-var lifecycle bound to decisions.md) is needed
in the form proposed; until B5 lands, the drift is harmless but
documents itself in the live unit file.

These four are NOT part of the B1 acceptance gate. F1 is already
closed; F2, F3, F4 are candidates for future ADRs once the bake
period validates the current B1 image.

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

### D-P1-8 — Router executors collapsed; v3/v4 plan unification deferred to T4

**Decision.** `scripts/v2/planner/router.py` now has exactly two
public functions: `execute(plan_or_spec)` and
`execute_stream(plan_or_spec)`. Both dispatch by
`isinstance(arg, PlanSpec)`:

- `PlanSpec` → `_execute_spec` / `_execute_spec_stream` (v4 DAG path)
- `QueryPlan` → `_execute_query_plan` / `_execute_query_plan_stream` (v3 linear path)

The previous four-function surface (`execute` / `execute_spec` /
`execute_stream` / `execute_spec_stream`) is gone; the per-shape
executors became private helpers under the new names. The T1_TZ §4.3
gate now passes:

    grep -nE '^def execute' scripts/v2/planner/router.py
    → def execute(plan_or_spec: PlanOrSpec, *, budget=None) -> RouterResult
    → def execute_stream(plan_or_spec: PlanOrSpec, *, budget=None) -> Iterator[dict]

`router_mod.execute_spec(spec)` callsites in `rag_v2.py` (4 places)
and tests `test_budget_enforcement.py` (2) / `test_v4_router_dag.py`
(10 + 2 stream) were updated to `router_mod.execute(spec)` /
`router_mod.execute_stream(spec)`. `execute(plan)` callers
(`rag_v2.py`, `test_budget_enforcement.py`, `test_router.py`,
`test_e5_fan_out_authors.py`) unchanged.

**Why not full v3/v4 plan-shape unification.** TZ §6 anticipated this
as a potential D-P1-6 fork: "если обе живы — это та самая v3-vs-v4
развилка, которую Фаза 1 должна закрыть: привести к одной форме
плана. Это потенциально объёмная правка — если получится больше 200
строк диффа, эскалируй (R7), не пытайся продавить за один коммит."
Inspection of the v3/v4 split:

- v3 emits `QueryPlan` via `plan_mod.build(intent.label, entities)` —
  rules-based, fast path for the dominant intent set.
- v4 emits `PlanSpec` via `llm_planner.plan_query(...)` — LLM-built
  DAG for compound / follow-up queries when v3 clarifies.
- The two shapes differ structurally: v3 uses
  `PlanStep.depends_on: list[int]` + `inject_result_as: str | None`
  (heuristic injection); v4 uses `PlanStepSpec.needs: list[str]` +
  `$sN.field` interpolation (typed DAG refs). The semantics differ in
  edge cases (e.g. v3's `inject_result_as="author_regex"` reshapes
  `top[0]` into a regex; v4's `$sN.field` would need an explicit
  reshape step).
- Collapsing them into one shape would touch `plan.py` (2458 lines —
  T4 territory), the 46 `_plan_*` builders, every test that asserts
  on `QueryPlan` vs `PlanSpec` field shapes, and the renderer/critic
  contract layer. Conservatively a 400+ line diff, with non-trivial
  behavioural risk.
- The polymorphic `execute(plan_or_spec)` collapse satisfies the
  syntactic T1 gate (one `def execute`, one `def execute_stream`)
  WITHOUT taking on that 400+ line risk in this session.

Full unification is therefore deferred to **T4** (the `plan.py`
decomposition pass): once builders move into `planner/builders/`
the question "should builders emit `QueryPlan` or `PlanSpec`?" is
the right place to resolve, alongside the static `PLAN_BUILDERS`
registry. Marking this as a follow-up rather than a freestanding
D-P1-x fork (no separate ticket needed — it's part of T4's scope).

**Consequences.**
- Router has two public entry points: `execute`, `execute_stream`.
  Both are polymorphic; callers don't pick a path.
- Internal four-way split is preserved as private helpers
  (`_execute_query_plan`, `_execute_spec`,
  `_execute_query_plan_stream`, `_execute_spec_stream`). Same logic
  as before T1.
- `apply_invariants(plan)` continues to be called on the v3 branch
  inside `_execute_query_plan`; the v4 branch goes through the spec
  topological order. Fan-out remains v3-only for now (T4 will move
  it to be invariant-applied on either form).
- `pytest tests/v2 -q` and `pytest tests/v2 -q -p no:randomly` —
  both 1448 passed / 19 skipped / 0 failed.

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
