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
            json.dump(raw, fh, default=_serialize_safely,
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
        from scripts.v2.contracts.live_args import LIVE_ARGS
        from scripts.v2.contracts.registry import V1_CONTRACTS
    except Exception as e:
        print(f"[record_fixtures] FATAL: could not load v2 contracts "
              f"layer: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.list:
        for key, payload in sorted(LIVE_ARGS.items()):
            print(f"{key}\t{payload}")
        return 0

    targets = sorted(args.only) if args.only else sorted(LIVE_ARGS)

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
        "results": [],
    }

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
    sys.exit(main(sys.argv[1:]))
