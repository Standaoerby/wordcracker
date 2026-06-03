"""S-E11 (2026-06-03) — retrieval-store page-cache warmup helper.

P11 cold latency (79-294s, diagnosed on prod 2026-06-03) is cold OS
page-cache on the find_book_by_topic retrieval path (FTS5 + Chroma +
per-candidate chunk reads), NOT BGE compute or model load (model was
GPU-resident; latency collapsed 294→79→51.7→7s as the page cache warmed
across queries). `chat_server._warm_page_cache` read-and-discards the whole
retrieval store at startup so the FIRST cache-miss (incl. the deploy probe,
whose AST-invalidated cache forces a miss on an arbitrary topic) is warm.

This guard pins the helper's contract: reads every file under the given
paths, tolerates missing paths, never raises, returns total bytes read.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))


class PageCacheWarm(unittest.TestCase):
    def setUp(self):
        try:
            import scripts.chat_server as cs  # noqa: F401
        except Exception as e:  # pragma: no cover - env without runtime deps
            self.skipTest(f"chat_server import unavailable here: {e}")
        self.cs = cs

    def test_reads_all_bytes_and_tolerates_missing(self, tmp_root=None):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            # a flat file + a nested dir with two files
            f1 = os.path.join(d, "a.bin")
            open(f1, "wb").write(b"x" * 1000)
            sub = os.path.join(d, "sub")
            os.makedirs(sub)
            open(os.path.join(sub, "b.bin"), "wb").write(b"y" * 2000)
            open(os.path.join(sub, "c.bin"), "wb").write(b"z" * 500)

            seen = []
            total = self.cs._warm_page_cache(
                [f1, d, "/nonexistent/path/xyz"],
                "test store",
                log=lambda m: seen.append(m),
            )
            # f1 counted once directly (1000) + walk of d picks up a.bin(1000)
            # + b.bin(2000) + c.bin(500) = 3500 → total 4500. Missing path is
            # silently skipped (no raise).
            self.assertEqual(total, 1000 + 3500)
            self.assertTrue(seen and "page-cache warmed" in seen[0])

    def test_missing_only_returns_zero_no_raise(self):
        total = self.cs._warm_page_cache(["/nope/aaa", "/nope/bbb"], "x")
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
