"""S-R5 coldstart P6 (2026-05-31) — top_ngrams_by_author parallel I/O.

Probe P6 «слова викторианцев 1837-1901» routes to
`top_ngrams_by_author(author_regex='.*', year_from=1837, year_to=1901)`.
The E6 cap (test_sr5_e6_period_vocab_book_cap.py) bounds the scan to
6000 books, but the per-book token READ was still SERIAL. Warm that scan
is 11s (OS page cache hot); COLD (post `--force-recreate`, empty page
cache) it ran 180s and timed the probe HTTP request out → FAIL. The
169s gap is pure disk-read latency, done one file at a time.

Fix mirrors word_collocates, which already threads the identical
read-loop (rag_tools.py ~1091): file open + read() releases the GIL, so
I/O overlaps across workers and cold wall time drops to ~I/O/8.

These tests pin the fix WITHOUT the real corpus:

  * perf-guard (FAILS pre-fix): a >16-book scan dispatches the per-book
    work through a ThreadPoolExecutor — the serial pre-fix code never
    instantiated one.
  * negative: a small scope (≤16 books) stays SERIAL — no pool overhead
    when there's nothing to parallelise.
  * output-preserving (the re-record question): threaded and serial
    execution produce a byte-identical `top`. ThreadPoolExecutor.map
    yields in INPUT order, so local counters merge in the same order as
    the serial walk → identical Counter insertion order → identical
    most_common() tie-breaking. This is WHY the parallelisation needs no
    content re-record (unlike the E6 cap, which changed which books are
    scanned).

R5 (CLAUDE.md): the query that timed out becomes a named guard, with a
negative case proving the pool doesn't fire on ordinary scopes.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import concurrent.futures as _cf  # noqa: E402

import pandas as pd  # noqa: E402
import scripts.rag_tools as rt  # noqa: E402


def _period_frame(n_books: int) -> pd.DataFrame:
    rows = []
    for i in range(n_books):
        rows.append({
            "id": f"PG{1000 + i}", "author": f"Victorian, A{i}",
            "title": f"Work {i}", "downloads": i,
            "authoryearofbirth": 1840, "authoryearofdeath": 1900,
            "language": "en",
        })
    return pd.DataFrame(rows)


class _SerialMapExecutor:
    """Drop-in ThreadPoolExecutor stand-in whose .map runs synchronously
    in the calling thread — lets us compare threaded vs serial output
    without a timing dependency."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, it):
        return list(map(fn, it))


class TopNgramsParallelScan(unittest.TestCase):
    def setUp(self):
        self._orig_select = rt._select_books
        self._orig_tokens = rt._tokens_path
        self._orig_tpe = _cf.ThreadPoolExecutor
        self.pool_calls: list = []

        def _spy_tpe(*a, **k):
            self.pool_calls.append(k.get("max_workers"))
            return self._orig_tpe(*a, **k)

        _cf.ThreadPoolExecutor = _spy_tpe

    def tearDown(self):
        rt._select_books = self._orig_select
        rt._tokens_path = self._orig_tokens
        _cf.ThreadPoolExecutor = self._orig_tpe

    def test_large_scan_uses_threadpool(self):
        """PERF-GUARD: a >16-book scan dispatches through a
        ThreadPoolExecutor(max_workers=8). Pre-fix (serial loop) never
        instantiated a pool → pool_calls would be empty."""
        rt._select_books = lambda *a, **k: _period_frame(40)
        rt._tokens_path = lambda pg: Path(f"/nonexistent/{pg}.tokens")
        rt.top_ngrams_by_author(author_regex=".*", n=1,
                                year_from=1837, year_to=1901)
        self.assertEqual(len(self.pool_calls), 1,
                         "large scan must use exactly one ThreadPoolExecutor")
        self.assertEqual(self.pool_calls[0], 8,
                         "I/O pool should run 8 workers (mirrors word_collocates)")

    def test_small_scan_stays_serial(self):
        """NEGATIVE: a ≤16-book scope must NOT spin up a pool — no
        parallel overhead when there's nothing to parallelise."""
        rt._select_books = lambda *a, **k: _period_frame(10)
        rt._tokens_path = lambda pg: Path(f"/nonexistent/{pg}.tokens")
        rt.top_ngrams_by_author(author_regex="^Austen,", n=2)
        self.assertEqual(self.pool_calls, [],
                         "small scope must stay serial (no ThreadPoolExecutor)")

    def test_threaded_output_matches_serial(self):
        """OUTPUT-PRESERVING (re-record question): threaded and serial
        execution produce identical `top`. Backs the claim that the
        parallelisation needs NO content re-record."""
        tmp = Path(self._mktmp())
        # 20 books (> 16 → threaded), deterministic token content with
        # ties so tie-breaking order is actually exercised.
        vocab = ["alpha", "bravo", "charlie", "delta", "echo"]
        for i in range(20):
            f = tmp / f"PG{1000 + i}.tokens"
            # rotate the word list per book so counts tie across books
            words = (vocab[i % len(vocab):] + vocab[:i % len(vocab)]) * 3
            f.write_text("\n".join(words) + "\n", encoding="utf-8")

        rt._select_books = lambda *a, **k: _period_frame(20)
        rt._tokens_path = lambda pg: tmp / f"{pg}.tokens"

        out_threaded = rt.top_ngrams_by_author(author_regex=".*", n=1, top=10)

        # force the serial path by swapping the executor for a synchronous
        # map shim, then run the identical inputs.
        _cf.ThreadPoolExecutor = _SerialMapExecutor
        out_serial = rt.top_ngrams_by_author(author_regex=".*", n=1, top=10)

        self.assertEqual(out_threaded["top"], out_serial["top"],
                         "threaded and serial scans must yield identical top "
                         "(order included) — else a re-record would be needed")
        self.assertEqual(out_threaded["total_ngrams"],
                         out_serial["total_ngrams"])
        self.assertEqual(out_threaded["books_used"], out_serial["books_used"])

    def _mktmp(self) -> str:
        import tempfile
        d = tempfile.mkdtemp(prefix="wc_topngrams_")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main(verbosity=2)
