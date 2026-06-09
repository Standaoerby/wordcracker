"""B-LW-corpus negative test (Stan 2026-06-09).

Regression: learning_words({"author": ".*", "country": "GB"}) returned []
-> DataValidity.BROKEN -> user saw the "no data" surface.

Two root causes:
  1. v1 called `_select_books(scope["author"])` and SILENTLY DROPPED the
     `country` / year keys, so {"author": ".*", "country": "GB"} collapsed
     from "British authors" to the entire corpus.
  2. On a whole-corpus scope (sc == cc for every word) the author-uniqueness
     guard `cc < min_corpus_ratio * sc` dropped 100% of candidates.

This file pins cause #1 at the unit level WITHOUT corpus data: it spies on
`_select_books` and asserts the country arg is forwarded. It FAILS on the
pre-fix source (country never passed -> recorded as None) and PASSES after.

R2 compliance: negative test that is red on pre-fix code, green on post-fix.
Cause #2 (ratio-guard bypass on whole-corpus) is exercised by the live
predeploy smoke on Server-on-Wheels - see the bug journal entry.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# top-level import: learning_tools does `from rag_tools import ...`, which needs
# scripts/ on sys.path (same pattern as tests/run_tests.py / test_all_tools.py).
import learning_tools as lt


class _ZeroLenSelection:
    """Stand-in for the empty _select_books DataFrame (len 0 -> early return),
    so the test needs no pandas data, no counts files, no band-pass machinery."""

    def __len__(self) -> int:
        return 0


class CountryForwardedToSelectBooks(unittest.TestCase):
    def test_country_is_forwarded_not_dropped(self):
        seen = {}

        def _spy(author_regex, lang="en", year_from=None, year_to=None,
                 country=None):
            seen["author_regex"] = author_regex
            seen["country"] = country
            seen["year_from"] = year_from
            seen["year_to"] = year_to
            return _ZeroLenSelection()

        with patch.object(lt, "_select_books", _spy):
            out = lt.learning_words(
                {"author": ".*", "country": "GB"},
                level="intermediate", top=10,
            )

        # The regression: pre-fix this is None because country was dropped.
        self.assertEqual(
            seen.get("country"), "GB",
            msg="country must be forwarded to _select_books (B-LW-corpus #1)",
        )
        # With a 0-book selection the tool returns the explicit error rather
        # than silently aggregating the whole corpus.
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("error"), "no books matched")

    def test_year_range_is_forwarded(self):
        seen = {}

        def _spy(author_regex, lang="en", year_from=None, year_to=None,
                 country=None):
            seen["year_from"] = year_from
            seen["year_to"] = year_to
            return _ZeroLenSelection()

        with patch.object(lt, "_select_books", _spy):
            lt.learning_words(
                {"author": ".*", "year_from": 1837, "year_to": 1901},
                level="intermediate", top=10,
            )

        self.assertEqual(seen.get("year_from"), 1837)
        self.assertEqual(seen.get("year_to"), 1901)


if __name__ == "__main__":
    unittest.main()
