"""dispatch() timeout enforcement — v3.3.1.

Prod observation 2026-05-21: book_similar (find_book_by_topic single-step)
burned 5 minutes per call. tool_registry.ToolSpec.timeout_s existed
but was never enforced — `result = spec.fn(**args)` ran without any
timeout wrap. Closes Q25/Q114/Q105-class for SINGLE-step plans
(router inter-step budget doesn't trip on single-tool plans).

Fix: signal.alarm-based timeout in dispatch (Linux). On non-Unix
(Windows dev) the context is no-op so tests still pass — prod gets
enforcement.

Coverage:
  - default timeout = 60s
  - timeout exceeded → ToolResult.fail(err_type="timeout", retryable=True)
  - timeout NOT exceeded → normal success
  - timeout_s = 0 disables enforcement (legacy unbounded path)
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tool_registry as tr
from scripts.v2._types import ToolResult


_IS_LINUX = hasattr(__import__("signal"), "SIGALRM")


class TimeoutDefault(unittest.TestCase):
    def test_default_timeout_is_60s(self):
        """v3.3.1 bumped default 30→60 because author_metadata p95 was
        49s on cold cache in prod."""
        # @tool decorator default
        def _noop() -> ToolResult:
            return ToolResult.success(tool="x", data={})
        spec = tr.ToolSpec(
            name="probe", fn=_noop, category="corpus_meta",
            description="probe", input_schema={},
        )
        self.assertEqual(spec.timeout_s, 60)


class TimeoutEnforcement(unittest.TestCase):
    """Linux-only tests. Skip on Windows where signal.SIGALRM is absent."""

    def setUp(self):
        # Clean registry
        self._saved_registry = dict(tr.REGISTRY)

    def tearDown(self):
        tr.REGISTRY.clear()
        tr.REGISTRY.update(self._saved_registry)

    @unittest.skipUnless(_IS_LINUX, "signal.SIGALRM only on Unix")
    def test_slow_tool_times_out_via_signal(self):
        """Tool that sleeps past timeout_s should be killed with
        err_type='timeout'."""
        @tr.tool(
            name="slow_probe", category="corpus_meta",
            description="probe that sleeps",
            input_schema={"type": "object", "properties": {}, "required": []},
            timeout_s=1,                # 1 second cap
            cacheable=False,
        )
        def slow_probe() -> ToolResult:
            time.sleep(3.0)             # exceeds cap
            return ToolResult.success(tool="slow_probe", data={"ok": True})

        t0 = time.perf_counter()
        result = tr.dispatch("slow_probe", {})
        elapsed = time.perf_counter() - t0

        self.assertFalse(result.ok)
        self.assertEqual(result.error.type, "timeout")
        self.assertTrue(result.error.retryable)
        # Aborted near 1s, NOT 3s
        self.assertLess(elapsed, 2.5,
                         f"timeout enforcement didn't fire (elapsed {elapsed:.1f}s)")

    @unittest.skipUnless(_IS_LINUX, "signal.SIGALRM only on Unix")
    def test_fast_tool_completes_normally(self):
        """Tool that finishes well under timeout_s returns success
        unmodified."""
        @tr.tool(
            name="fast_probe", category="corpus_meta",
            description="quick probe",
            input_schema={"type": "object", "properties": {}, "required": []},
            timeout_s=2,
            cacheable=False,
        )
        def fast_probe() -> ToolResult:
            time.sleep(0.05)            # 50ms — well under cap
            return ToolResult.success(tool="fast_probe", data={"ok": True})

        result = tr.dispatch("fast_probe", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"ok": True})

    @unittest.skipUnless(_IS_LINUX, "signal.SIGALRM only on Unix")
    def test_timeout_zero_disables_enforcement(self):
        """timeout_s=0 → unbounded (legacy behaviour). Useful for
        intentionally long-running batch tools."""
        @tr.tool(
            name="unbounded_probe", category="corpus_meta",
            description="unbounded",
            input_schema={"type": "object", "properties": {}, "required": []},
            timeout_s=0,                # disabled
            cacheable=False,
        )
        def unbounded_probe() -> ToolResult:
            time.sleep(0.5)
            return ToolResult.success(tool="unbounded_probe", data={"done": 1})

        result = tr.dispatch("unbounded_probe", {})
        self.assertTrue(result.ok)


class WindowsDevPath(unittest.TestCase):
    """On Windows / non-Unix the timeout context is no-op. Tests pass
    by NOT crashing on signal API absence."""

    def test_dispatch_works_without_sigalrm(self):
        """No exception even if signal module lacks SIGALRM."""
        @tr.tool(
            name="dev_probe", category="corpus_meta",
            description="dev path",
            input_schema={"type": "object", "properties": {}, "required": []},
            timeout_s=10,
            cacheable=False,
        )
        def dev_probe() -> ToolResult:
            return ToolResult.success(tool="dev_probe", data={"ok": True})

        result = tr.dispatch("dev_probe", {})
        self.assertTrue(result.ok)
        # Cleanup
        del tr.REGISTRY["dev_probe"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
