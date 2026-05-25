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

All classes carry `@pytest.mark.v1_contract` (declared in
[conftest.py](tests/v2/conftest.py)) so the contract sweep is
targetable from CI / deploy host via `pytest -m v1_contract`.

Designed to be fast (no network, no v1 actually called) and deterministic.
The optional `WC_CONTRACT_LIVE_V1=1` env var swaps in real v1 calls for
contract verification against live data — kept opt-in because a few v1
calls touch disk / pandas / network. Under S-F2 the gated `LIVE_ARGS`
table covers all 19 wrappers driven by the four golden PG books named in
the TZ (PG1342 *Pride and Prejudice*, PG174 *The Picture of Dorian
Gray*, PG345 *Dracula*, PG84 *Frankenstein*).
"""
from __future__ import annotations

import os
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
from scripts.v2.contracts.registry import V1_CONTRACTS
from _v1_contract_lint import scan_all_contracts


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


# Golden-book regexes — PG1342 / PG174 / PG345 / PG84 anchor the live
# sweep per TZ S-F2 "фикстура «golden books»". The four books cover
# distinct author regexes so author-scope contracts exercise real-name
# matching (not just the regex syntax). PG174 (Dorian Gray) does not
# appear in every wrapper's args directly — book-scope wrappers rotate
# through PG1342/PG345/PG84 so coverage spans more than one (id, raw)
# pair against real corpus shape.
_AUSTEN = "^Austen,"          # PG1342
_WILDE = "^Wilde, Oscar$"     # PG174
_STOKER = "^Stoker, Bram$"    # PG345
_SHELLEY = "^Shelley, Mary"   # PG84


@unittest.skipUnless(os.environ.get("WC_CONTRACT_LIVE_V1") == "1",
                     "live v1 contract sweep is opt-in (slow, touches disk/network)")
class LiveV1Contracts(unittest.TestCase):
    """Optional: call each v1 with golden-book args; verify against schema.

    Gated by WC_CONTRACT_LIVE_V1 because several v1 functions read
    pandas / chroma / disk. CI green path doesn't need it; the deploy
    host flips the env var via `predeploy_gate.sh` so the sweep runs
    against the live corpus the prod chat is serving.

    The LIVE_ARGS table covers every binding in V1_CONTRACTS — one
    entry per `v1_qualname`. Args are tuned to hit the four golden
    PG books (PG1342 Pride and Prejudice, PG174 The Picture of Dorian
    Gray, PG345 Dracula, PG84 Frankenstein) and their authors. Live
    failures are recorded as `(key, diff)` tuples and surface with a
    schema-mismatch diff in the test failure — the TZ S-F2 acceptance
    criterion in literal form.
    """

    LIVE_ARGS: dict[str, dict] = {
        # rag_tools — word-scope (words sampled from the golden corpus)
        "scripts.rag_tools.word_collocates":
            {"word": "love", "window": 4},
        "scripts.rag_tools.emotion_collocates":
            {"emotion": "fear"},
        "scripts.rag_tools.word_contexts":
            {"word": "love", "author_regex": _AUSTEN},
        "scripts.rag_tools.word_contexts_global":
            {"word": "blood"},
        "scripts.rag_tools.word_pos_distribution":
            {"word": "love"},
        "scripts.rag_tools.word_freq_timeline":
            {"word": "monster"},
        "scripts.rag_tools.words_disappearing_after":
            {"year": 1920, "top": 5},
        "scripts.rag_tools.words_appearing_after":
            {"year": 1920, "top": 5},
        "scripts.rag_tools.word_etymology":
            {"word": "sword"},
        "scripts.rag_tools.find_words_by_etymology":
            {"family": "Old_English"},

        # rag_tools — author-scope (regexes hit the four golden authors)
        "scripts.rag_tools.affinity_by_author":
            {"author_regex": _AUSTEN, "top": 5, "min_corpus_count": 500},
        "scripts.rag_tools.compare_authors":
            {"author1_regex": _AUSTEN, "author2_regex": _WILDE},
        "scripts.rag_tools.corpus_stats_by_author":
            {"author_regex": _STOKER},
        "scripts.rag_tools.author_profile":
            {"author_regex": _SHELLEY},
        "scripts.rag_tools.author_influences":
            {"author_regex": _AUSTEN},
        "scripts.rag_tools.author_attribution":
            {"text_sample": "It is a truth universally acknowledged."},
        "scripts.rag_tools.author_metadata":
            {"author_regex": _WILDE},
        "scripts.rag_tools.top_ngrams_by_author":
            {"author_regex": _AUSTEN, "n": 2},
        "scripts.rag_tools.lexical_diversity":
            {"author_regex": _STOKER},

        # rag_tools — book-scope (golden PG ids)
        "scripts.rag_tools.book_readability":
            {"pg_id": "PG1342"},
        "scripts.rag_tools.book_emotion_profile":
            {"pg_id": "PG345"},

        # rag_tools — global search / top-N (no scope arg needed)
        "scripts.rag_tools.semantic_search":
            {"query": "vampire"},
        "scripts.rag_tools.find_book":
            {"title": "Pride and Prejudice"},
        "scripts.rag_tools.top_authors_by":
            {"metric": "books"},
        "scripts.rag_tools.top_authors_by_country":
            {"country": "GB"},
        "scripts.rag_tools.top_books_by_downloads":
            {"top_n": 5},
        "scripts.rag_tools.top_books_by_recency":
            {"top_n": 5},

        # learning_tools — covers PG84 + PG174 to round out the four
        "scripts.learning_tools.learning_words":
            {"scope": "book:PG1342", "level": "intermediate"},
        "scripts.learning_tools.enrich_word":
            {"word": "pleasure"},
        "scripts.learning_tools.export_word_list":
            {"entries": [{"word": "love"}], "format": "anki_csv"},
        "scripts.learning_tools.affinity_by_book":
            {"pg_id": "PG174"},
        "scripts.learning_tools.book_archaic_words":
            {"pg_id": "PG84", "top": 10},
    }

    def test_live_v1_outputs_match_schema(self):
        failures = []
        for binding in V1_CONTRACTS.values():
            key = binding.v1_qualname
            args = self.LIVE_ARGS.get(key)
            if args is None:
                # V6 resolvers (`scripts.v2.entity_resolver_v6.*`) are
                # not in LIVE_ARGS — they're v2-internal and have their
                # own test paths. The sweep skips them by design.
                continue
            try:
                raw = binding.resolved_v1_fn()(**args)
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
