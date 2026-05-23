"""Pytest fixtures shared by the v2 suite.

Critical: isolate scripts.v2.cache disk store to a session tmpdir.
Without this, the v2 disk cache at WC_V2_CACHE_DIR (defaults to
/data/v2_cache) survives across pytest runs. Tests that monkeypatch
scripts.rag_tools then dispatch a `cacheable=True` tool either prime
that on-disk store (poisoning future runs) or read a stale entry from
a previous mock shape (producing order-coupled false positives). The
disk cache key folds in an AST fingerprint of (wrapper, v1_callee), but
fingerprint computation can land on a fake v1 module during tests, so
re-using the shared cache across runs is unsafe regardless.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_CACHE_DIR = tempfile.mkdtemp(prefix="wc-v2-cache-")
os.environ["WC_V2_CACHE_DIR"] = _TMP_CACHE_DIR

if "scripts.v2.cache" in sys.modules:
    sys.modules["scripts.v2.cache"].CACHE_ROOT = Path(_TMP_CACHE_DIR)


@pytest.fixture(autouse=True)
def _reset_v2_lru_cache():
    """Drop the in-process LRU before every test.

    Disk isolation alone is not enough: scripts.v2.cache._lru is a
    module-level OrderedDict that persists across tests within a single
    pytest session. A test that mocks v1 and triggers cache_put will
    leave the mocked result visible to any later test using the same
    (tool, args) key.
    """
    try:
        from scripts.v2.cache import cache_clear
        cache_clear()
    except ImportError:
        pass
    yield


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_CACHE_DIR, ignore_errors=True)


# --- T0 / REMEDIATION_BRIEF: v1-layer module-swap isolation -----------------
#
# test_router, test_search, test_tool_contract and test_tool_migration install
# fake ModuleType stubs for scripts.rag_tools / scripts.rag_query /
# scripts.learning_tools in their per-test setUp, but their tearDown only pops
# rag_tools/rag_query — it leaks a fake scripts.learning_tools and never
# restores the real modules. A downstream test then desyncs: mock.patch
# resolves the v1 module via the `scripts` package attribute while wrapper
# bodies do `from scripts.rag_tools import ...` via sys.modules, so the test
# silently runs real v1 instead of its mock (the order-dependent failures
# catalogued in REMEDIATION_BRIEF / T0).
#
# The fixture below pins all three back to their canonical real module after
# every test. It is a strict no-op for tests that never swap the v1 layer
# (sys.modules entry already IS the canonical object). test_pipeline_e2e is
# exempt: it swaps the v1 layer in setUpClass and restores it in
# tearDownClass itself, and a function-scoped fixture must not fight that
# class-scoped lifecycle.

_REAL_V1_MODULES: dict = {}


@pytest.fixture(autouse=True)
def _isolate_v1_module_swaps(request):
    _names = ("scripts.rag_query", "scripts.rag_tools", "scripts.learning_tools")
    for n in _names:
        m = sys.modules.get(n)
        if n not in _REAL_V1_MODULES and m is not None and getattr(m, "__file__", None):
            _REAL_V1_MODULES[n] = m
    yield
    if request.module.__name__.rsplit(".", 1)[-1] == "test_pipeline_e2e":
        return
    pkg = sys.modules.get("scripts")
    for n, real in _REAL_V1_MODULES.items():
        if sys.modules.get(n) is not real:
            sys.modules[n] = real
        attr = n.split(".", 1)[1]
        if pkg is not None and getattr(pkg, attr, None) is not real:
            setattr(pkg, attr, real)
