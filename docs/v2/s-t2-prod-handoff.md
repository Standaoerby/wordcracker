# S-T2 prod handoff runbook

The Tier-2 contract fixes (branch `s-t2-tier2-contracts`, PR into `main`) are
code-complete and pass the local corpus-free suite (2112 passed). Three steps
remain that need the **prod host** (corpus + Linux/CUDA container + the live
`user_uploads_metadata.csv`). Run them in order. Deploy-free — deploy is a
separate command.

Why these can't run in CI / locally: the SPGC corpus (~35 GB), ChromaDB, a
warm Ollama, and the user-uploads CSV live only on prod; `record_fixtures`
and the full v2 suite depend on them.

---

## 0. Why CI on the PR is (expected) red until step 1

- **`FixtureFreshnessGate`** — Group B edited v1 functions in `rag_tools.py`
  (the 9 lang-filter sites, incl. `_select_books`, a depth-1 callee of many
  wrappers). The fixture fingerprints stamped at the last recording no longer
  match the current source → the gate correctly flags the fixtures stale.
  Step 1 refreshes them.
- **`version-bump`** — handled on the branch (`ANALYTICS_VERSION` 2.6.20 →
  2.6.21). No action needed unless `main` moves first.

---

## 1. Re-record golden fixtures (refresh stale fingerprints)

```bash
git fetch origin && git checkout s-t2-tier2-contracts && git pull
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm gutenberg-lab \
    python -m scripts.v2.contracts.record_fixtures
cat scripts/v2/contracts/fixtures/_manifest.json   # expect 31 ok / 0 failed / enrich_word exempt
git add scripts/v2/contracts/fixtures/
git commit -m "chore(s-t2): re-record fixtures after v1 lang-filter edits"
git push origin s-t2-tier2-contracts
```

Notes:
- `enrich_word` stays **exempt** (LLM/qwen3 non-deterministic) — no Ollama
  warmup needed for the sweep.
- If any binding reports `v1_returned_error` / `v1_raised`, fix its
  `LIVE_ARGS` entry (wrong PG id / zero-match regex) — do **not** commit an
  empty/error fixture.

## 2. Full v2 suite + 12-probe (the part CI can't do)

```bash
# Full suite on the prod container (mirrors predeploy tests/v2, with corpus)
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm gutenberg-lab python -m pytest tests/v2 -q
# Target: >= 1460 passed, 0 failed (FixtureFreshnessGate green after step 1).

# Live 12-probe (needs the chat API up)
bash scripts/predeploy_gate.sh        # or the project's probe entrypoint
# Expect: 12/12 probes pass, no regressions vs the pre-S-T2 baseline.
```

## 3. Repair the corrupted user-uploads CSV (one-shot migration)

```bash
# Dry-run first — review the planned ['eng']->['en'] etc. changes
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm gutenberg-lab \
    python -m scripts.migrations.migrate_user_meta_lang
# Apply (keeps a .bak alongside the CSV)
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm gutenberg-lab \
    python -m scripts.migrations.migrate_user_meta_lang --apply
```

The CSV (`/workspace/spgc/derived/user_uploads_metadata.csv`) is **not** in
the repo, so its diff is reviewed on the host (compare against the `.bak`),
not via `git diff`.

---

## 4. Merge checklist

- [ ] Step 1 pushed → PR `FixtureFreshnessGate` green
- [ ] `version-bump` green (branch at 2.6.21 > main 2.6.20)
- [ ] `tests/v2` green on CI (Py 3.11)
- [ ] Step 2 full suite ≥ 1460 + 12/12 probes (prod)
- [ ] Step 3 migration applied + reviewed
- [ ] Squash-merge into `main`. **No deploy** (separate command).
- [ ] If the predeploy on `main` is red on anything unexpected: `git revert`
      the merge SHA and push (deploy-free), do not patch forward under fire.
```
