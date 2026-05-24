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


# =====================================================================
# W-2 (2026-05-24) — render-row shape: r.data.timeline carries the
# canonical fields the renderer (LLM and typed) needs so the prod
# «None–None» / empty per-million columns can't recur. Pinning the
# in-data shape complements the view-side tests above.
# =====================================================================


class W2ComputeFreqFromCounts(unittest.TestCase):
    """The wrapper recomputes freq_per_million from
    occurrences/total_tokens × 1e6 — robust against v1 dropping the
    field and the structural fix for prod's empty «Частота» column
    (railway/telegraph/steam, 2026-05-24)."""

    def test_freq_recomputed_matches_formula(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        for row in r.data["timeline"]:
            expected = round(
                1_000_000.0 * row["occurrences"] / row["total_tokens"], 2,
            )
            self.assertEqual(row["freq_per_million"], expected, msg=row)
            # The v1 row key stays in sync so the V1WordFreqTimeline
            # __row_keys__ contract isn't violated by callers reading
            # `per_million` directly.
            self.assertEqual(row["per_million"], expected, msg=row)

    def test_freq_survives_v1_missing_per_million(self):
        # v1 dropped per_million from a row — wrapper still surfaces
        # freq from counts, so the LLM column doesn't go empty.
        from scripts.v2.tools.words.timeline import word_freq_timeline
        v1 = {
            "word": "railway", "bucket_years": 25, "basis": "auto",
            "axis_basis": "",
            "timeline": [
                # per_million intentionally absent
                {"period": "1825-1849", "books": 50, "total_tokens": 1_000_000,
                 "occurrences": 20},
            ],
        }
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=v1):
            r = word_freq_timeline("railway")
        row = r.data["timeline"][0]
        self.assertEqual(row["freq_per_million"], 20.0)
        self.assertEqual(row["per_million"], 20.0)
        self.assertEqual(row["count"], 20)


class W2DataTimelineShape(unittest.TestCase):
    """r.data.timeline (the LLM render payload) carries renderer-ready
    fields. Pins each row to a shape the LLM can't render as
    «None–None» even if it hallucinates view-side field names."""

    def test_period_is_display_ready_en_dash(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway")
        for row in r.data["timeline"]:
            # Display label uses en-dash «–», not ASCII hyphen «-»
            self.assertIn("–", row["period"], msg=row)
            self.assertNotIn("None", row["period"], msg=row)
            # And the raw int bounds are surfaced too, for the typed
            # renderer + any downstream column-aware consumers.
            self.assertIsInstance(row["bucket_start"], int)
            self.assertIsInstance(row["bucket_end"], int)
            self.assertGreaterEqual(row["bucket_end"], row["bucket_start"])

    def test_renderer_aliases_populated(self):
        # `freq_per_million` and `count` are the renderer-friendly
        # aliases. Their values are populated from raw counts so the
        # LLM column for «Частота на 1M слов» is never empty.
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway")
        for row in r.data["timeline"]:
            self.assertIsInstance(row["freq_per_million"], (int, float))
            self.assertIsInstance(row["count"], int)

    def test_data_timeline_sorted_ascending(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        out_of_order = dict(_v1_railway_realistic())
        out_of_order["timeline"] = list(reversed(out_of_order["timeline"]))
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=out_of_order):
            r = word_freq_timeline("railway")
        starts = [row["bucket_start"] for row in r.data["timeline"]]
        self.assertEqual(starts, sorted(starts))


class W2RailwayGrowthShape(unittest.TestCase):
    """Acceptance criterion mirror — «частота слова railway по эпохам»
    sees a populated period column, populated freq column, and an
    ascending freq curve toward the end of the 19th century."""

    def test_railway_acceptance_shape(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway", bucket_years=25, basis="auto")
        rows = r.data["timeline"]
        # Periods all labelled, no «None» anywhere.
        labels = [row["period"] for row in rows]
        self.assertTrue(all("None" not in lbl for lbl in labels), msg=labels)
        self.assertEqual(labels, ["1825–1849", "1850–1874", "1875–1899"])
        # Freq column is populated and ascending — the visible-growth
        # signal the acceptance criterion calls out.
        freqs = [row["freq_per_million"] for row in rows]
        self.assertEqual(freqs, sorted(freqs))
        self.assertGreater(freqs[-1], freqs[0])


class W2LLMRenderPayloadNoNone(unittest.TestCase):
    """The shrinking normalize path used by _llm_render must not strip
    period column or leave None bounds. Locks the prod failure where
    the LLM saw a payload that licensed a «None–None» rendering."""

    def test_payload_normalize_keeps_period_and_freq(self):
        from scripts.v2.tools.words.timeline import word_freq_timeline
        from scripts.v2.rag_v2 import _normalize_data_for_render
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway")
        normalized = _normalize_data_for_render(r.data)
        # Every row keeps period as a real string and freq as a real number.
        for row in normalized["timeline"]:
            self.assertNotIn(row.get("period"), (None, "", "None", "?–?"))
            self.assertIsInstance(row.get("freq_per_million"), (int, float))
            # Not stripped to empty value
            self.assertIsNotNone(row.get("freq_per_million"))

    def test_payload_serialized_has_no_none_none(self):
        # End-to-end belt + suspenders: serialize what the LLM would
        # see and assert «None–None» does not appear textually.
        import json
        from scripts.v2.tools.words.timeline import word_freq_timeline
        from scripts.v2.rag_v2 import _normalize_data_for_render
        with mock.patch("scripts.rag_tools.word_freq_timeline",
                          return_value=_v1_railway_realistic()):
            r = word_freq_timeline("railway")
        normalized = _normalize_data_for_render(r.data)
        as_json = json.dumps(normalized, ensure_ascii=False, default=str)
        self.assertNotIn("None–None", as_json)
        self.assertNotIn("\"None\"", as_json)


if __name__ == "__main__":
    unittest.main(verbosity=2)
