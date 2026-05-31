"""S-R5 E6 (2026-05-31) — period_vocab full-corpus scan hard-cap.

Probe P6 «слова викторианцев 1837-1901» routes to
`top_ngrams_by_author(author_regex='.*', year_from=1837, year_to=1901)`.
`_select_books` pushes the year-range down correctly, but the Victorian
subset is still ~the whole en corpus (word_collocates documents ~27860
books for the same period), and the per-book full-token scan was
UNCAPPED → 180s probe timeout (warm AND cold).

Fix mirrors the cap word_collocates already carries: sample books by
downloads desc and scan at most `max_books`. These tests pin the cap
behavior WITHOUT the real corpus:

  * perf-guard (FAILS pre-fix): a full-corpus period selection is
    truncated to `max_books` before the token scan — the scan never
    touches more than the cap.
  * download-ranking: the kept books are the most-downloaded ones (most
    evidence → representative era vocabulary).
  * negative / no-over-cap (correctness): a narrow author scope below
    the cap (e.g. 18 books) is scanned in full and reports
    books_capped=False — the cap must not truncate normal queries.

R2/R5 (CLAUDE.md): the query that timed out becomes a named guard, with
a negative case proving the cap doesn't fire on ordinary scopes.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import pandas as pd  # noqa: E402
import scripts.rag_tools as rt  # noqa: E402


def _period_frame(n_books: int) -> pd.DataFrame:
    """`n_books` synthetic Victorian-era books with strictly increasing
    download counts, so the highest ids are the most-downloaded."""
    rows = []
    for i in range(n_books):
        rows.append({
            "id": f"PG{1000 + i}", "author": f"Victorian, A{i}",
            "title": f"Work {i}", "downloads": i,  # PG(1000+n-1) most popular
            "authoryearofbirth": 1840, "authoryearofdeath": 1900,
            "language": "en",
        })
    return pd.DataFrame(rows)


class PeriodVocabBookCap(unittest.TestCase):
    def setUp(self):
        # Record which books the scan actually asked tokens for, and force
        # every tokens path to "not exist" so the heavy scan body is a
        # no-op — we only assert WHICH books survived the cap.
        self.requested: list[str] = []
        self._orig_select = rt._select_books
        self._orig_tokens = rt._tokens_path

        def _recording_tokens_path(pg):
            self.requested.append(pg)
            return Path(f"/nonexistent/{pg}.tokens")  # .exists() → False

        rt._tokens_path = _recording_tokens_path

    def tearDown(self):
        rt._select_books = self._orig_select
        rt._tokens_path = self._orig_tokens

    def test_full_corpus_period_scan_is_capped(self):
        """PERF-GUARD (behavioral, default cap): a Victorian selection
        larger than the shipped cap is bounded to it — the scan never
        touches the whole corpus. Pre-fix (no cap) iterated all 6001
        matched books → this assertion fails on time AND on count."""
        rt._select_books = lambda *a, **k: _period_frame(6001)
        out = rt.top_ngrams_by_author(author_regex=".*", n=1,
                                      year_from=1837, year_to=1901)
        self.assertLessEqual(
            len(self.requested), 6000,
            f"scan touched {len(self.requested)} books — full-corpus "
            f"period query is not bounded by the cap",
        )
        self.assertEqual(out["books_matched"], 6001)
        self.assertTrue(out["books_capped"])
        self.assertEqual(out["max_books"], 6000)

    def test_cap_keeps_most_downloaded_books(self):
        """The surviving books are the most-downloaded ones — that's what
        makes the capped era vocabulary representative."""
        rt._select_books = lambda *a, **k: _period_frame(50)
        rt.top_ngrams_by_author(author_regex=".*", n=1,
                                year_from=1837, year_to=1901, max_books=10)
        # downloads == numeric suffix; top-10 by downloads are PG1040..PG1049.
        kept = set(self.requested)
        self.assertEqual(kept, {f"PG{1040 + i}" for i in range(10)})

    def test_narrow_scope_below_cap_not_truncated(self):
        """NEGATIVE / correctness: an ordinary author scope below the cap
        is scanned in full and reports books_capped=False. The cap must
        not fire on normal queries (no silent truncation of e.g. Austen)."""
        rt._select_books = lambda *a, **k: _period_frame(18)
        out = rt.top_ngrams_by_author(author_regex="^Austen,", n=2,
                                      max_books=6000)
        self.assertEqual(len(self.requested), 18,
                         "narrow scope must be scanned in full")
        self.assertFalse(out["books_capped"])
        self.assertEqual(out["books_matched"], 18)

    def test_default_cap_is_6000(self):
        """The default cap is the shipped value — a regression here would
        silently change prod latency/coverage."""
        import inspect
        sig = inspect.signature(rt.top_ngrams_by_author)
        self.assertEqual(sig.parameters["max_books"].default, 6000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
