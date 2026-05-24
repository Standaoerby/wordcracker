"""Cache _write_disk race-fix — negative test per R2.

Prod 2026-05-24: two concurrent ThreadingHTTPServer threads completed
the same `top_ngrams_by_author` query simultaneously
(`done in 3346.75s` and `done in 3747.57s` at the same wall-clock
second). The pre-fix code computed `tmp = p.with_suffix(".tmp")` —
a fixed name per cache_key — so both threads targeted the same .tmp.

Race trace:
    T_A: write_text(.tmp)   ← writes payload_A
    T_B: write_text(.tmp)   ← overwrites with payload_B
    T_A: replace(.tmp → .json)   ← succeeds, cached bytes are B's
    T_B: replace(.tmp → .json)   ← ENOENT, source .tmp already renamed

Two failure modes:
  (a) Loser thread logs `cache write failed [Errno 2]`; the entry was
      written by the winner but with the LOSER's race-overlapped data.
  (b) Both writers overlap mid-write_text → JSON corruption inside the
      .tmp before the winner's replace renames it. Subsequent reads
      raise JSONDecodeError and cache_get returns None silently.

Fix: tempfile.NamedTemporaryFile(delete=False) gives each writer a
unique name in the same directory; the final replace() is independent
per writer. This test should FAIL on pre-fix code (assertNoLogs
catches the warning) and PASS on post-fix.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import cache as cache_mod
from scripts.v2._types import Coverage, SourceInfo, ToolResult


def _make_result(payload_size: int) -> ToolResult:
    return ToolResult.success(
        tool="_race_probe",
        data={"rows": [{"ngram": f"w_{i}", "count": i}
                       for i in range(payload_size)]},
        coverage=Coverage(books_matched=10, books_total=10),
        source_info=SourceInfo(corpus_version="v1", analytics_version="v1"),
        query={"author_regex": "^Test, Author"},
    )


class CacheWriteDiskRaceSafe(unittest.TestCase):
    """Concurrent writers for the same cache key must not race on the
    .tmp file. Pre-fix code shared a single .tmp name across writers —
    the loser logged `cache write failed`. Post-fix: each writer gets
    a unique .tmp via NamedTemporaryFile, all writes succeed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_root = cache_mod.CACHE_ROOT
        cache_mod.CACHE_ROOT = Path(self.tmp.name)
        cache_mod.cache_clear()

    def tearDown(self):
        cache_mod.CACHE_ROOT = self._saved_root
        cache_mod.cache_clear()
        self.tmp.cleanup()

    def test_concurrent_writes_same_key_no_warnings(self):
        # Payload large enough that write_text takes long enough for
        # the GIL-released IO to interleave writers. 50k rows ≈ 1-2 MB
        # JSON; in pre-fix code this is the failure window.
        result = _make_result(payload_size=50_000)
        fingerprint = {"author_regex": "^Test, Author", "n": 1}

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def writer():
            try:
                barrier.wait()  # release all 4 threads at once
                cache_mod.cache_put("_race_probe", fingerprint, result)
            except BaseException as e:
                errors.append(e)

        with self.assertNoLogs("wordcracker.v2.cache", level="WARNING"):
            threads = [threading.Thread(target=writer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [], f"writer raised: {errors}")

        # Entry must be readable after the race.
        cache_mod.cache_clear()  # force disk read
        hit = cache_mod.cache_get("_race_probe", fingerprint)
        self.assertIsNotNone(hit, "cache miss after concurrent writes")
        self.assertTrue(hit.ok)
        self.assertEqual(len(hit.data["rows"]), 50_000)

    def test_concurrent_writes_distinct_keys_independent(self):
        """Sanity check: distinct keys never raced even pre-fix, but the
        post-fix code must keep that property. Each thread writes to a
        different key; all should succeed."""
        result = _make_result(payload_size=5_000)
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def writer(i: int):
            try:
                barrier.wait()
                fp = {"author_regex": f"^A{i}", "n": 1}
                cache_mod.cache_put("_race_probe", fp, result)
            except BaseException as e:
                errors.append(e)

        with self.assertNoLogs("wordcracker.v2.cache", level="WARNING"):
            threads = [threading.Thread(target=writer, args=(i,))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [], f"writer raised: {errors}")
        for i in range(4):
            hit = cache_mod.cache_get("_race_probe",
                                       {"author_regex": f"^A{i}", "n": 1})
            self.assertIsNotNone(hit, f"miss for key i={i}")
            self.assertEqual(len(hit.data["rows"]), 5_000)

    def test_no_dangling_tmp_files_after_writes(self):
        """Post-fix: .tmp file is renamed to .json on success. After a
        successful write, no .tmp lingers in the cache dir."""
        result = _make_result(payload_size=100)
        cache_mod.cache_put("_race_probe",
                              {"author_regex": "^Solo"}, result)

        # Walk the cache root, count .tmp files.
        tmp_files = list(Path(self.tmp.name).rglob("*.tmp"))
        self.assertEqual(tmp_files, [],
                          f"dangling .tmp after success: {tmp_files}")


if __name__ == "__main__":
    unittest.main()
