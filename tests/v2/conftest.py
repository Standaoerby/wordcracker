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
