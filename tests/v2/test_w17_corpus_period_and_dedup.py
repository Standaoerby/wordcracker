"""W-17 (Phase 5 P2 polish, 2026-05-23) — corpus_meta period coverage
+ edition-dedup hardening.

Before:
  * «какой период охватывает корпус» bounced to clarify because
    corpus_overview never surfaced min/max years.
  * Frankenstein leaked as 5 PG-ids and Hard Times as 2 across
    find_book_by_topic result lists — dedup_book_editions's title
    normalizer only stripped «;»/«,»-delimited subtitles, missed
    parentheticals («(1818 edition)») and bare «Or»/«For these times».
  * «Авторов: 0» rendered when the tool couldn't compute author count
    (dev box without /workspace) — misleading.

After:
  * corpus_meta_snapshot carries year_min/year_max + year_basis fields
    and the renderer adds a «Период охвата» row when both are present.
  * _normalize_book_title strips parenthetical edition tags and bare
    «Or <subtitle>» / «For These Times» tails, collapsing all 5
    Frankenstein editions and both Hard Times editions to one row.
  * «Авторов» row is suppressed when n_authors == 0.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest.mock as mock

from scripts.v2.tools._result_filters import (
    _normalize_book_title, dedup_book_editions,
)
from scripts.v2 import view_builders as vb
from scripts.v2 import template_executor


class W17NormalizeBookTitle(unittest.TestCase):

    def test_frankenstein_editions_collapse_to_one_key(self):
        forms = [
            "Frankenstein",
            "Frankenstein; Or, The Modern Prometheus",
            "Frankenstein, or The Modern Prometheus",
            "Frankenstein (1818 edition)",
            "Frankenstein Or The Modern Prometheus",  # bare Or, no separator
        ]
        keys = {_normalize_book_title(t) for t in forms}
        self.assertEqual(
            keys, {"frankenstein"},
            f"All Frankenstein editions should normalize to one key; "
            f"got {keys}",
        )

    def test_hard_times_editions_collapse(self):
        forms = [
            "Hard Times",
            "Hard Times — For These Times",
            "Hard Times For These Times",        # bare, no separator
            "Hard Times (Illustrated)",
        ]
        keys = {_normalize_book_title(t) for t in forms}
        self.assertEqual(
            keys, {"hard times"},
            f"Hard Times editions should collapse; got {keys}",
        )

    def test_pride_and_prejudice_illustrated(self):
        self.assertEqual(
            _normalize_book_title("Pride and Prejudice (Illustrated)"),
            _normalize_book_title("Pride and Prejudice"),
        )

    def test_moby_dick_legacy_form_still_works(self):
        # Legacy «;» path must keep collapsing
        self.assertEqual(
            _normalize_book_title("Moby Dick; Or, The Whale"),
            _normalize_book_title("Moby Dick"),
        )

    def test_dedup_book_editions_frankenstein(self):
        rows = [
            {"title": "Frankenstein", "author": "Shelley, Mary Wollstonecraft",
             "downloads": 1000, "pg_id": "PG84"},
            {"title": "Frankenstein; Or, The Modern Prometheus",
             "author": "Shelley, Mary Wollstonecraft",
             "downloads": 800, "pg_id": "PG41445"},
            {"title": "Frankenstein (1818 edition)",
             "author": "Shelley, Mary Wollstonecraft",
             "downloads": 200, "pg_id": "PG20000"},
            {"title": "Frankenstein Or The Modern Prometheus",
             "author": "Shelley, Mary Wollstonecraft",
             "downloads": 50, "pg_id": "PG30000"},
            {"title": "Frankenstein (Illustrated)",
             "author": "Shelley, Mary Wollstonecraft",
             "downloads": 10, "pg_id": "PG40000"},
        ]
        out, dropped = dedup_book_editions(rows)
        self.assertEqual(len(out), 1,
                          f"Expected 1 Frankenstein row, got {len(out)}: "
                          f"{[r['title'] for r in out]}")
        self.assertEqual(dropped, 4)
        # Keeps the highest-downloads row
        self.assertEqual(out[0]["pg_id"], "PG84")


class W17CorpusMetaPeriod(unittest.TestCase):

    def test_view_builder_carries_period_fields(self):
        view = vb.build_corpus_meta_snapshot(
            n_books=100, n_authors=12, n_tokens=1_000_000,
            spgc_baseline="SPGC-2018-07-18",
            year_min=1700, year_max=1924,
            year_basis="authoryearofbirth+30 proxy (75,000 books)",
        )
        self.assertEqual(view.payload["year_min"], 1700)
        self.assertEqual(view.payload["year_max"], 1924)
        self.assertIn("authoryearofbirth", view.payload["year_basis"])

    def test_render_period_row_present_when_both_bounds_set(self):
        view = vb.build_corpus_meta_snapshot(
            n_books=100, n_authors=12, n_tokens=1_000_000,
            spgc_baseline="SPGC-2018-07-18",
            year_min=1700, year_max=1924,
            year_basis="pub_year + birth+30 proxy",
        )
        out = template_executor._render_corpus_meta_snapshot(view)
        self.assertIn("Период охвата", out)
        self.assertIn("1700", out)
        self.assertIn("1924", out)
        # Basis caveat surfaced
        self.assertIn("Период оценён по", out)

    def test_render_omits_period_row_when_bounds_missing(self):
        view = vb.build_corpus_meta_snapshot(
            n_books=100, n_authors=12, n_tokens=1_000_000,
            spgc_baseline="SPGC-2018-07-18",
        )  # no year_min / year_max
        out = template_executor._render_corpus_meta_snapshot(view)
        self.assertNotIn("Период охвата", out,
                          "Period row must be suppressed when bounds missing")

    def test_render_omits_authors_row_when_zero(self):
        """W-17 acceptance: when tool didn't compute n_authors (dev box,
        missing /workspace), the «Авторов: 0» row used to mislead users
        into thinking the corpus was empty. Now it's suppressed."""
        view = vb.build_corpus_meta_snapshot(
            n_books=100, n_authors=0, n_tokens=1_000_000,
            spgc_baseline="SPGC-2018-07-18",
        )
        out = template_executor._render_corpus_meta_snapshot(view)
        self.assertNotIn("Авторов", out)

    def test_render_keeps_authors_row_when_nonzero(self):
        view = vb.build_corpus_meta_snapshot(
            n_books=100, n_authors=12, n_tokens=1_000_000,
            spgc_baseline="SPGC-2018-07-18",
        )
        out = template_executor._render_corpus_meta_snapshot(view)
        self.assertIn("Авторов", out)
        self.assertIn("12", out)


class W17FindBookEndToEndDedup(unittest.TestCase):
    """W-17 acceptance — «найди Frankenstein» / «найди Hard Times»
    no longer return 5/2 PG-ids respectively. The dedup must run on
    the find_book wrapper itself, not only via _normalize_book_title
    in isolation."""

    def test_find_book_collapses_frankenstein_editions(self):
        from scripts.v2.tools.books.find_book import find_book
        v1_raw = {
            "title_query": "Frankenstein",
            "author_filter": None,
            "total_matches": 5,
            "matches": [
                {"id": "PG84", "title": "Frankenstein",
                 "author": "Shelley, Mary Wollstonecraft",
                 "downloads": 30_000, "language": "en",
                 "authoryearofbirth": 1797, "pub_year": 1818,
                 "pg_id": "PG84"},
                {"id": "PG41445",
                 "title": "Frankenstein; Or, The Modern Prometheus",
                 "author": "Shelley, Mary Wollstonecraft",
                 "downloads": 5_000, "language": "en",
                 "authoryearofbirth": 1797, "pub_year": 1818,
                 "pg_id": "PG41445"},
                {"id": "PG20000",
                 "title": "Frankenstein (1818 edition)",
                 "author": "Shelley, Mary Wollstonecraft",
                 "downloads": 1_200, "language": "en",
                 "authoryearofbirth": 1797, "pub_year": 1818,
                 "pg_id": "PG20000"},
                {"id": "PG30000",
                 "title": "Frankenstein Or The Modern Prometheus",
                 "author": "Shelley, Mary Wollstonecraft",
                 "downloads": 800, "language": "en",
                 "authoryearofbirth": 1797, "pub_year": 1818,
                 "pg_id": "PG30000"},
                {"id": "PG40000",
                 "title": "Frankenstein (Illustrated)",
                 "author": "Shelley, Mary Wollstonecraft",
                 "downloads": 200, "language": "en",
                 "authoryearofbirth": 1797, "pub_year": 1818,
                 "pg_id": "PG40000"},
            ],
        }
        with mock.patch("scripts.rag_tools.find_book", return_value=v1_raw):
            r = find_book(title="Frankenstein", top=10)
        self.assertTrue(r.ok)
        ids = [m["id"] for m in r.data["matches"]]
        self.assertEqual(ids, ["PG84"],
                          f"W-17 acceptance: Frankenstein must collapse to "
                          f"one PG id (highest downloads). Got {ids}")
        # Warning surfaced so renderer can disclose
        codes = [w.code for w in r.warnings]
        self.assertIn("edition_dedup", codes)

    def test_find_book_collapses_hard_times_editions(self):
        from scripts.v2.tools.books.find_book import find_book
        v1_raw = {
            "title_query": "Hard Times",
            "author_filter": None,
            "total_matches": 2,
            "matches": [
                {"id": "PG786", "title": "Hard Times",
                 "author": "Dickens, Charles",
                 "downloads": 8_000, "language": "en",
                 "authoryearofbirth": 1812, "pg_id": "PG786"},
                {"id": "PG34989",
                 "title": "Hard Times — For These Times",
                 "author": "Dickens, Charles",
                 "downloads": 1_500, "language": "en",
                 "authoryearofbirth": 1812, "pg_id": "PG34989"},
            ],
        }
        with mock.patch("scripts.rag_tools.find_book", return_value=v1_raw):
            r = find_book(title="Hard Times", top=10)
        self.assertTrue(r.ok)
        ids = [m["id"] for m in r.data["matches"]]
        self.assertEqual(ids, ["PG786"],
                          f"W-17: Hard Times must collapse to one PG id. "
                          f"Got {ids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
