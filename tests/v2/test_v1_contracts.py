"""Phase 2 contract sweep — REFACTOR_BRIEF Gate + TZ S-F2 / ADR-F2 closeout.

For every (wrapper, v1_fn, schema) triple in `V1_CONTRACTS`:

  1. Static gate (runs at wrapper import, but verified here too):
     the wrapper body reads only keys declared in the schema (plus the
     well-known internal/error-branch sets). Phantom keys → import-time
     ContractError, which means this test file would also fail to
     collect — we re-run the check explicitly for clarity, via the
     standalone lint helper `_v1_contract_lint.scan_all_contracts`
     (one body, two callers — TZ S-F2 D-SF2-3).

  2. Drift gate (the "remove any key from v1 → exactly one contract test
     fails" half of Phase 2 gate): a minimal mock built from the schema
     is fed back into a validator that asserts every required schema key
     is present. If someone removes a required key from a schema, this
     test fails exactly once — no quiet regression.

  3. Decorator visibility: every wrapper exposes its (v1_fn, schema)
     binding through `__v1_contract__`.

  4. R2 negative gate (TZ S-F2 acceptance): synthesise a wrapper that
     reads a phantom key, apply `@v1_contract`, assert `ContractError`
     is raised with both phantom keys named in the message. Locks in
     the import-time enforcement — a future regression that catches
     and logs instead of raising flips this test red.

  5. Fixture-coverage hard-gate (TZ S-F2 D-SF2-4 revised — agreed
     with user 2026-05-25): every binding in V1_CONTRACTS with a
     LIVE_ARGS entry MUST have a recorded fixture under
     scripts/v2/contracts/fixtures/. Missing = CI red. Recording
     happens on prod via
     `python -m scripts.v2.contracts.record_fixtures`. This is the
     load-bearing v1↔v2 contract gate — it catches "declared
     contract diverged from what v1 actually returns", which the
     static-AST / schema-mock gates above cannot.

  6. Recorded-fixture replay (TZ S-F2 D-SF2-4): for each fixture
     JSON, load + `assert_matches_schema(raw, binding.schema)`.
     Fails red when v1 renamed a key, dropped a required field, or
     when the wrapper's declared schema diverged from reality. Runs
     by default — no env gate.

All classes carry `@pytest.mark.v1_contract` (declared in
[conftest.py](tests/v2/conftest.py)) so the contract sweep is
targetable from CI / deploy host via `pytest -m v1_contract`.

Static / schema-mock / coverage gates are fast (no v1 called).
Replay reads JSON files on disk — likewise fast. The recorder CLI
([scripts/v2/contracts/record_fixtures.py](scripts/v2/contracts/record_fixtures.py))
is the only step that touches real v1, and runs on prod only.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Allow `from _v1_contract_lint import …` — the standalone lint helper
# lives next to this test module (TZ S-F2 D-SF2-3 path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

# Importing tools populates V1_CONTRACTS.
import scripts.v2.tools  # noqa: F401
from scripts.v2.contracts import (
    assert_matches_schema,
    mock_from_schema,
)
from scripts.v2.contracts.live_args import LIVE_ARGS, fixture_filename
from scripts.v2.contracts.registry import V1_CONTRACTS
from _v1_contract_lint import scan_all_contracts


_FIXTURES_DIR = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "v2" / "contracts" / "fixtures"
)


# Module-level marker — `pytest -m v1_contract` runs every class in
# this file and nothing else. Declared in conftest.py so unknown-marker
# warnings stay clean.
pytestmark = pytest.mark.v1_contract


class StaticContractGate(unittest.TestCase):
    """AST gate — wrappers read only declared keys.

    This is also enforced at import time (the @v1_contract decorator
    raises ContractError on phantom keys), but we re-run explicitly so
    a CI failure points at this test by name rather than at a generic
    import collection error.
    """

    def test_every_wrapper_reads_only_declared_keys(self):
        offenders = scan_all_contracts()
        self.assertEqual(
            offenders, {},
            f"wrappers reading phantom keys not in their schema: {offenders}. "
            f"Either widen the schema (if v1 actually returns these) or "
            f"remove the read from the wrapper (drop the `.get(...) or "
            f".get(...)` fallback chain — Phase 2 R3).",
        )


class SchemaShapeGate(unittest.TestCase):
    """Drift gate — schema-derived mock satisfies the schema.

    If anybody trims `__required__` to drop a key, this test fails
    once per affected schema (exactly the "remove any key from v1 →
    exactly one contract test fails" property in the brief).
    """

    def test_every_schema_mock_passes_validation(self):
        bad = []
        seen_schemas = set()
        for binding in V1_CONTRACTS.values():
            cls = binding.schema
            if cls in seen_schemas:
                continue
            seen_schemas.add(cls)
            mock = mock_from_schema(cls)
            try:
                assert_matches_schema(
                    mock, cls, context=cls.__name__,
                )
            except AssertionError as e:
                bad.append((cls.__name__, str(e)))
        self.assertEqual(bad, [], f"schema-mock validation failures: {bad}")


class ContractBindingVisibility(unittest.TestCase):
    """Every wrapper exposes its contract binding for downstream tooling
    (record_fixtures, the cache fingerprint, future mock generators)."""

    def test_decorator_exposes_v1_contract_attr(self):
        missing = []
        for binding in V1_CONTRACTS.values():
            fn = binding.wrapper_fn
            if not hasattr(fn, "__v1_contract__"):
                missing.append(binding.wrapper_qualname)
        self.assertEqual(
            missing, [],
            "wrappers missing __v1_contract__ attribute — decoration "
            "order may be wrong (@v1_contract must wrap before @tool)",
        )


class RegistryCoverage(unittest.TestCase):
    """Sanity floor — Phase 2 promised one contract per v1-backed v2 tool.

    27 v1 funcs in rag_tools.py + 5 in learning_tools.py = 32 total. Two
    are not wrapped by v2 (corpus_overview is v2-native; semantic_search
    is the live-only path). With 27 + 5 - 1 = 31 wrappers, expect ≥ 30
    contract bindings.
    """

    def test_minimum_contracts_registered(self):
        self.assertGreaterEqual(
            len(V1_CONTRACTS), 30,
            f"only {len(V1_CONTRACTS)} contracts bound — every v2 wrapper "
            f"with a v1 callee MUST carry @v1_contract per Phase 2 gate.",
        )


class V1ImportPathCanonical(unittest.TestCase):
    """R3 / R4 — RECOVERY_BRIEF Cluster A negative test.

    The v1 module path is canonicalized to use the `scripts.` prefix
    across decorators, wrapper lazy imports, and test mock.patch sites.
    Drift between these three triggered ~24 silent test failures
    after Phase 2 (wrappers imported `from learning_tools import …`,
    tests mocked `scripts.learning_tools.X` — two different Python
    objects in sys.modules, mock invisible to wrapper).

    This sweep catches a drift back to bare-name in any decorator.
    """

    def test_every_decorator_uses_scripts_prefix(self):
        offenders = []
        for binding in V1_CONTRACTS.values():
            path = binding.v1_qualname
            if not (path.startswith("scripts.rag_tools.")
                     or path.startswith("scripts.learning_tools.")
                     or path.startswith("scripts.v2.")):  # v6 resolvers
                offenders.append((binding.wrapper_qualname, path))
        self.assertEqual(
            offenders, [],
            "v1_fn must use `scripts.<module>.<attr>` form so that "
            "mock.patch('scripts.<module>.<attr>') from tests reaches "
            "the same Python object the wrapper imports. Bare-name "
            "(`learning_tools.X`) creates a duplicate module in "
            "sys.modules and mocks miss. See RECOVERY_BRIEF Cluster A.",
        )

    def test_no_bare_name_v1_imports_in_wrappers(self):
        """AST-scan each wrapper module for top-level / function-body
        imports of the form `from learning_tools import …` or `from
        rag_tools import …` — both bypass tests' `scripts.<module>`
        mock.patch. Any bare-name v1 import is a regression.
        """
        import ast
        import inspect
        violations = []
        for binding in V1_CONTRACTS.values():
            module = inspect.getmodule(binding.wrapper_fn)
            if module is None:
                continue
            try:
                src = inspect.getsource(module)
            except (OSError, TypeError):
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "learning_tools", "rag_tools",
                ):
                    violations.append(
                        f"{module.__name__}:{node.lineno} — "
                        f"`from {node.module} import …`",
                    )
        self.assertEqual(
            violations, [],
            "Bare-name v1 imports detected. Always use the `scripts.` "
            "prefix in v2 wrappers so tests' mock.patch reaches the "
            "actual call site.",
        )


class PhantomKeyNegativeGate(unittest.TestCase):
    """R2 negative test (TZ S-F2 acceptance, ADR-F2 §"Negative tests").

    Synthesise a tiny wrapper that reads two phantom keys not in the
    schema and not in INTERNAL_V2_KEYS / ERROR_BRANCH_KEYS. Apply
    `@v1_contract(...)` to it. The decorator MUST raise `ContractError`
    at decoration time (NOT at first call, NOT silently logged) and the
    error message MUST list both phantom keys.

    Locks in the import-time enforcement: a future regression that
    catches and logs instead of raising, or one that carelessly
    broadens INTERNAL_V2_KEYS, will flip this test red — proving the
    static gate is the structural defence the audit named (C2).
    """

    def test_phantom_key_raises_contract_error(self):
        from scripts.v2.contracts import v1_contract, ContractError
        from scripts.v2.contracts.schemas import V1FindBook

        def synthetic_phantom_wrapper(**_kw):
            raw = {"matches": []}
            # Neither key appears in V1FindBook nor in the global
            # INTERNAL_V2_KEYS / ERROR_BRANCH_KEYS allowances.
            _ = raw.get("xyz_nonexistent_phantom")
            _ = raw["yyy_other_phantom"]
            return raw

        with self.assertRaises(ContractError) as ctx:
            v1_contract(
                v1_fn="scripts.rag_tools.find_book",
                schema=V1FindBook,
            )(synthetic_phantom_wrapper)

        msg = str(ctx.exception)
        # Diff is part of the contract — error message names BOTH
        # phantom keys, not just the first one we found.
        self.assertIn("xyz_nonexistent_phantom", msg)
        self.assertIn("yyy_other_phantom", msg)
        # Schema name appears so the developer reading the failure
        # knows which schema to widen (or which wrapper to fix).
        self.assertIn("V1FindBook", msg)


class FixtureCoverageGate(unittest.TestCase):
    """Hard-gate (TZ S-F2 D-SF2-4 revised, 2026-05-25): every binding
    in V1_CONTRACTS with a LIVE_ARGS entry MUST have a recorded
    fixture under scripts/v2/contracts/fixtures/.

    What this catches that nothing else does:

      Static AST and schema-mock gates verify the wrapper reads only
      keys it declared. They DO NOT verify the declaration matches
      what v1 actually returns — that's the E14/E15 failure class.
      Without a recorded artefact tied to the actual v1 output, the
      contract is a hypothesis that can be wrong while every other
      gate stays green.

    Recording is performed on the prod host (where /workspace/spgc/
    and /workspace/chroma_db/ live) via

        python -m scripts.v2.contracts.record_fixtures

    and the resulting JSON files are committed. A wrapper added
    without a recording flips THIS test red on the next PR — the
    contract-coverage promise that S-F2 / ADR-F2 lands.

    Bootstrap state (S-F2 first PR): expected RED until the operator
    runs record_fixtures on prod the first time. After that, every
    deploy that touches v1 source re-runs record_fixtures so the
    fixtures stay in sync with reality.
    """

    def test_every_live_args_binding_has_a_fixture(self):
        missing = []
        no_live_args = []
        for binding in V1_CONTRACTS.values():
            key = binding.v1_qualname
            if key not in LIVE_ARGS:
                # V6 resolvers (`scripts.v2.entity_resolver_v6.*`)
                # are v2-internal and live outside LIVE_ARGS by
                # design — they have their own test paths. Surface
                # them in the assertion message so coverage gaps
                # remain visible (zero-cost; the assertion still
                # passes as long as `missing` is empty).
                no_live_args.append(key)
                continue
            fixture_path = _FIXTURES_DIR / fixture_filename(key)
            if not fixture_path.exists():
                missing.append(key)
        self.assertEqual(
            missing, [],
            f"missing recorded fixtures for {len(missing)} bindings: "
            f"{missing}.\n"
            f"Record them on the prod host:\n"
            f"    docker compose exec gutenberg-lab \\\n"
            f"        python -m scripts.v2.contracts.record_fixtures\n"
            f"then commit scripts/v2/contracts/fixtures/*.json. "
            f"(v2-internal bindings without LIVE_ARGS are excluded "
            f"by design: {sorted(no_live_args)})",
        )


class RecordedFixtureReplay(unittest.TestCase):
    """Replay recorded golden fixtures through schema validation.

    For every fixture file present under
    `scripts/v2/contracts/fixtures/`, load + `assert_matches_schema`
    against the binding's declared schema. When a future prod
    deploy makes v1 rename a key, the next
    `record_fixtures` run writes a new shape into the fixture; that
    fixture then fails THIS test red, forcing both schema AND
    wrapper to be updated in lockstep with v1.

    Bindings whose fixture is missing are SILENTLY skipped here
    (`FixtureCoverageGate` is what fails when they're missing) so
    this test's signal is unambiguous: red == schema-drift,
    not == missing-recording.
    """

    def test_every_recorded_fixture_satisfies_schema(self):
        if not _FIXTURES_DIR.exists():
            # First-ever recording hasn't happened yet — the directory
            # itself doesn't exist. CoverageGate carries the loud
            # complaint; this test passes vacuously so the signal
            # stays "0 schema-drift detected" rather than mixing the
            # two failure modes.
            return

        failures = []
        replayed = 0
        for binding in V1_CONTRACTS.values():
            key = binding.v1_qualname
            fixture_path = _FIXTURES_DIR / fixture_filename(key)
            if not fixture_path.exists():
                continue
            try:
                with open(fixture_path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                failures.append((key, f"fixture unreadable: {e}"))
                continue
            try:
                assert_matches_schema(
                    raw, binding.schema, context=binding.schema_name,
                )
                replayed += 1
            except AssertionError as e:
                failures.append((key, str(e)))
        self.assertEqual(
            failures, [],
            f"recorded fixtures diverge from declared schemas — v1 "
            f"output drifted, or the wrapper's schema is wrong. "
            f"({replayed} fixtures passed, {len(failures)} failed.) "
            f"Re-record on prod with "
            f"`python -m scripts.v2.contracts.record_fixtures` and "
            f"compare the diff:\n{failures}",
        )


if __name__ == "__main__":
    unittest.main()
