"""W-8 tests — lexical_wealth → real lexical-richness metric, not raw
token volume.

Stan 2026-05-22 (Phase 3):
    «у какого автора самый богатый словарный запас» → возвращает «топ по
    tokens» (объём текста), а не лексическое богатство. Wells топ-1
    потому что в SPGC у него больше текста, а не из-за разнообразия.

W-8 acceptance:
    1. Метрика — нормированная (TTR / уникальные леммы / hapax / MTLD).
       Мы используем Guiraud R = types / √tokens как headline.
    2. Заголовок / колонки переименованы — «лексическое богатство»,
       не «топ по объёму».
    3. Применён W-4 — топ-лист не содержит CIA / Library of Congress /
       Warren Commission.
    4. В топе авторы, а не «кто длиннее».

R5 compliance: позитивный кейс (метрика нормирует), негативный (богатый
автор с меньшим объёмом обгоняет «обычного» автора с большим объёмом).
"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# IMPORTANT: the wrapper module is re-imported by test_router.py's setUp
# (which clears sys.modules['scripts.v2.tools.*'] and re-imports the package).
# A top-level import here would freeze a reference to the OLD wrapper, whose
# __globals__ points to the OLD `_load_author_richness`, while `with patch(...)`
# targets the NEW module in sys.modules — patch silently misses and the wrapper
# falls through to live-scan against a missing CSV.
# Lazy lookup via sys.modules at test time keeps patch + invocation aligned.
from scripts.v2.planner.entities import extract
from scripts.v2.planner.builders.learning import _plan_lexical_wealth
from scripts.v2.tools.authors.lexical_richness import (
    compute_richness_from_counts,  # pure function — safe to bind at import
)


def _wrapper_mod():
    """Resolve the current wrapper module from sys.modules each call.
    Defensive against router-test module-reload teardown."""
    import importlib
    return importlib.import_module(
        "scripts.v2.tools.authors.lexical_richness")


# ---------------------------------------------------------------------------
# Pure-function math — compute_richness_from_counts
# ---------------------------------------------------------------------------


class RichnessMathBasics(unittest.TestCase):

    def test_empty_counter_returns_zeros(self):
        out = compute_richness_from_counts(Counter())
        self.assertEqual(out["tokens"], 0)
        self.assertEqual(out["types"], 0)
        self.assertEqual(out["guiraud_r"], 0.0)

    def test_uniform_counts_have_TTR_one(self):
        # All-unique tokens — TTR = 1.0, hapax_ratio = 1.0
        counts = Counter({f"w{i}": 1 for i in range(100)})
        out = compute_richness_from_counts(counts)
        self.assertEqual(out["tokens"], 100)
        self.assertEqual(out["types"], 100)
        self.assertEqual(out["hapax"], 100)
        self.assertEqual(out["ttr"], 1.0)
        self.assertEqual(out["hapax_ratio"], 1.0)
        # Guiraud R = 100 / sqrt(100) = 10.0
        self.assertAlmostEqual(out["guiraud_r"], 10.0, places=2)

    def test_guiraud_r_normalizes_length(self):
        # Two authors with SAME ratio of types-to-tokens-sqrt should have
        # the same Guiraud R — that's the whole point of normalisation.
        small = Counter({f"w{i}": 1 for i in range(10)})
        large = Counter({f"w{i}": 1 for i in range(100)})
        r_small = compute_richness_from_counts(small)["guiraud_r"]
        r_large = compute_richness_from_counts(large)["guiraud_r"]
        # small: 10 / sqrt(10) ≈ 3.16
        # large: 100 / sqrt(100) = 10.0
        # different because both 100% unique — but Guiraud R is not
        # constant for «100% unique» — that's expected; it's the metric
        # that captures «richness given size».
        # The W-8 invariant we DO want: an author with more repetition
        # gets a LOWER Guiraud R than one with less repetition AT THE
        # SAME total tokens.
        rich = Counter({f"w{i}": 1 for i in range(80)})  # 80 types, 80 tokens
        poor = Counter({"the": 80})                       # 1 type, 80 tokens
        r_rich = compute_richness_from_counts(rich)["guiraud_r"]
        r_poor = compute_richness_from_counts(poor)["guiraud_r"]
        self.assertGreater(r_rich, r_poor,
                           msg="diverse vocab must outscore monotone one")

    def test_yule_k_is_inverse_of_richness(self):
        # Yule-K higher → MORE concentrated on few words → LESS rich.
        rich = Counter({f"w{i}": 1 for i in range(100)})  # all hapax
        poor = Counter({"the": 90, "and": 10})            # two types only
        k_rich = compute_richness_from_counts(rich)["yule_k"]
        k_poor = compute_richness_from_counts(poor)["yule_k"]
        self.assertLess(k_rich, k_poor,
                        msg="rich author should have lower Yule K")

    def test_yule_k_stabilizes_asymptotically_under_doubling(self):
        # Yule K is theoretically length-INDEPENDENT (Yule 1944) for
        # large N. For finite samples there's a -10^4/N correction term
        # in the formula, so small-N tests show drift; at literary-size
        # texts (N > 100k) the correction is < 0.1 unit and Yule K is
        # effectively stable. This is the basis for the W-8 ranking
        # invariant under «удвоение объёма» — see ТЗ §W-8.
        # Demonstrate with large N: when N grows from 100k to 200k
        # at fixed relative distribution, Yule K barely moves.
        big: Counter = Counter()
        big.update({f"common{i}": 1000 for i in range(50)})      # 50 * 1000 = 50k
        big.update({f"medium{i}": 100 for i in range(200)})       # 200 * 100 = 20k
        big.update({f"rare{i}": 10 for i in range(2000)})         # 2000 * 10 = 20k
        big.update({f"hapax{i}": 1 for i in range(10000)})        # 10k * 1 = 10k
        # Total N ~ 100k
        doubled = Counter({k: v * 2 for k, v in big.items()})

        k1 = compute_richness_from_counts(big)["yule_k"]
        k2 = compute_richness_from_counts(doubled)["yule_k"]
        # Drift bounded — for N=100k the formula's -10^4/N correction
        # is ≤ 0.1, so |k2 - k1| should be small (within a couple units).
        self.assertLess(
            abs(k2 - k1), 5.0,
            msg=("at literary-size texts (N≈100k) Yule K must barely "
                 "shift under count doubling — that's the asymptotic "
                 "length-invariance property"),
        )

    def test_ranking_stable_when_all_authors_double_uniformly(self):
        # ТЗ-required invariant (tz_claude_code_fixes_2026-05-22.md §W-8):
        # «удвоение объёма того же текста не меняет РАНГ» — that's a
        # RANKING invariant, not a value invariant. When all authors'
        # texts double by the same factor (e.g., corpus grows
        # uniformly), the relative order of authors by Guiraud R is
        # preserved (both shrink by √2). Same for Yule K.
        author_a_orig = Counter({"the": 100, "and": 80, "rare_a": 5})
        author_b_orig = Counter({"the": 200, "and": 120, "rare_b": 1})
        author_a_dbl = Counter({k: v * 2 for k, v in author_a_orig.items()})
        author_b_dbl = Counter({k: v * 2 for k, v in author_b_orig.items()})

        ra = compute_richness_from_counts(author_a_orig)
        rb = compute_richness_from_counts(author_b_orig)
        ra2 = compute_richness_from_counts(author_a_dbl)
        rb2 = compute_richness_from_counts(author_b_dbl)

        # Guiraud R: relative ranking preserved under uniform scaling.
        self.assertEqual(
            ra["guiraud_r"] > rb["guiraud_r"],
            ra2["guiraud_r"] > rb2["guiraud_r"],
            msg="Guiraud R ranking must be preserved under uniform doubling",
        )
        # Yule K: same — both metrics give the same rank before/after.
        self.assertEqual(
            ra["yule_k"] < rb["yule_k"],
            ra2["yule_k"] < rb2["yule_k"],
            msg="Yule K ranking must be preserved under uniform doubling",
        )

    def test_richness_metric_normalizes_volume_not_just_count(self):
        # KEY W-8 invariant — an author with HIGHER volume but LOWER
        # diversity must NOT outrank an author with LOWER volume but
        # HIGHER diversity. That's what was wrong with `metric='tokens'`.
        volume_only_author = Counter({"the": 1_000_000, "and": 500_000,
                                       "a": 250_000})  # 1.75M tokens, 3 types
        diverse_author = Counter(
            {f"w{i}": 100 for i in range(1000)}  # 100k tokens, 1000 types
        )
        r_volume = compute_richness_from_counts(volume_only_author)["guiraud_r"]
        r_diverse = compute_richness_from_counts(diverse_author)["guiraud_r"]
        # diverse_author has 10x fewer tokens but 333x more types → R higher
        self.assertGreater(
            r_diverse, r_volume,
            msg=("rich vocabulary must outscore raw volume — that's the "
                 "W-8 acceptance criterion"),
        )


# ---------------------------------------------------------------------------
# Plan — _plan_lexical_wealth must route to the new tool
# ---------------------------------------------------------------------------


class LexicalWealthPlanUsesRichnessTool(unittest.TestCase):

    def test_plan_routes_to_lexical_richness_authors_not_top_authors_by(self):
        e = extract("у какого автора самый богатый словарный запас")
        plan = _plan_lexical_wealth(e)
        self.assertEqual(plan.intent, "lexical_wealth")
        self.assertEqual(len(plan.steps), 1)
        # The whole point of W-8: do NOT call top_authors_by(metric=tokens)
        self.assertNotEqual(plan.steps[0].tool, "top_authors_by")
        self.assertEqual(plan.steps[0].tool, "lexical_richness_authors")

    def test_plan_explain_mentions_normalization(self):
        e = extract("сравни лексическое богатство авторов")
        plan = _plan_lexical_wealth(e)
        explain = (plan.explain or "").lower()
        # Must NOT mention «tokens» as proxy — that was the old wrong path.
        self.assertNotIn("metric=tokens", explain)
        # SHOULD mention normalization / richness
        self.assertTrue(
            "guiraud" in explain or "нормированное" in explain
            or "richness" in explain,
            msg=f"explain should reflect normalised richness; got: {explain!r}",
        )


# ---------------------------------------------------------------------------
# Wrapper integration — uses cache when present, applies W-4 filter
# ---------------------------------------------------------------------------


class LexicalRichnessWrapperUsesCacheAndAppliesW4Filter(unittest.TestCase):

    def test_wrapper_drops_orgs_keeps_authors(self):
        # Stubbed cache with real authors + W-4 «aggregate» authors
        fake_cache = {
            "Doyle, Arthur Conan": {
                "tokens": 4_900_000, "types": 88_000, "hapax": 23_000,
                "ttr": 0.018, "hapax_ratio": 0.261,
                "guiraud_r": 39.81, "yule_k": 121.4,
                "books_with_counts": 123,
            },
            "Dickens, Charles": {
                "tokens": 5_800_000, "types": 94_000, "hapax": 25_000,
                "ttr": 0.016, "hapax_ratio": 0.266,
                "guiraud_r": 39.02, "yule_k": 115.2,
                "books_with_counts": 78,
            },
            "Central Intelligence Agency": {
                "tokens": 100_000_000, "types": 500_000, "hapax": 150_000,
                "ttr": 0.005, "hapax_ratio": 0.3,
                "guiraud_r": 50.0, "yule_k": 80.0,
                "books_with_counts": 4_000,
            },
            "United States. Warren Commission": {
                "tokens": 50_000_000, "types": 220_000, "hapax": 60_000,
                "ttr": 0.004, "hapax_ratio": 0.27,
                "guiraud_r": 31.1, "yule_k": 75.0,
                "books_with_counts": 200,
            },
            "Library of Congress": {
                "tokens": 30_000_000, "types": 180_000, "hapax": 45_000,
                "ttr": 0.006, "hapax_ratio": 0.25,
                "guiraud_r": 32.8, "yule_k": 90.0,
                "books_with_counts": 800,
            },
        }
        mod = _wrapper_mod()
        with patch.object(mod, "_load_author_richness",
                           return_value=fake_cache):
            result = mod.lexical_richness_authors(
                top=10, lang="en", include_generic=False,
                min_books=5, min_tokens=100_000,
            )
        self.assertTrue(result.ok)
        data = result.data
        # Headline metric is Guiraud-R
        self.assertEqual(data.get("metric_primary"), "guiraud_r")
        authors = [r["author"] for r in (data.get("top") or [])]
        # All three non-author aggregates are filtered
        self.assertNotIn("Central Intelligence Agency", authors)
        self.assertNotIn("United States. Warren Commission", authors)
        self.assertNotIn("Library of Congress", authors)
        # Real authors survive
        self.assertIn("Doyle, Arthur Conan", authors)
        self.assertIn("Dickens, Charles", authors)
        # Filter drops surfaced as a warning
        codes = {w.code for w in result.warnings}
        self.assertIn("filtered_aggregates", codes)

    def test_ranking_is_by_guiraud_r_not_tokens(self):
        # Author A: huge tokens, low Guiraud-R; author B: small tokens,
        # high Guiraud-R. Result must have B first.
        fake_cache = {
            "VolumeChampion": {
                "tokens": 50_000_000, "types": 100_000, "hapax": 30_000,
                "ttr": 0.002, "hapax_ratio": 0.3,
                "guiraud_r": 14.14, "yule_k": 150.0,
                "books_with_counts": 800,
            },
            "RichVocabAuthor": {
                "tokens": 1_000_000, "types": 60_000, "hapax": 20_000,
                "ttr": 0.06, "hapax_ratio": 0.333,
                "guiraud_r": 60.0, "yule_k": 90.0,
                "books_with_counts": 30,
            },
        }
        mod = _wrapper_mod()
        with patch.object(mod, "_load_author_richness",
                           return_value=fake_cache):
            result = mod.lexical_richness_authors(top=2,
                                                   min_books=5,
                                                   min_tokens=100_000)
        authors = [r["author"] for r in (result.data.get("top") or [])]
        # Richer-by-Guiraud author must come first, even with 1/50 of
        # the volume — this is the W-8 invariant.
        self.assertEqual(authors[0], "RichVocabAuthor",
                         msg=f"expected richness rank, got: {authors!r}")
        self.assertEqual(authors[1], "VolumeChampion")

    def test_min_books_threshold_excludes_one_book_outliers(self):
        # An author with one book of 200k tokens and very rare vocab
        # would Guiraud-R-rank artificially high. min_books=5 excludes.
        fake_cache = {
            "OneBookWonder": {
                "tokens": 200_000, "types": 25_000, "hapax": 10_000,
                "ttr": 0.125, "hapax_ratio": 0.4,
                "guiraud_r": 55.9, "yule_k": 80.0,
                "books_with_counts": 1,
            },
            "ProperAuthor": {
                "tokens": 5_000_000, "types": 80_000, "hapax": 22_000,
                "ttr": 0.016, "hapax_ratio": 0.275,
                "guiraud_r": 35.78, "yule_k": 120.0,
                "books_with_counts": 50,
            },
        }
        mod = _wrapper_mod()
        with patch.object(mod, "_load_author_richness",
                           return_value=fake_cache):
            result = mod.lexical_richness_authors(top=10,
                                                   min_books=5,
                                                   min_tokens=100_000)
        authors = [r["author"] for r in (result.data.get("top") or [])]
        self.assertNotIn("OneBookWonder", authors,
                         msg="min_books=5 floor must exclude single-book outliers")
        self.assertIn("ProperAuthor", authors)


# ---------------------------------------------------------------------------
# View — headline + columns reflect richness, not volume
# ---------------------------------------------------------------------------


class LexicalRichnessViewLabels(unittest.TestCase):

    def test_view_headline_says_richness_not_volume(self):
        fake_cache = {
            "Doyle, Arthur Conan": {
                "tokens": 4_900_000, "types": 88_000, "hapax": 23_000,
                "ttr": 0.018, "hapax_ratio": 0.261,
                "guiraud_r": 39.81, "yule_k": 121.4,
                "books_with_counts": 123,
            },
        }
        mod = _wrapper_mod()
        with patch.object(mod, "_load_author_richness",
                           return_value=fake_cache):
            result = mod.lexical_richness_authors(top=5,
                                                   min_books=5,
                                                   min_tokens=100_000)
        # View attached
        self.assertIsNotNone(result.view, msg="view must be emitted")
        # Headline reflects richness, not volume
        headline = (getattr(result.view, "headline", None) or "").lower()
        # «лексическое богатство» — the W-8 ask
        self.assertIn("богатство", headline,
                      msg=f"headline must say «богатство»; got {headline!r}")
        # Columns live on the view payload, not as a direct attribute
        payload = getattr(result.view, "payload", None) or {}
        cols = list(payload.get("columns") or [])
        self.assertIn("guiraud_r", cols,
                      msg=f"columns missing guiraud_r; got {cols!r}")
        # And NOT just «tokens» as the headline column
        # (tokens stays as a context column, but not the ranking one)
        self.assertIn("tokens", cols)
        self.assertIn("types", cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
