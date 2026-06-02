"""Record golden v1-output fixtures for the CI contract-replay gate.

Runs on the **prod host** (or any environment with the full corpus
mounted at `/workspace/spgc/` + ChromaDB at `/workspace/chroma_db/`).
For each entry in
[`live_args.LIVE_ARGS`](scripts/v2/contracts/live_args.py), invokes
the real v1 function and writes its output to
`scripts/v2/contracts/fixtures/<v1_qualname>.json`. The CI-side
[`tests/v2/test_v1_contracts.py`](tests/v2/test_v1_contracts.py)
then validates those fixtures against the declared schemas — that
is the load-bearing v1↔v2 contract gate (TZ S-F2 / ADR-F2).

Usage on prod:

    # SSH to host, working dir = repo root
    docker compose exec gutenberg-lab \
        python -m scripts.v2.contracts.record_fixtures

    # Commit the resulting JSON files
    git add scripts/v2/contracts/fixtures/*.json
    git commit -m "chore(s-f2): record v1 golden fixtures"

The recorder writes a JSON file per binding in LIVE_ARGS, plus one
`_manifest.json` that lists which bindings recorded successfully
and which raised. The manifest tracks coverage so a later
`record_fixtures --diff` can highlight which fixtures are missing.

Exit codes:

    0 — every LIVE_ARGS binding recorded successfully
    1 — at least one binding raised; recorded fixtures still written,
        but operator must investigate before merging (one of two
        things changed: v1 signature drifted, or the LIVE_ARGS entry
        was tuned for state the corpus no longer has)
    2 — fixtures dir not writable, or LIVE_ARGS / V1_CONTRACTS could
        not be loaded (catastrophic)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("wordcracker.v2.contracts.record_fixtures")

# Where the per-binding fixture JSONs land. Each filename mirrors the
# binding's v1_qualname for traceability.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _serialize_safely(obj: object) -> object:
    """Best-effort JSON-coercion for v1 outputs that contain Path,
    set, frozenset, numpy scalars, etc.

    The replay test loads back via `json.load(...)` and validates
    only the *shape* (keys present, row_keys present in list-of-dict
    items) — exact value fidelity is not required. So we collapse
    non-JSON types to their string repr rather than failing the dump.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    # numpy scalars expose .item(); duck-type without import.
    if hasattr(obj, "item") and callable(obj.item):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)


# S-P2c (#2): keys carrying per-call wall-clock that make the fixture BODY
# non-deterministic — every re-record churns them, drowning real drift in noise
# (and tripping the deploy F2-RERECORD diff gate on a timing wobble, not a
# contract change). The real timing is preserved in _manifest.json (the
# recorder's own `elapsed_s` per result, written below). _elapsed_s is OPTIONAL
# in every schema (V1Schema is total=False; not in any __required__) and no
# wrapper reads it, so dropping it from the body keeps RecordedFixtureReplay
# green with zero schema / return-shape / fingerprint change.
_VOLATILE_BODY_KEYS = ("_elapsed_s",)


def _strip_volatile_body(raw: object) -> object:
    """Return `raw` without the volatile timing keys at the top level.

    Only the top level is stripped — that is where v1 tools attach
    `_elapsed_s` (mirrors the schema TypedDicts, which declare it at the
    top level). Non-dict outputs (list-returning tools) pass through.
    """
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if k not in _VOLATILE_BODY_KEYS}
    return raw


def _record_one(v1_qualname: str, args: dict, fixtures_dir: Path) -> dict:
    """Record one binding. Returns a manifest entry summarising the
    outcome. Writes the fixture JSON (success branch) or skips the
    write and records the error (failure branch)."""
    from scripts.v2.contracts.registry import V1_CONTRACTS

    binding = None
    for b in V1_CONTRACTS.values():
        if b.v1_qualname == v1_qualname:
            binding = b
            break
    if binding is None:
        return {
            "v1_qualname": v1_qualname,
            "status": "no_binding",
            "error": f"no @v1_contract registered with v1_fn={v1_qualname!r}",
        }

    try:
        fn = binding.resolved_v1_fn()
    except Exception as e:  # ImportError, AttributeError, etc.
        return {
            "v1_qualname": v1_qualname,
            "status": "v1_unresolvable",
            "error": f"{type(e).__name__}: {e}",
        }

    t0 = time.perf_counter()
    try:
        raw = fn(**args)
    except Exception as e:
        return {
            "v1_qualname": v1_qualname,
            "status": "v1_raised",
            "error": f"{type(e).__name__}: {e}",
            "args": args,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }

    elapsed = round(time.perf_counter() - t0, 3)

    # v1 has a uniform error-branch convention — a dict with an
    # `"error"` key + `"details"` is the failure shape. These are NOT
    # valid contract fixtures: the schema replay validator treats
    # error-dicts as a no-op (allow_error=True), so writing them
    # would let CI pass on a no-data recording. Surface as
    # `v1_returned_error` so the operator can fix LIVE_ARGS (wrong
    # PG id, regex with zero matches, etc.) instead of accidentally
    # committing an empty contract.
    if isinstance(raw, dict) and "error" in raw:
        return {
            "v1_qualname": v1_qualname,
            "status": "v1_returned_error",
            "error": str(raw.get("error")),
            "details": raw.get("details"),
            "args": args,
            "elapsed_s": elapsed,
        }

    out_path = fixtures_dir / f"{v1_qualname}.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(_strip_volatile_body(raw), fh, default=_serialize_safely,
                      indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        return {
            "v1_qualname": v1_qualname,
            "status": "write_failed",
            "error": f"{type(e).__name__}: {e}",
            "path": str(out_path),
        }

    # D-SF2-6 — stale-fixture detection: stamp the AST-fingerprint of
    # (wrapper, v1_fn) at recording time. Same formula as ADR-F1's
    # cache fingerprint, so cache invalidation and fixture staleness
    # share one definition (if the cache key flips, the fixture is
    # stale by the same logic). At CI time
    # `FixtureFreshnessGate` recomputes the fingerprint from the
    # current source tree and asserts it matches the stamped value —
    # drift means v1 or wrapper was edited since the recording, the
    # fixture is stale, operator must re-run on prod.
    try:
        from scripts.v2.contracts.registry import ast_fingerprint
        fingerprint = ast_fingerprint(binding.wrapper_fn, binding.v1_fn)
    except Exception as e:
        fingerprint = f"fingerprint_failed:{type(e).__name__}:{e}"

    return {
        "v1_qualname": v1_qualname,
        "status": "ok",
        "path": str(out_path.relative_to(fixtures_dir.parent.parent.parent)),
        "elapsed_s": elapsed,
        "size_bytes": out_path.stat().st_size,
        "v1_fingerprint": fingerprint,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="record_fixtures",
        description="Record real v1 outputs into golden fixtures for CI replay.",
    )
    parser.add_argument(
        "--only",
        action="append",
        help="record only this v1_qualname (may be repeated). "
             "Default: record every entry in LIVE_ARGS.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the LIVE_ARGS table and exit; no v1 calls.",
    )
    parser.add_argument(
        "--skip-heavy",
        action="store_true",
        help="skip the full-corpus-scan bindings in HEAVY_BINDINGS "
             "(word_contexts_global, word_freq_timeline, "
             "find_words_by_etymology). Used by the deploy-time re-record "
             "gate (scripts/deploy.sh) so a multi-minute scan cannot hang "
             "a deploy. An explicit `--only <heavy-name>` still force-records "
             "a heavy binding even with this flag.",
    )
    args = parser.parse_args(argv)

    # Repo-root path resolution so the script can be called as
    # `python -m scripts.v2.contracts.record_fixtures` or directly.
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Importing scripts.v2.tools fires every @v1_contract decoration,
    # populating V1_CONTRACTS. Without this the recorder sees an
    # empty registry and reports every binding as no_binding.
    try:
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.contracts.live_args import (
            LIVE_ARGS, FIXTURE_EXEMPT, HEAVY_BINDINGS,
        )
        from scripts.v2.contracts.registry import V1_CONTRACTS
    except Exception as e:
        print(f"[record_fixtures] FATAL: could not load v2 contracts "
              f"layer: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.list:
        for key, payload in sorted(LIVE_ARGS.items()):
            print(f"{key}\t{payload}")
        return 0

    # FIXTURE_EXEMPT bindings (e.g. LLM-generative enrich_word) are skipped
    # in the default sweep — their output is non-deterministic so a frozen
    # fixture is meaningless / false-fails replay. An explicit `--only
    # <qualname>` still force-records them (for a one-off shape eyeball).
    # --skip-heavy (S-B7 F2-DEPLOY-RERECORD): drop the full-corpus-scan
    # bindings so the deploy-time gate cannot hang on a multi-minute scan.
    # An explicit `--only <name>` always wins — force-recording a heavy
    # binding by name ignores --skip-heavy (one-off shape eyeball / manual
    # full re-record of a single tool).
    if args.only:
        targets = sorted(args.only)
        skipped_exempt = []
        skipped_heavy = []
    else:
        skip_set = set(FIXTURE_EXEMPT)
        if args.skip_heavy:
            skip_set |= set(HEAVY_BINDINGS)
        targets = [k for k in sorted(LIVE_ARGS) if k not in skip_set]
        skipped_exempt = sorted(k for k in LIVE_ARGS if k in FIXTURE_EXEMPT)
        skipped_heavy = (
            sorted(k for k in LIVE_ARGS if k in HEAVY_BINDINGS)
            if args.skip_heavy else []
        )

    try:
        _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[record_fixtures] FATAL: fixtures dir not writable: "
              f"{_FIXTURES_DIR} ({e})", file=sys.stderr)
        return 2

    manifest = {
        "v1_contracts_loaded": len(V1_CONTRACTS),
        "live_args_total": len(LIVE_ARGS),
        "targets_attempted": len(targets),
        "exempt": sorted(FIXTURE_EXEMPT),
        "skipped_heavy": skipped_heavy,
        # S-B7 FINGERPRINT-PYVER (D-SB7): the per-fixture v1_fingerprint is
        # SHA-256 of `ast.dump(ast.parse(source))`, whose output is
        # Python-minor-specific (AST node fields evolve across minors). A
        # fixture recorded on 3.11 then checked on 3.13 reports every
        # binding as "source changed" — a false positive. Stamp the
        # recording interpreter's minor so FixtureFreshnessGate can fail
        # loud with "Python minor mismatch" instead of 31 misleading
        # source-drift lines. (Does NOT change the fingerprint formula or
        # the F1 cache key — purely an accounting field.)
        "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "results": [],
    }

    for v1_qualname in skipped_exempt:
        manifest["results"].append({
            "v1_qualname": v1_qualname,
            "status": "exempt",
            "reason": "fixture-coverage waived (FIXTURE_EXEMPT): "
                      "LLM-generative / non-deterministic output",
        })
        print(f"[record_fixtures] SKIP {v1_qualname}: exempt "
              f"(FIXTURE_EXEMPT — non-deterministic)")

    # Heavy bindings skipped under --skip-heavy (deploy gate). Logged
    # loudly so the dropped coverage is never silent (S-B7): a deploy
    # operator reading the gate output sees exactly which contracts the
    # JSON-diff gate did NOT re-check. Their fixtures on disk are left
    # untouched — so they cannot trigger a false drift signal — and the
    # PR-time FixtureFreshnessGate still fingerprints them.
    for v1_qualname in skipped_heavy:
        manifest["results"].append({
            "v1_qualname": v1_qualname,
            "status": "skipped_heavy",
            "reason": "full-corpus scan skipped (--skip-heavy, HEAVY_BINDINGS): "
                      "fixture left untouched; PR-time fingerprint still applies",
        })
        print(f"[record_fixtures] SKIP {v1_qualname}: heavy "
              f"(--skip-heavy — full-corpus scan; fixture left untouched)")

    ok = 0
    failed = 0
    for v1_qualname in targets:
        sample_args = LIVE_ARGS.get(v1_qualname)
        if sample_args is None:
            entry = {
                "v1_qualname": v1_qualname,
                "status": "no_live_args",
                "error": "no entry in LIVE_ARGS",
            }
        else:
            entry = _record_one(v1_qualname, sample_args, _FIXTURES_DIR)

        manifest["results"].append(entry)
        if entry["status"] == "ok":
            ok += 1
            print(f"[record_fixtures] OK  {v1_qualname} "
                  f"({entry['elapsed_s']}s, {entry['size_bytes']}B)")
        else:
            failed += 1
            print(f"[record_fixtures] ERR {v1_qualname}: "
                  f"{entry['status']} — {entry.get('error', '')}",
                  file=sys.stderr)

    manifest_path = _FIXTURES_DIR / "_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)

    print(f"\n[record_fixtures] DONE: {ok} ok, {failed} failed, "
          f"manifest at {manifest_path.relative_to(repo_root)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    rc = main(sys.argv[1:])
    # Hard-exit, do NOT fall through to a normal interpreter shutdown.
    #
    # The recorded v1 tools instantiate a process-singleton
    # `chromadb.PersistentClient` (rag_tools._get_chroma_collection_with_embedder)
    # whose default-on Posthog telemetry spawns a NON-daemon background
    # `Consumer` thread, plus a cuda-bound SentenceTransformer that holds a
    # native torch/CUDA context. Neither is released by `sys.exit`, so the
    # interpreter blocks at shutdown trying to join the lingering thread —
    # observed on the live 2.6.33 deploy as "DONE: 28 ok, 0 failed" followed
    # by a hang until the gate's `timeout 600` killed it (rc=124). All work
    # is finished and the manifest is already written by the time `main`
    # returns, so we flush our own output and exit the process directly,
    # skipping the wedged third-party threads entirely.
    #
    # `os._exit` is the robust fix here precisely because the root cause is
    # third-party (telemetry / native) threads we don't own and can't
    # reliably join. It touches NO v1 source, so the AST fingerprints stamped
    # into the fixtures are unchanged — this needs no fixture re-record.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
