"""Sprint 20 — Stan 2026-05-19 prod: basis='pub_year' coverage trap.

The v4 LLM planner enthusiastically picks `basis='pub_year'` for any
timeline-by-year query because the schema enum lists it and the
description suggests «real publication year» is more authoritative.

Reality: SPGC corpus has ~4 books with real pub_year enrichment from
Open Library — the rest fall through to default «2000-2024» bucket.
A query about 1880-1950 timeline gets one bucket with zero occurrences
and a renderer that honestly reports «не охватывают 1880-1950».

Fix layer A — catalog description warns the LLM to use basis='auto'.

Fix layer B — wrapper auto-fallback when pub_year returns sparse/zero
results. The user gets a meaningful timeline (via birth_plus_30) + a
ToolWarning explaining the fallback.

This test pins the fallback behavior so future schema additions don't
silently bypass it.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401


def _pub_year_sparse():
    """The actual prod data shape from Stan's screenshot:
    1 bucket, all-zero occurrences."""
    return {
        "word": "telephone",
        "bucket_years": 25,
        "basis": "pub_year",
        "axis_basis": "pub_year (Open Library, real publication year)",
        "timeline": [
            {"period": "2000-2024", "books": 3, "total_tokens": 8980,
             "occurrences": 0, "per_million": 0.0},
        ],
    }


def _birth_plus_30_rich():
    """The expected fallback shape — full corpus timeline."""
    return {
        "word": "telephone",
        "bucket_years": 25,
        "basis": "birth_plus_30",
        "axis_basis": "authoryearofbirth + 30 (writing prime proxy)",
        "timeline": [
            {"period": "1850-1874", "books": 250, "total_tokens": 5_000_000,
             "occurrences": 12, "per_million": 2.4},
            {"period": "1875-1899", "books": 1100, "total_tokens": 24_000_000,
             "occurrences": 880, "per_million": 36.7},
            {"period": "1900-1924", "books": 900, "total_tokens": 18_000_000,
             "occurrences": 1450, "per_million": 80.6},
            {"period": "1925-1949", "books": 200, "total_tokens": 3_500_000,
             "occurrences": 310, "per_million": 88.6},
        ],
        "books_total": 2450,
    }


class AutoFallback(unittest.TestCase):
    """When pub_year is sparse, wrapper transparently retries with auto."""

    def test_fallback_when_pub_year_sparse(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        call_log: list[str] = []

        def fake_v1(word, bucket_years, basis):
            call_log.append(basis)
            if basis == "pub_year":
                return _pub_year_sparse()
            return _birth_plus_30_rich()

        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          side_effect=fake_v1):
            r = word_freq_timeline("telephone", bucket_years=25,
                                     basis="pub_year")
        # Both calls happened: first pub_year, then auto fallback
        self.assertEqual(call_log, ["pub_year", "auto"])
        # Result reflects the rich auto timeline, not the sparse pub_year
        self.assertEqual(len(r.data["timeline"]), 4)
        self.assertEqual(r.data["basis_originally_requested"], "pub_year")
        self.assertIn("auto-fallback", r.data["basis_fallback_reason"])
        # _render_note instructs renderer to use fallback buckets
        self.assertIn("auto-fell-back", r.data["_render_note"])
        # Warning surfaces the fallback
        codes = [w.code for w in r.warnings]
        self.assertIn("basis_auto_fallback", codes)

    def test_no_fallback_when_pub_year_has_data(self):
        """If pub_year somehow returns useful data (future Open Library
        enrichment), keep it — don't override."""
        from scripts.v2.tools.words.timeline import word_freq_timeline

        rich_pub_year = {
            "word": "telephone",
            "basis": "pub_year",
            "axis_basis": "pub_year",
            "timeline": [
                {"period": "1875-1899", "books": 400, "total_tokens": 8_000_000,
                  "occurrences": 200, "per_million": 25.0},
                {"period": "1900-1924", "books": 700, "total_tokens": 14_000_000,
                  "occurrences": 950, "per_million": 67.9},
                {"period": "1925-1949", "books": 300, "total_tokens": 5_000_000,
                  "occurrences": 420, "per_million": 84.0},
            ],
        }
        call_log: list[str] = []

        def fake_v1(word, bucket_years, basis):
            call_log.append(basis)
            return rich_pub_year

        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          side_effect=fake_v1):
            r = word_freq_timeline("telephone", basis="pub_year")
        # Only pub_year was called, no auto fallback
        self.assertEqual(call_log, ["pub_year"])
        self.assertNotIn("basis_fallback_reason", r.data)
        codes = [w.code for w in r.warnings]
        self.assertNotIn("basis_auto_fallback", codes)

    def test_auto_basis_no_fallback_loop(self):
        """If user / LLM picks basis='auto' directly and result is
        sparse, do NOT loop back to pub_year (that'd be infinite)."""
        from scripts.v2.tools.words.timeline import word_freq_timeline
        call_log: list[str] = []

        def fake_v1(word, bucket_years, basis):
            call_log.append(basis)
            return {"timeline": [], "basis": basis}  # genuinely empty

        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          side_effect=fake_v1):
            r = word_freq_timeline("noexisto", basis="auto")
        # Only one call — no fallback (basis was already auto)
        self.assertEqual(call_log, ["auto"])
        codes = [w.code for w in r.warnings]
        self.assertIn("sparse_timeline", codes)
        self.assertNotIn("basis_auto_fallback", codes)


class CatalogDescription(unittest.TestCase):
    """Catalog description must warn LLM about pub_year coverage."""

    def test_description_warns_about_pub_year(self):
        from scripts.v2.tool_registry import REGISTRY
        spec = REGISTRY["word_freq_timeline"]
        # The catalog gets this description; LLM should see the warning
        self.assertIn("auto", spec.description)
        self.assertIn("pub_year", spec.description)
        # Coverage hint — «~4 books / книги» is the actionable number
        desc_lc = spec.description.lower()
        self.assertTrue(
            "4 books" in desc_lc or "4 книг" in desc_lc or "few books" in desc_lc,
            msg=f"description doesn't mention coverage: {spec.description}",
        )

    def test_basis_arg_has_description(self):
        from scripts.v2.tool_registry import REGISTRY
        spec = REGISTRY["word_freq_timeline"]
        basis_schema = spec.input_schema["properties"]["basis"]
        self.assertIn("description", basis_schema)
        self.assertIn("auto", basis_schema["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
