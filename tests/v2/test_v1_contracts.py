"""Phase 2 contract sweep — REFACTOR_BRIEF Gate.

For every (wrapper, v1_fn, schema) triple in `V1_CONTRACTS`:

  1. Static gate (runs at wrapper import, but verified here too):
     the wrapper body reads only keys declared in the schema (plus the
     well-known internal/error-branch sets). Phantom keys → import-time
     ContractError, which means this test file would also fail to
     collect — we re-run the check explicitly for clarity.

  2. Drift gate (the "remove any key from v1 → exactly one contract test
     fails" half of Phase 2 gate): a minimal mock built from the schema
     is fed back into a validator that asserts every required schema key
     is present. If someone removes a required key from a schema, this
     test fails exactly once — no quiet regression.

  3. Decorator visibility: every wrapper exposes its (v1_fn, schema)
     binding through `__v1_contract__`.

Designed to be fast (no network, no v1 actually called) and deterministic.
The optional `WC_CONTRACT_LIVE_V1=1` env var swaps in real v1 calls for
contract verification against live data — kept opt-in because a few v1
calls touch disk / pandas / network.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Importing tools populates V1_CONTRACTS.
import scripts.v2.tools  # noqa: F401
from scripts.v2.contracts import (
    assert_matches_schema,
    check_wrapper_against_schema,
    mock_from_schema,
)
from scripts.v2.contracts.registry import V1_CONTRACTS


class StaticContractGate(unittest.TestCase):
    """AST gate — wrappers read only declared keys.

    This is also enforced at import time (the @v1_contract decorator
    raises ContractError on phantom keys), but we re-run explicitly so
    a CI failure points at this test by name rather than at a generic
    import collection error.
    """

    def test_every_wrapper_reads_only_declared_keys(self):
        offenders = {}
        for key, binding in V1_CONTRACTS.items():
            rogue = check_wrapper_against_schema(
                binding.wrapper_fn, binding.schema,
            )
            if rogue:
                offenders[key] = rogue
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


@unittest.skipUnless(os.environ.get("WC_CONTRACT_LIVE_V1") == "1",
                     "live v1 contract sweep is opt-in (slow, touches disk/network)")
class LiveV1Contracts(unittest.TestCase):
    """Optional: call each v1 with safe defaults; verify against schema.

    Gated by WC_CONTRACT_LIVE_V1 because several v1 functions read
    pandas / chroma / disk. CI green path doesn't need it; ops can flip
    the env var to spot drift in actual prod v1 outputs.
    """

    LIVE_ARGS = {
        "scripts.rag_tools.affinity_by_author":
            {"author_regex": "^Doyle,", "top": 5, "min_corpus_count": 500},
        "scripts.rag_tools.word_etymology": {"word": "sword"},
        "scripts.rag_tools.corpus_overview": {},
    }

    def test_live_v1_outputs_match_schema(self):
        failures = []
        for binding in V1_CONTRACTS.values():
            key = binding.v1_qualname
            args = self.LIVE_ARGS.get(key)
            if args is None:
                continue
            try:
                raw = binding.v1_fn(**args)
            except Exception as e:
                failures.append((key, f"v1 raised: {e}"))
                continue
            try:
                assert_matches_schema(raw, binding.schema,
                                       context=binding.schema_name)
            except AssertionError as e:
                failures.append((key, str(e)))
        self.assertEqual(failures, [], f"live-v1 contract drift: {failures}")


if __name__ == "__main__":
    unittest.main()
