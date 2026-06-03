"""S-P2c-followup (E6/P6) — warm_period_tokens page-cache touch-warm.

period_vocab («слова викторианцев 1837-1901») scans ~6000 token files; cold
(empty page cache) that is 180s → probe TimeoutError. `warm_period_tokens`
pre-reads the SAME files into the OS page cache at startup, computing no
n-grams and caching no result. These tests pin the selection/cap behaviour
with the corpus mocked out (the clone has no corpus).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


class WarmPeriodTokens(unittest.TestCase):
    def setUp(self):
        try:
            import rag_tools as rt
            import pandas as pd
        except Exception as e:  # pragma: no cover - env without runtime deps
            self.skipTest(f"rag_tools/pandas unavailable: {e}")
        self.rt = rt
        self.pd = pd
        self._orig_sel = rt._select_books
        self._orig_path = rt._tokens_path

    def tearDown(self):
        if hasattr(self, "rt"):
            self.rt._select_books = self._orig_sel
            self.rt._tokens_path = self._orig_path

    def _wire(self, td, ids, downloads):
        df = self.pd.DataFrame({"id": ids, "downloads": downloads})
        self.rt._select_books = lambda *a, **k: df
        self.rt._tokens_path = lambda pg: Path(td) / f"{pg}.txt"
        return df

    def test_reads_all_selected_files(self):
        with tempfile.TemporaryDirectory() as td:
            ids = ["PG1", "PG2", "PG3"]
            for i in ids:
                (Path(td) / f"{i}.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            self._wire(td, ids, [10, 5, 1])
            n = self.rt.warm_period_tokens(year_from=1837, year_to=1901)
            self.assertEqual(n, 3)

    def test_cap_keeps_top_by_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            ids = [f"PG{i}" for i in range(5)]
            for i in ids:
                (Path(td) / f"{i}.txt").write_text("x\n", encoding="utf-8")
            self._wire(td, ids, [1, 2, 3, 4, 5])
            reads = []
            base = self.rt._tokens_path
            self.rt._tokens_path = lambda pg: (reads.append(pg) or base(pg))
            n = self.rt.warm_period_tokens(max_books=2)
            self.assertEqual(n, 2, "cap reads only max_books files")
            self.assertEqual(set(reads), {"PG4", "PG3"},
                             "cap must keep the top-2 by downloads")

    def test_missing_files_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            self._wire(td, ["PG1", "PG2"], [2, 1])
            (Path(td) / "PG1.txt").write_text("a\n", encoding="utf-8")  # PG2 absent
            n = self.rt.warm_period_tokens()
            self.assertEqual(n, 1)

    def test_empty_selection_returns_zero(self):
        self.rt._select_books = lambda *a, **k: self.pd.DataFrame(
            {"id": [], "downloads": []})
        self.assertEqual(self.rt.warm_period_tokens(), 0)


if __name__ == "__main__":
    unittest.main()
