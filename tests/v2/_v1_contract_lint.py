"""Standalone v1↔v2 contract lint — TZ S-F2 / ADR-F2 D-SF2-3.

Iterates `scripts.v2.contracts.registry.V1_CONTRACTS` and applies
`check_wrapper_against_schema` to each binding. Returns a dict of
offenders (`wrapper_qualname -> [phantom_keys]`). Importable from
test code; runnable as a CLI:

    python tests/v2/_v1_contract_lint.py            # exits 0 / 1
    python tests/v2/_v1_contract_lint.py --verbose  # prints every binding

The underscore prefix keeps pytest from auto-collecting this file as
a test module; it is a shared helper. The production logic lives in
`scripts/v2/contracts/__init__.py` (`check_wrapper_against_schema`) —
this module is the orchestrator + CLI surface that the TZ S-F2
acceptance gate names explicitly.

Two callers:

  * `tests/v2/test_v1_contracts.py::StaticContractGate` — passes the
    offenders dict through `unittest.TestCase.assertEqual({}, ...)`.
  * Deploy host / developer terminal — runs the CLI directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Same sys.path hack as every other tests/v2 module — the test
# harness runs from repo root with pytest, but the CLI is invoked
# bare from anywhere.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def scan_all_contracts() -> dict[str, list[str]]:
    """Return `{wrapper_qualname: [phantom_keys]}` for every binding
    whose wrapper reads a key not in its schema.

    Empty dict on success. Importing this triggers wrapper module
    loads (via `scripts.v2.tools`), which is what populates
    `V1_CONTRACTS` — so calling code does not need to pre-import the
    tools package.
    """
    # Importing tools populates V1_CONTRACTS as a side effect of each
    # wrapper module's `@v1_contract` decorator firing.
    import scripts.v2.tools  # noqa: F401
    from scripts.v2.contracts import check_wrapper_against_schema
    from scripts.v2.contracts.registry import V1_CONTRACTS

    offenders: dict[str, list[str]] = {}
    for key, binding in V1_CONTRACTS.items():
        rogue = check_wrapper_against_schema(
            binding.wrapper_fn, binding.schema,
        )
        if rogue:
            offenders[key] = rogue
    return offenders


def _main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv
    offenders = scan_all_contracts()

    if verbose:
        from scripts.v2.contracts.registry import V1_CONTRACTS
        print(f"[v1_contract_lint] scanned {len(V1_CONTRACTS)} bindings")

    if offenders:
        print("[v1_contract_lint] FAIL — wrappers reading phantom keys:",
              file=sys.stderr)
        for qualname, rogue in sorted(offenders.items()):
            print(f"  {qualname}: {rogue}", file=sys.stderr)
        print(
            "\nFix: either add the key to the wrapper's schema in "
            "scripts/v2/contracts/schemas.py (if v1 actually returns it) "
            "or remove the read from the wrapper (drop the `.get(...) or "
            ".get(...)` fallback chain — Phase 2 R3).",
            file=sys.stderr,
        )
        return 1

    print("[v1_contract_lint] OK")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
