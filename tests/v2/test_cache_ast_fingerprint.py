"""S-F1 / ADR-F1 / D67 — AST-hash cache invalidation tests.

Twelve invariants pin the contract described in
[docs/v2/decisions.md](../../docs/v2/decisions.md) → 2026-05-25 S-F1:

1.  Body edit on a wrapper flips its fingerprint.
2.  Body edit on a shared depth=1 helper propagates to every consumer.
3.  Cosmetic (whitespace) edits do NOT flip the fingerprint.
4.  Cosmetic (comment-only) edits on a depth=1 helper do NOT propagate.
5.  C-extension fallback: ``inspect.getsource`` failure → stable
    ``unsource:`` marker, no exception leaks.
6.  v2-native tools (no ``@v1_contract``) fingerprint via ``spec.fn``
    fallback; coverage is 37/37 not 32/37.
7.  ``WC_CACHE_PIN_FINGERPRINT=1`` snapshots the full registry at
    first call; subsequent ``_ast_fingerprint_for`` reads serve the
    snapshot even when sources change.
8.  ``WC_CACHE_AST_INVALIDATION=off`` short-circuits the walk:
    ``cache_key`` reverts to ``(schema, wrapper_version, args)`` only.
9.  Depth=1 callee picked up: a same-``scripts.``-module helper
    referenced from the entry fn is folded into the fingerprint.
10. Depth=2 callee NOT picked up: a helper-of-a-helper edit does NOT
    flip the entry fn's fingerprint — the ceiling is enforced.
11. Determinism: ``ast_fingerprint(a, b) == ast_fingerprint(b, a)``.
12. ``CACHE_SCHEMA_VERSION`` change globally invalidates ``cache_key``.

Plus a thirteenth integration check on the
:mod:`scripts.v2.cache_fingerprint_audit` CLI module — covering the
TZ acceptance line "12 тестов + CI-гейт ``cache_fingerprint_audit.py
--since=HEAD~1``".

Test helpers that need to be picked up by ``_depth1_callees`` lie about
their ``__module__`` (set to ``"scripts._fp_test_fixture"``). This
lets the depth=1 walk's project-boundary filter (``__module__``
startswith ``scripts.``, see D-SF1-3) include them WITHOUT shipping a
real fixture module under ``scripts/``. ``inspect.getsource`` uses
``__code__.co_filename`` (not ``__module__``), so monkey-patching
``__module__`` does not affect source lookup.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import cache
from scripts.v2.contracts.registry import (
    _depth1_callees,
    ast_fingerprint,
    wrapper_fingerprint_for_tool,
)


def _as_in_scripts(fn):
    """Mark `fn` as living under ``scripts.`` so the walk includes it.

    Mutates ``fn.__module__`` in place; the actual source file (used by
    ``inspect.getsource``) is unaffected. Returns the fn for chaining.
    """
    fn.__module__ = "scripts._fp_test_fixture"
    return fn


# ---------------------------------------------------------------------------
# Fixture functions — at module level so inspect.getsource can find them.
# Each pair (_a, _b) is identical in *name* shape but different in body so
# fingerprint comparisons isolate the body axis.
# ---------------------------------------------------------------------------


def _body_a():
    return 1


def _body_b():
    return 1 + 1  # different AST


# Cosmetic-edit pair: same AST, different whitespace/comments. Both
# functions have the SAME `__name__` ("f") so `ast.dump` (which includes
# the function's name in `FunctionDef(name=...)`) does not contribute
# to the diff — only formatting differs. Defined via closures so the
# inner names don't collide at module scope. `_ast_part_for` applies
# `textwrap.dedent` before parsing so the indented closure source still
# round-trips through the AST.
def _make_cosmetic_clean():
    def f():
        return 42
    f.__module__ = "scripts._fp_test_fixture"
    return f


def _make_cosmetic_commented():
    def f():
        # this comment must not affect the fingerprint
        return     42        # neither should the extra whitespace
    f.__module__ = "scripts._fp_test_fixture"
    return f


_cosmetic_a = _make_cosmetic_clean()
_cosmetic_b = _make_cosmetic_commented()


# Shared-helper propagation: two `_shared_helper_*` versions, a single
# `_consumer_using_shared` whose globals get rebound between calls.
def _shared_helper_v1():
    return "v1-payload"


def _shared_helper_v2():
    return "v2-payload"  # different literal → different AST


def _consumer_using_shared():
    # Body references `_shared_helper_v1` by name; the walk resolves
    # the name in `_consumer_using_shared.__globals__`, which can be
    # rebound for the propagation test.
    return _shared_helper_v1()


# Cosmetic helper-edit: same AST, different comment. Used by test 4.
# Same closure trick as _cosmetic_a/_cosmetic_b so the inner `h` names
# match exactly.
def _make_helper_h_clean():
    def h():
        return 7
    h.__module__ = "scripts._fp_test_fixture"
    return h


def _make_helper_h_commented():
    def h():
        # only a comment differs from h_clean
        return 7
    h.__module__ = "scripts._fp_test_fixture"
    return h


_cosmetic_helper_v1 = _make_helper_h_clean()
_cosmetic_helper_v2 = _make_helper_h_commented()


# Depth=2 chain: `_top_caller` calls `_mid_helper` (depth=1), which
# calls `_leaf_helper` (depth=2 from `_top_caller`).
def _leaf_helper_v1():
    return "leaf-v1"


def _leaf_helper_v2():
    return "leaf-v2"


def _mid_helper():
    return _leaf_helper_v1()


def _top_caller():
    return _mid_helper()


# Mark all the above as living in `scripts.` so the walk admits them.
for _fn in (
    _body_a, _body_b,
    _cosmetic_a, _cosmetic_b,
    _shared_helper_v1, _shared_helper_v2,
    _consumer_using_shared,
    _cosmetic_helper_v1, _cosmetic_helper_v2,
    _leaf_helper_v1, _leaf_helper_v2,
    _mid_helper, _top_caller,
):
    _as_in_scripts(_fn)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestAstFingerprint(unittest.TestCase):
    """Twelve invariants of ADR-F1 (S-F1 / D67)."""

    def setUp(self):
        # Each test starts with a clean fp snapshot and on env state.
        cache._reset_ast_fp_cache_for_tests()
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"WC_CACHE_AST_INVALIDATION": "on"},
            clear=False,
        )
        self._env_patcher.start()
        # Remove pin-mode if set in the inherited env.
        os.environ.pop("WC_CACHE_PIN_FINGERPRINT", None)
        self.addCleanup(self._env_patcher.stop)
        self.addCleanup(cache._reset_ast_fp_cache_for_tests)

    # 1. Body edit on a wrapper flips its fingerprint.
    def test_01_body_edit_flips_fingerprint(self):
        fp_a = ast_fingerprint(_body_a)
        fp_b = ast_fingerprint(_body_b)
        self.assertNotEqual(
            fp_a, fp_b,
            "Different function bodies must produce different "
            "fingerprints — otherwise cache invalidation is broken.",
        )

    # 2. Body edit on a shared depth=1 helper propagates to consumers.
    def test_02_shared_helper_edit_propagates_to_consumer(self):
        original = _consumer_using_shared.__globals__["_shared_helper_v1"]
        try:
            fp_before = ast_fingerprint(_consumer_using_shared)
            # Swap the helper binding without changing the consumer's
            # bytecode — the walk should pick up the new helper via
            # `globals()` lookup and re-hash.
            _consumer_using_shared.__globals__["_shared_helper_v1"] = (
                _shared_helper_v2
            )
            fp_after = ast_fingerprint(_consumer_using_shared)
        finally:
            _consumer_using_shared.__globals__["_shared_helper_v1"] = original

        self.assertNotEqual(
            fp_before, fp_after,
            "Editing a shared depth=1 helper must invalidate the "
            "fingerprint of every consumer that calls it — this is the "
            "TZ acceptance criterion for _title_lookup-class helpers.",
        )

    # 3. Cosmetic edits do NOT flip the fingerprint.
    def test_03_cosmetic_edit_does_not_flip_fingerprint(self):
        fp_a = ast_fingerprint(_cosmetic_a)
        fp_b = ast_fingerprint(_cosmetic_b)
        self.assertEqual(
            fp_a, fp_b,
            "Whitespace + comments must not flip the fingerprint — "
            "ast.dump strips them before hashing. Otherwise every "
            "reformat busts the cache.",
        )

    # 4. Cosmetic edits on a depth=1 helper do NOT propagate.
    def test_04_cosmetic_helper_edit_does_not_propagate(self):
        original = _consumer_using_shared.__globals__["_shared_helper_v1"]
        try:
            _consumer_using_shared.__globals__["_shared_helper_v1"] = (
                _cosmetic_helper_v1
            )
            fp_before = ast_fingerprint(_consumer_using_shared)
            _consumer_using_shared.__globals__["_shared_helper_v1"] = (
                _cosmetic_helper_v2
            )
            fp_after = ast_fingerprint(_consumer_using_shared)
        finally:
            _consumer_using_shared.__globals__["_shared_helper_v1"] = original

        self.assertEqual(
            fp_before, fp_after,
            "Cosmetic helper edits must not propagate either — the "
            "AST-strip applies to depth=1 callees too.",
        )

    # 5. C-extension fallback: builtin → stable `unsource:` marker.
    def test_05_c_extension_fallback_is_stable(self):
        # `len` is a builtin — `inspect.getsource(len)` raises TypeError.
        fp_once = ast_fingerprint(len)
        fp_twice = ast_fingerprint(len)
        self.assertEqual(
            fp_once, fp_twice,
            "C-extension callable must produce a stable fingerprint "
            "via the `unsource:` marker — no exception leakage.",
        )
        # And it should produce a different fingerprint from a sourced
        # function, not collide with whatever default-empty hash:
        fp_sourced = ast_fingerprint(_body_a)
        self.assertNotEqual(
            fp_once, fp_sourced,
            "C-extension `unsource:` fingerprint should not collide "
            "with a real sourced function's fingerprint.",
        )

    # 6. Full coverage (40/40 non-empty) + v1-callee walk reaches real
    # rag_tools helpers for the bulk of contract-bound tools. This is
    # stronger than the original "non-empty fp" check — non-empty only
    # proves "something hashed", not "v1 helpers are inside the hash".
    def test_06_full_coverage_and_v1_callee_walk_reaches_helpers(self):
        import scripts.v2.tools  # noqa: F401 — registers tools
        from scripts.v2.tool_registry import REGISTRY
        from scripts.v2.contracts.registry import (
            V1_CONTRACTS, _depth1_callees,
        )

        # Part (a): full coverage — every tool returns non-empty fp.
        empty: list[str] = []
        for tool_name in REGISTRY:
            fp = wrapper_fingerprint_for_tool(tool_name)
            self.assertIsNotNone(
                fp,
                f"tool {tool_name} returned None — registry import "
                f"failed or wrapper_fingerprint_for_tool regressed",
            )
            if fp == "":
                empty.append(tool_name)
        self.assertEqual(
            empty, [],
            f"Empty fingerprint means cache invalidation cannot move "
            f"for these tools — neither contract-bound nor spec.fn "
            f"fallback produced source: {empty}",
        )

        # Part (b): contract-bound tools' v1-callees are practically
        # walked. We identify bindings via identity-match against
        # spec.fn (matches the production lookup path), then ask
        # _depth1_callees(v1_fn) for each — expect the BULK to return
        # at least one same-`scripts.`-module helper. Some v1 functions
        # are leaf callables (pure dict shaping, no helpers) so a
        # strict "every bound tool" would over-specify.
        bound_tools: dict = {}
        for tool_name, spec in REGISTRY.items():
            target = spec.fn
            target_inner = getattr(target, "__wrapped__", target)
            for b in V1_CONTRACTS.values():
                cand = b.wrapper_fn
                cand_inner = getattr(cand, "__wrapped__", cand)
                if cand is target or cand_inner is target_inner:
                    bound_tools[tool_name] = b
                    break

        self.assertGreater(
            len(bound_tools), 20,
            "Expected at least 20 contract-bound tools to exercise the "
            "v1-callee walk path; got fewer — @v1_contract coverage "
            "may have regressed.",
        )

        tools_with_v1_helpers_walked: list[str] = []
        for tool_name, binding in bound_tools.items():
            try:
                v1_fn = binding.resolved_v1_fn()
            except Exception:
                continue
            callees = _depth1_callees(v1_fn)
            if callees:
                tools_with_v1_helpers_walked.append(tool_name)

        self.assertGreater(
            len(tools_with_v1_helpers_walked),
            len(bound_tools) // 2,
            f"v1-callee walk reaches helpers for only "
            f"{len(tools_with_v1_helpers_walked)} of "
            f"{len(bound_tools)} contract-bound tools — the bulk "
            f"should walk into rag_tools helpers. Walk may have "
            f"regressed; check `_depth1_callees` filter.",
        )

    # 6b. Strongest acceptance for TZ "правка shared-хелпера _title_lookup
    # меняет fp всех потребителей": swap a real rag_tools helper that's
    # a known depth=1 callee of a real prod tool's v1, verify the tool's
    # fingerprint moves. Locks the practical contract end-to-end.
    def test_06b_real_v1_helper_edit_flips_real_tool_fp(self):
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY
        from scripts.v2.contracts.registry import V1_CONTRACTS

        spec = REGISTRY.get("affinity_by_author")
        if spec is None:
            self.skipTest("affinity_by_author not in REGISTRY")

        target = spec.fn
        target_inner = getattr(target, "__wrapped__", target)
        binding = None
        for b in V1_CONTRACTS.values():
            cand = getattr(b.wrapper_fn, "__wrapped__", b.wrapper_fn)
            if cand is target_inner or b.wrapper_fn is target:
                binding = b
                break
        self.assertIsNotNone(
            binding,
            "no contract binding for affinity_by_author — the v1-callee "
            "walk path for this tool isn't exercised",
        )

        v1_fn = binding.resolved_v1_fn()
        rag_tools_module = sys.modules[v1_fn.__module__]
        # `_slug` is a known depth=1 callee of v1 affinity_by_author
        # (verified empirically: rag_tools.affinity_by_author co_names
        # includes `_slug`, `_log`, `_spacy_pos_tags`).
        helper_name = "_slug"
        original_helper = getattr(rag_tools_module, helper_name, None)
        self.assertIsNotNone(
            original_helper,
            f"rag_tools.{helper_name} not present — test fixture is "
            f"out of sync with the v1 module shape",
        )

        def _alt_helper(s):  # noqa: ANN001
            # Different body → different ast.dump → different fp.
            return s + "__altered_for_test"
        _alt_helper.__module__ = original_helper.__module__
        _alt_helper.__name__ = helper_name

        # Reset the process-level fp cache between calls, otherwise the
        # first call's fp is cached and the second returns the same
        # value regardless of the helper swap.
        try:
            cache._reset_ast_fp_cache_for_tests()
            fp_before = wrapper_fingerprint_for_tool("affinity_by_author")
            setattr(rag_tools_module, helper_name, _alt_helper)
            cache._reset_ast_fp_cache_for_tests()
            fp_after = wrapper_fingerprint_for_tool("affinity_by_author")
        finally:
            setattr(rag_tools_module, helper_name, original_helper)
            cache._reset_ast_fp_cache_for_tests()

        self.assertNotEqual(
            fp_before, fp_after,
            f"Editing the real rag_tools.{helper_name} (a depth=1 v1 "
            f"helper of affinity_by_author) must flip affinity_by_author's "
            f"fingerprint. If it doesn't, the walk doesn't actually "
            f"reach v1 helpers — S-F1's central promise is broken.",
        )

    # 14. Documented gap (D67 §D-SF1-4): symbols introduced by `from X
    # import Y` INSIDE a function body — or accessed via `module.attr`
    # — are NOT picked up by the depth=1 walk because they aren't in
    # the function's `__globals__`. `wrapper_version` is the explicit
    # manual backstop for this class (D-SF1-4 imperative).
    #
    # **Forcing-function design.** This test asserts that the walk
    # *DOES* reach the lazy-imported / module.attr-accessed callee.
    # Currently it doesn't, so the assertion *fails* — which is why
    # the method is decorated `@pytest.mark.xfail(strict=True)`. If
    # the gap is ever closed (the walk extended to follow lazy
    # imports or attribute access), the assertion will pass, XPASS
    # under strict mode flips the suite to red, and the developer is
    # forced to update D67 explicitly. Without `strict=True` an XPASS
    # would be silent — the forcing function would be dead.
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "D-SF1-4 documented gap: depth=1 walk does NOT follow "
            "lazy imports inside function body, nor `module.attr` "
            "attribute access. Backstop is wrapper_version (manual). "
            "If this test starts passing, the gap was closed — "
            "update D67 and remove this xfail. strict=True ensures "
            "the closure cannot land silently."
        ),
    )
    def test_14_lazy_import_and_module_attr_gap_is_documented(self):
        # Construct a wrapper that lazy-imports a helper inside its
        # body. We use `os.path` only because it's a stable stdlib
        # symbol; the relevant property is that `path` ends up in
        # `co_varnames`, not `co_names`. To make the test ALSO assert
        # the project-scope filter behaviour, we monkey-patch the
        # lazy-imported attribute's __module__ to look like an in-tree
        # helper — so the ONLY thing keeping it out of the fingerprint
        # is the resolution-model gap, not the `scripts.`-filter.
        def _wrapper_with_lazy_import():
            from os import path  # lazy: `path` becomes a LOCAL, not a global
            return path.join("a", "b")

        _wrapper_with_lazy_import.__module__ = "scripts._fp_test_fixture"

        callees = _depth1_callees(_wrapper_with_lazy_import)
        callee_names = {getattr(c, "__name__", "?") for c in callees}
        # Expected (today): the walk does NOT discover `join` because
        # `path` is a local, not a global. xfail(strict=True) holds the
        # contract: if a future change closes the gap, this assertion
        # starts passing, XPASS fails the suite, D67 must be updated.
        self.assertIn(
            "join", callee_names,
            "Expected the depth=1 walk to discover `os.path.join` from "
            "a lazy `from os import path; path.join(...)` pattern. "
            "Currently it does NOT (the gap D-SF1-4 owns). When this "
            "starts passing, remove the xfail and document the new "
            "resolution model in D67.",
        )

        # Module.attr pattern: `import scripts.X as alias; alias.fn()` —
        # `alias` is in `co_names` and IS in globals (if imported at
        # module level), but it's a module, not a callable. `fn` is
        # accessed via attribute lookup at runtime — never in `co_names`.
        import scripts.v2.cache as _alias_cache  # noqa: F401 — module-level

        def _wrapper_with_module_attr():
            return _alias_cache.cache_key("t", {})  # module.attr call

        _wrapper_with_module_attr.__module__ = "scripts._fp_test_fixture"
        _wrapper_with_module_attr.__globals__["_alias_cache"] = _alias_cache
        callees2 = _depth1_callees(_wrapper_with_module_attr)
        callee_names2 = {getattr(c, "__name__", "?") for c in callees2}
        # Same forcing-function design: assert the closure of the gap.
        # When this starts passing, the walk has been extended; D67
        # update mandatory.
        self.assertIn(
            "cache_key", callee_names2,
            "Expected the depth=1 walk to discover `cache_key` from "
            "the `_alias_cache.cache_key(...)` module.attr pattern. "
            "Currently it does NOT — LOAD_ATTR never surfaces in "
            "`co_names`. Backstop: wrapper_version (manual).",
        )

    # 7. WC_CACHE_PIN_FINGERPRINT=1 snapshots at first call.
    def test_07_pin_mode_snapshots_full_registry(self):
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY

        cache._reset_ast_fp_cache_for_tests()
        with mock.patch.dict(
            os.environ, {"WC_CACHE_PIN_FINGERPRINT": "1"},
        ):
            self.assertFalse(cache._AST_FP_PINNED)
            # First call triggers the full sweep.
            cache._ast_fingerprint_for(next(iter(REGISTRY)))
            self.assertTrue(cache._AST_FP_PINNED)
            self.assertGreaterEqual(
                len(cache._AST_FP_CACHE), len(REGISTRY),
                "Pin mode must populate fingerprints for the whole "
                "REGISTRY on first call, not just the one requested.",
            )

    # 8. WC_CACHE_AST_INVALIDATION=off short-circuits the walk.
    def test_08_kill_switch_off_reverts_to_wrapper_version_only(self):
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY

        # Pick a tool present in REGISTRY (any contract-bound one).
        tool_name = next(
            t for t, s in REGISTRY.items() if s.cacheable
        )

        cache._reset_ast_fp_cache_for_tests()
        with mock.patch.dict(
            os.environ, {"WC_CACHE_AST_INVALIDATION": "on"},
        ):
            key_on = cache.cache_key(
                tool_name, {"sample": "args"}, wrapper_version="v6",
            )

        cache._reset_ast_fp_cache_for_tests()
        with mock.patch.dict(
            os.environ, {"WC_CACHE_AST_INVALIDATION": "off"},
        ):
            key_off = cache.cache_key(
                tool_name, {"sample": "args"}, wrapper_version="v6",
            )

        self.assertNotEqual(
            key_on, key_off,
            "With AST invalidation ON the key folds the fingerprint; "
            "with it OFF the key folds an empty string — they must "
            "differ for the same (args, wrapper_version).",
        )

    # 9. Depth=1 callee picked up.
    def test_09_depth1_callee_is_in_walk(self):
        callees = _depth1_callees(_consumer_using_shared)
        names = {getattr(c, "__name__", "?") for c in callees}
        self.assertIn(
            "_shared_helper_v1", names,
            f"_depth1_callees missed _shared_helper_v1 in consumer "
            f"globals. Found: {names}",
        )

    # 10. Depth=2 callee NOT picked up (ceiling enforced).
    def test_10_depth2_callee_not_in_walk(self):
        # `_top_caller` calls `_mid_helper` (depth=1). `_mid_helper`
        # calls `_leaf_helper_v1` (depth=2 from _top_caller). The walk
        # must STOP at depth=1.
        callees = _depth1_callees(_top_caller)
        names = {getattr(c, "__name__", "?") for c in callees}
        self.assertIn(
            "_mid_helper", names,
            "_mid_helper (depth=1) must be in the walk",
        )
        self.assertNotIn(
            "_leaf_helper_v1", names,
            "_leaf_helper_v1 (depth=2) must NOT be in the walk — the "
            "depth=1 ceiling is what keeps over-invalidation bounded.",
        )

        # Sanity: editing _leaf_helper alone should NOT flip the fp
        # of _top_caller. We simulate the edit by swapping the binding
        # in `_mid_helper`'s globals — _top_caller's fp must stay put.
        original = _mid_helper.__globals__["_leaf_helper_v1"]
        try:
            fp_before = ast_fingerprint(_top_caller)
            _mid_helper.__globals__["_leaf_helper_v1"] = _leaf_helper_v2
            fp_after = ast_fingerprint(_top_caller)
        finally:
            _mid_helper.__globals__["_leaf_helper_v1"] = original

        self.assertEqual(
            fp_before, fp_after,
            "Depth=2 edit (helper-of-a-helper) must NOT invalidate "
            "the top caller's fingerprint — D-SF1-3 ceiling.",
        )

    # 11. Determinism: order-independent.
    def test_11_fingerprint_is_order_independent(self):
        fp_ab = ast_fingerprint(_body_a, _body_b)
        fp_ba = ast_fingerprint(_body_b, _body_a)
        self.assertEqual(
            fp_ab, fp_ba,
            "ast_fingerprint must sort by qualname before hashing — "
            "argument order cannot influence the result, otherwise "
            "the cache_key would be sensitive to internal registry "
            "ordering.",
        )

    # 12. CACHE_SCHEMA_VERSION change globally invalidates cache_key.
    def test_12_schema_version_invalidates_globally(self):
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY

        tool_name = next(iter(REGISTRY))
        original_schema = cache.CACHE_SCHEMA_VERSION
        try:
            cache.CACHE_SCHEMA_VERSION = "v3-ast-fp"
            cache._reset_ast_fp_cache_for_tests()
            k1 = cache.cache_key(tool_name, {"x": 1}, wrapper_version="v1")
            cache.CACHE_SCHEMA_VERSION = "v4-hypothetical"
            cache._reset_ast_fp_cache_for_tests()
            k2 = cache.cache_key(tool_name, {"x": 1}, wrapper_version="v1")
        finally:
            cache.CACHE_SCHEMA_VERSION = original_schema
            cache._reset_ast_fp_cache_for_tests()

        self.assertNotEqual(
            k1, k2,
            "A schema version bump must invalidate every entry — it's "
            "the global escape hatch for shape changes that the "
            "fingerprint cannot detect.",
        )

    # 13 (CI gate). cache_fingerprint_audit CLI is importable and the
    # no-changes path returns 0. The full diff path is exercised in CI
    # against a real range; this test pins the contract surface.
    def test_13_audit_cli_imports_and_no_diff_returns_zero(self):
        from scripts.v2 import cache_fingerprint_audit

        # `audit("HEAD")` — diff between HEAD and HEAD is empty → no
        # scripts/ files changed → skip path, exit 0.
        code, msg = cache_fingerprint_audit.audit("HEAD")
        self.assertEqual(
            code, 0,
            f"audit('HEAD') should short-circuit on empty diff, got "
            f"exit code {code} with msg: {msg}",
        )
        self.assertIn(
            "no scripts/ files changed", msg,
            f"Expected the skip message, got: {msg}",
        )


if __name__ == "__main__":
    unittest.main()
