"""S-E11 (2026-06-03) — cache-hit runtime_ms phantom fix.

A cache HIT returns the stored ToolResult, whose `runtime_ms` is the
ORIGINAL cold-compute stamp from when the entry was first written. Before
this fix, dispatch() returned it verbatim → a 19 s cache-hit request
reported tool runtime_ms=156_433 (the 156 s cold compute), poisoning the
admin dashboard, feedback.py, bench_v2, and the human-readable tool_calls.

`ToolResult.with_cache_hit(True)` is the single chokepoint both cache_get
paths (LRU + disk) funnel through, so zeroing runtime_ms there fixes every
hit. The probe itself measures wall-clock (not runtime_ms), so this is an
observability fix, independent of the cold-start latency work.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scripts.v2._types import ToolResult  # noqa: E402


class CacheHitRuntimeMs(unittest.TestCase):
    def _result(self, runtime_ms: int) -> ToolResult:
        r = ToolResult.success(tool="find_book_by_topic", data={"matches": []})
        r.runtime_ms = runtime_ms
        return r

    def test_hit_zeros_phantom_runtime_ms(self):
        r = self._result(156_433).with_cache_hit(True)
        self.assertTrue(r.cache_hit)
        self.assertEqual(r.runtime_ms, 0,
                         "cache hit must not surface the cold-compute stamp")

    def test_miss_marker_preserves_runtime_ms(self):
        # with_cache_hit(False) is the explicit not-a-hit marker; it must
        # leave a genuinely-measured runtime untouched.
        r = self._result(4_200).with_cache_hit(False)
        self.assertFalse(r.cache_hit)
        self.assertEqual(r.runtime_ms, 4_200)

    def test_default_arg_is_hit(self):
        r = self._result(99_999).with_cache_hit()
        self.assertTrue(r.cache_hit)
        self.assertEqual(r.runtime_ms, 0)


if __name__ == "__main__":
    unittest.main()
