"""S-R5 / E1 — author_metadata.sample_titles must rank by downloads.

PROBE E1 (deploy 408874e — «какие книги у Уэллса»): the answer didn't
mention any canonical Wells title (Time Machine / War of the Worlds /…).

MECHANISM (confirmed by code reading): author_lookup routes to
`rag_tools.author_metadata`, whose `sample_titles` was
`sel["title"].head(10)`. `_select_books` returns rows in metadata-CSV
(≈PG-id) order with NO popularity ranking — so for an author with many
catalogued works the 10-title sample was popularity-blind and could omit
the famous works the user actually means.

FIX: rank `sel` by downloads before taking head(10), so the most
downloaded (≈most canonical) titles surface deterministically.

This is a data-layer negative test on a synthetic metadata frame
(corpus-free): pre-fix `sample_titles` = the first 10 rows by order
(all obscure); post-fix = the top-downloaded titles. The full end-to-end
probe (live corpus + LLM render) is verified by the re-run probe-gate on
deploy.
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


def _synthetic_wells_frame() -> pd.DataFrame:
    """10 obscure low-download works FIRST (by row order), then the three
    canonical high-download novels. Pre-fix head(10) would surface only
    the obscure rows; post-fix download-ranking surfaces the canon."""
    rows = []
    for i in range(10):
        rows.append({
            "id": f"PG{2000 + i}", "author": "Wells, H. G.",
            "title": f"Obscure Tract {i}", "downloads": 5 + i,
            "authoryearofbirth": 1866, "authoryearofdeath": 1946,
            "language": "en",
        })
    for title, dl in [("The Time Machine", 99000),
                      ("The War of the Worlds", 95000),
                      ("The Invisible Man", 80000)]:
        rows.append({
            "id": "PG35", "author": "Wells, H. G.", "title": title,
            "downloads": dl, "authoryearofbirth": 1866,
            "authoryearofdeath": 1946, "language": "en",
        })
    return pd.DataFrame(rows)


class SampleTitlesRankedByDownloads(unittest.TestCase):
    def setUp(self):
        self._orig = rt._select_books
        rt._select_books = lambda *a, **k: _synthetic_wells_frame()

    def tearDown(self):
        rt._select_books = self._orig

    def test_canonical_titles_surface_in_sample(self):
        out = rt.author_metadata("^Wells, H")
        titles = out["sample_titles"]
        # The canonical works — popularity-blind head(10) on the synthetic
        # frame would NOT include any of these (they are rows 10-12).
        self.assertIn("The Time Machine", titles)
        self.assertIn("The War of the Worlds", titles)
        self.assertIn("The Invisible Man", titles)

    def test_sample_is_download_ordered(self):
        out = rt.author_metadata("^Wells, H")
        titles = out["sample_titles"]
        # Most-downloaded first.
        self.assertEqual(titles[0], "The Time Machine")
        self.assertEqual(titles[1], "The War of the Worlds")
        self.assertEqual(titles[2], "The Invisible Man")

    def test_other_fields_unaffected(self):
        out = rt.author_metadata("^Wells, H")
        # Ranking changes only the sample order, not the aggregates.
        self.assertEqual(out["books_matched"], 13)
        self.assertEqual(out["year_of_birth_min"], 1866)
        self.assertEqual(len(out["sample_titles"]), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
