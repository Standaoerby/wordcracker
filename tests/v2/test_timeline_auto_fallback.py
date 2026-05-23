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


# =====================================================================
# W-2 (2026-05-23) — bucketizer + renderer schema regression suite.
#
# The reported prod failure: timeline table for "railway"/"telegraph"/
# "steam" showed Period = "None–None" in every row and an empty
# per-million column, with only raw occurrences filled. The wrapper's
# bucket parser handles two failure modes here:
#   1. period strings that fail strict shape — boundaries land as
#      None instead of being half-assigned ("1825–?")
#   2. ascending order is asserted at the wrapper, so v1 returning
#      out-of-order rows doesn't poison the axis
# Renderer never produces the literal "None" string (Phase 6 safe_str
# guarantee, pinned with a value-level assertion here).
# =====================================================================


def _v1_railway_realistic():
    """The expected v1 shape — what rag_tools.word_freq_timeline:1248
    actually returns from a healthy corpus path."""
    return {
        "word": "railway",
        "bucket_years": 25,
        "basis": "auto",
        "axis_basis": "pub_year when known, otherwise birth+30",
        "timeline": [
            {"period": "1825-1849", "books": 50, "total_tokens": 1_000_000,
             "occurrences": 20, "per_million": 20.0},
            {"period": "1850-1874", "books": 100, "total_tokens": 2_000_000,
             "occurrences": 80, "per_million": 40.0},
            {"period": "1875-1899", "books": 200, "total_tokens": 4_000_000,
             "occurrences": 320, "per_million": 80.0},
        ],
    }


class W2TimelineBucketShape(unittest.TestCase):
    """Bucketizer — wrapper's `period` string parse and series shape."""

    def test_buckets_have_int_boundaries(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.view)
        series = r.view.payload["series"]
        self.assertEqual(len(series), 3)
        for s in series:
            self.assertIsInstance(s["bucket_start"], int)
            self.assertIsInstance(s["bucket_end"], int)
            self.assertGreaterEqual(s["bucket_end"], s["bucket_start"])

    def test_per_million_is_numeric_not_none(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        series = r.view.payload["series"]
        for s in series:
            self.assertIsNotNone(
                s["freq_per_million"],
                msg=f"per-million missing in {s!r}",
            )
            self.assertIsInstance(s["freq_per_million"], (int, float))

    def test_count_is_present(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        series = r.view.payload["series"]
        for s in series:
            self.assertIsNotNone(s["count"])
            self.assertIsInstance(s["count"], int)

    def test_buckets_ascending(self):
        # Even if v1 returns them out-of-order, the wrapper must sort.
        from scripts.v2.tools.words.timeline import word_freq_timeline
        out_of_order = dict(_v1_railway_realistic())
        out_of_order["timeline"] = list(reversed(out_of_order["timeline"]))
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=out_of_order):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        series = r.view.payload["series"]
        starts = [s["bucket_start"] for s in series]
        self.assertEqual(starts, sorted(starts))


class W2TimelineRendererIntegration(unittest.TestCase):
    """End-to-end: wrapper → renderer never produces "None–None" rows
    and the period labels are concrete year ranges."""

    def test_rendered_table_has_concrete_periods(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        from scripts.v2 import template_executor

        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")

        rendered = template_executor._render_timeline_chart(r.view)
        # Concrete period labels (en-dash separator from renderer)
        self.assertIn("1825–1849", rendered)
        self.assertIn("1850–1874", rendered)
        self.assertIn("1875–1899", rendered)
        # The "growth toward end of 19th century" signal — the spec
        # acceptance criterion. 80.00 > 20.00.
        self.assertIn("20.00", rendered)
        self.assertIn("80.00", rendered)
        # And the literal "None" must never appear in renderer output.
        self.assertNotIn("None", rendered,
                          msg=f"renderer leaked 'None' into output:\n{rendered}")
        self.assertNotIn("None–None", rendered)

    def test_malformed_v1_period_does_not_leak_none(self):
        # Defensive: even if v1 returns a row with a missing/garbled
        # period, the rendered cell is "?–?", never "None–None".
        from scripts.v2.tools.words.timeline import word_freq_timeline
        from scripts.v2 import template_executor

        garbled = {
            "word": "railway",
            "bucket_years": 25,
            "basis": "auto",
            "axis_basis": "",
            "timeline": [
                {"period": None, "books": 5, "total_tokens": 100,
                 "occurrences": 1, "per_million": 0.5},
                {"period": "bad-shape-foo", "books": 5, "total_tokens": 100,
                 "occurrences": 1, "per_million": 0.5},
            ],
        }
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=garbled):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")

        rendered = template_executor._render_timeline_chart(r.view)
        self.assertNotIn("None", rendered)
        # Wrapper must surface the period-shape failure as a warning so
        # the prod path doesn't quietly swallow it.
        codes = [w.code for w in r.warnings]
        self.assertIn("period_unparseable", codes)


class W2ParsePeriod(unittest.TestCase):
    """Unit-level: `_parse_period` strictness — half-parses are out."""

    def test_well_formed_period(self):
        from scripts.v2.tools.words.timeline import _parse_period
        self.assertEqual(_parse_period("1825-1849"), (1825, 1849))

    def test_none_period(self):
        from scripts.v2.tools.words.timeline import _parse_period
        self.assertEqual(_parse_period(None), (None, None))

    def test_missing_high_year(self):
        from scripts.v2.tools.words.timeline import _parse_period
        # Strict — "1825-" doesn't half-assign
        self.assertEqual(_parse_period("1825-"), (None, None))

    def test_multi_hyphen(self):
        from scripts.v2.tools.words.timeline import _parse_period
        # "1825-1849-1900" must not silently produce (1825, None)
        self.assertEqual(_parse_period("1825-1849-1900"), (None, None))

    def test_garbage(self):
        from scripts.v2.tools.words.timeline import _parse_period
        self.assertEqual(_parse_period("foo"), (None, None))
        self.assertEqual(_parse_period(""), (None, None))
        self.assertEqual(_parse_period(123), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
