"""Sprint 16 Phase B3 — ScoringPlugin registry contract tests.

Verifies every registered plugin matches the Protocol and produces
deterministic shape. New plugins added to REGISTRY are picked up
automatically — no extra test code needed."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.scoring import (
    REGISTRY, ScoredItem, ScoringPlugin, ScoringQuery, get, list_plugins,
)


class RegistryContract(unittest.TestCase):
    def test_every_plugin_matches_protocol(self):
        for name, plugin in REGISTRY.items():
            with self.subTest(plugin=name):
                self.assertIsInstance(plugin, ScoringPlugin,
                                       msg=f"{name} doesn't satisfy ScoringPlugin")

    def test_every_plugin_has_required_fields(self):
        for name, plugin in REGISTRY.items():
            with self.subTest(plugin=name):
                self.assertEqual(plugin.name, name,
                                  msg=f"name mismatch: registry={name!r} plugin.name={plugin.name!r}")
                self.assertIsInstance(plugin.kinds, tuple)
                self.assertGreater(len(plugin.kinds), 0)
                self.assertIn(plugin.cost, ("cheap", "medium", "heavy"))

    def test_get_returns_plugin_or_none(self):
        self.assertIsNotNone(get("burrows_delta"))
        self.assertIsNone(get("not_a_real_plugin"))

    def test_list_plugins_returns_introspection(self):
        plugins = list_plugins()
        self.assertGreaterEqual(len(plugins), 3)  # at least burrows + jaccard + ensemble
        for p in plugins:
            self.assertIn("name", p)
            self.assertIn("kinds", p)
            self.assertIn("cost", p)


class BurrowsDeltaPlugin(unittest.TestCase):
    def test_returns_scored_items_with_lower_better(self):
        from scripts.v2.scoring import BurrowsDelta
        with mock.patch("scripts.rag_tools.author_influences") as mp:
            mp.return_value = {
                "closest": [
                    {"author": "Wells",      "delta": 0.42, "books": 30},
                    {"author": "Stevenson",  "delta": 0.51, "books": 18},
                ],
            }
            plugin = BurrowsDelta()
            results = plugin.compute(ScoringQuery(
                kind="author_similarity", target="^Doyle,",
                candidates=[]))
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].direction, "lower_better")
            self.assertEqual(results[0].id, "Wells")
            self.assertAlmostEqual(results[0].score, 0.42)

    def test_wrong_kind_returns_empty(self):
        from scripts.v2.scoring import BurrowsDelta
        plugin = BurrowsDelta()
        self.assertEqual(plugin.compute(ScoringQuery(
            kind="retrieval_rerank", target="x")), [])

    def test_v1_error_returns_empty(self):
        from scripts.v2.scoring import BurrowsDelta
        with mock.patch("scripts.rag_tools.author_influences") as mp:
            mp.return_value = {"error": "no books"}
            self.assertEqual(BurrowsDelta().compute(ScoringQuery(
                kind="author_similarity", target="^Nobody,")), [])


class JaccardPlugin(unittest.TestCase):
    def test_jaccard_overlap(self):
        from scripts.v2.scoring import JaccardTop200
        with mock.patch("scripts.rag_tools.affinity_by_author") as mp:
            # First call (target) returns set A
            # Second + third call (candidates) return sets B and C
            mp.side_effect = [
                {"top_words": [{"word": w} for w in
                                ["nevermore", "raven", "morella", "dupin"]]},
                # Wells — partial overlap
                {"top_words": [{"word": w} for w in
                                ["raven", "thing", "stranger"]]},
                # Lovecraft — no overlap
                {"top_words": [{"word": w} for w in
                                ["cyclopean", "eldritch", "nameless"]]},
            ]
            plugin = JaccardTop200()
            results = plugin.compute(ScoringQuery(
                kind="author_similarity", target="^Poe,",
                candidates=["^Wells,", "^Lovecraft,"]))
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].direction, "higher_better")
            # Wells (1 overlap of 6 unique) should beat Lovecraft (0 overlap)
            self.assertEqual(results[0].id, "^Wells,")
            self.assertGreater(results[0].score, results[1].score)


class BGERerankerPlugin(unittest.TestCase):
    def test_wrong_kind_returns_empty(self):
        from scripts.v2.scoring import BGEReranker
        plugin = BGEReranker()
        self.assertEqual(plugin.compute(ScoringQuery(
            kind="author_similarity", target="^Doyle,")), [])

    def test_empty_target_returns_empty(self):
        from scripts.v2.scoring import BGEReranker
        plugin = BGEReranker()
        self.assertEqual(plugin.compute(ScoringQuery(
            kind="retrieval_rerank", target="")), [])
        self.assertEqual(plugin.compute(ScoringQuery(
            kind="retrieval_rerank", target="   ")), [])

    def test_no_valid_candidates_returns_empty(self):
        from scripts.v2.scoring import BGEReranker
        plugin = BGEReranker()
        # No id/text — all candidates filtered out
        self.assertEqual(plugin.compute(ScoringQuery(
            kind="retrieval_rerank", target="fog in london",
            candidates=[{"foo": "bar"}, None, 42])), [])

    def test_normalizes_tuple_and_dict_candidates(self):
        from scripts.v2.scoring import BGEReranker
        # _normalize is the conversion hot-path; test directly so we
        # don't need to load the model for shape checks.
        norm = BGEReranker._normalize
        self.assertEqual(norm(("PG123", "fog")), ("PG123", "fog"))
        self.assertEqual(norm(["PG123", "fog"]), ("PG123", "fog"))
        self.assertEqual(norm({"id": "X", "text": "y"}), ("X", "y"))
        self.assertEqual(norm({"pg_id": "PG345", "snippet": "haze"}),
                          ("PG345", "haze"))
        self.assertEqual(norm({"pg_id": "PG345", "text": "haze"}),
                          ("PG345", "haze"))
        self.assertIsNone(norm({"id": "X"}))            # no text
        self.assertIsNone(norm({"text": "y"}))          # no id
        self.assertIsNone(norm("just a string"))
        self.assertIsNone(norm(None))

    def test_compute_with_mocked_model(self):
        """End-to-end shape with a fake CrossEncoder — no real download."""
        from scripts.v2.scoring import BGEReranker, ScoringQuery
        plugin = BGEReranker()
        fake_model = mock.MagicMock()
        # Score reversed-order: 2nd candidate gets highest score
        fake_model.predict.return_value = [0.1, 0.9, 0.5]
        plugin._model = fake_model  # bypass _load()
        results = plugin.compute(ScoringQuery(
            kind="retrieval_rerank",
            target="fog in london",
            candidates=[
                {"pg_id": "PG1", "snippet": "weather report"},
                {"pg_id": "PG2", "snippet": "london fog at dawn"},
                {"pg_id": "PG3", "snippet": "novel about haze"},
            ],
        ))
        self.assertEqual(len(results), 3)
        # Highest score first
        self.assertEqual(results[0].id, "PG2")
        self.assertAlmostEqual(results[0].score, 0.9)
        self.assertEqual(results[0].direction, "higher_better")
        # Verify pairs structure: [[query, text], ...]
        called_pairs = fake_model.predict.call_args[0][0]
        self.assertEqual(called_pairs[0], ["fog in london", "weather report"])

    def test_load_failure_returns_empty(self):
        from scripts.v2.scoring import BGEReranker, ScoringQuery
        plugin = BGEReranker()
        with mock.patch.object(plugin, "_load",
                                side_effect=RuntimeError("no model")):
            self.assertEqual(plugin.compute(ScoringQuery(
                kind="retrieval_rerank", target="x",
                candidates=[("id1", "text1")])), [])


class WordPairPlugins(unittest.TestCase):
    """PMI / NPMI / Dice — pure math, deterministic, no I/O."""

    def _q(self, candidates, c_target=100, N=10_000, window=4):
        from scripts.v2.scoring import ScoringQuery
        return ScoringQuery(
            kind="word_pair", target="fog", candidates=candidates,
            options={"c_target": c_target, "N": N, "window": window},
        )

    def test_pmi_strong_association_outranks_weak(self):
        """A pair seen far more than chance should beat a pair seen at chance."""
        from scripts.v2.scoring import PMI
        # In a 10k-token scope, fog occurs 100x, window=4 (W=8).
        # Expected co-occurrence of "fog" with "thick" (c=80) = 100*8*80/10000=6.4
        # Observed = 50 → strong positive PMI.
        # Compare with "the" (c=600, expected=48, observed=48 → PMI ≈ 0)
        results = PMI().compute(self._q([
            {"word": "thick", "c_pair": 50, "c_neighbor": 80},
            {"word": "neutral", "c_pair": 48, "c_neighbor": 600},
        ]))
        self.assertEqual(results[0].id, "thick")
        self.assertGreater(results[0].score, results[1].score)
        self.assertAlmostEqual(results[1].score, 0, delta=0.5)

    def test_pmi_wrong_kind_returns_empty(self):
        from scripts.v2.scoring import PMI, ScoringQuery
        self.assertEqual(PMI().compute(ScoringQuery(
            kind="author_similarity", target="^Doyle,")), [])

    def test_pmi_missing_options_returns_empty(self):
        from scripts.v2.scoring import PMI, ScoringQuery
        # No c_target → can't compute
        self.assertEqual(PMI().compute(ScoringQuery(
            kind="word_pair", target="fog",
            candidates=[{"word": "thick", "c_pair": 5, "c_neighbor": 10}],
            options={"N": 1000, "window": 4})), [])

    def test_pmi_skips_malformed_candidates(self):
        from scripts.v2.scoring import PMI
        results = PMI().compute(self._q([
            {"word": "good", "c_pair": 30, "c_neighbor": 50},
            {"word": "", "c_pair": 10, "c_neighbor": 10},     # empty word
            {"word": "bad", "c_pair": 0, "c_neighbor": 10},   # zero c_pair
            {"word": "bad2", "c_pair": 10, "c_neighbor": 0},  # zero c_neighbor
            "not a dict",                                       # wrong type
        ]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "good")

    def test_npmi_in_minus_one_to_one_range(self):
        from scripts.v2.scoring import NPMI
        results = NPMI().compute(self._q([
            {"word": "perfect_pair", "c_pair": 100, "c_neighbor": 100},
            {"word": "weak",         "c_pair": 5,   "c_neighbor": 500},
            {"word": "balanced",     "c_pair": 30,  "c_neighbor": 200},
        ]))
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertGreaterEqual(r.score, -1.0)
            self.assertLessEqual(r.score, 1.0)

    def test_npmi_clear_winner(self):
        """Pair that always co-occurs with target should score near 1."""
        from scripts.v2.scoring import NPMI
        # Very specific pair: target=100 occurrences, "marsh" co-occurs 95 times
        # → tightly bound. Compare with random="the" (large word, weak link).
        results = NPMI().compute(self._q([
            {"word": "marsh", "c_pair": 95, "c_neighbor": 100},
            {"word": "the",   "c_pair": 30, "c_neighbor": 1000},
        ]))
        self.assertEqual(results[0].id, "marsh")
        # marsh should dominate, the should be much lower
        self.assertGreater(results[0].score - results[1].score, 0.1)

    def test_dice_symmetric_and_bounded(self):
        from scripts.v2.scoring import Dice
        results = Dice().compute(self._q([
            {"word": "tight",  "c_pair": 80, "c_neighbor": 100},
            {"word": "loose",  "c_pair": 10, "c_neighbor": 1000},
        ]))
        self.assertEqual(results[0].id, "tight")
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0)

    def test_dice_uses_c_neighbor_in_denom(self):
        """A pair with same c_pair but larger c_neighbor should score lower —
        Dice penalizes neighbor's overall commonality."""
        from scripts.v2.scoring import Dice
        results = Dice().compute(self._q([
            {"word": "rare_pair", "c_pair": 20, "c_neighbor": 50},
            {"word": "common_pair", "c_pair": 20, "c_neighbor": 5000},
        ]))
        rare = next(r for r in results if r.id == "rare_pair")
        common = next(r for r in results if r.id == "common_pair")
        self.assertGreater(rare.score, common.score)

    def test_all_three_register_correctly(self):
        from scripts.v2.scoring import REGISTRY
        for name in ("pmi", "npmi", "dice"):
            self.assertIn(name, REGISTRY)
            self.assertIn("word_pair", REGISTRY[name].kinds)


class EnsemblePlugin(unittest.TestCase):
    def test_borda_count_combines_metrics(self):
        from scripts.v2.scoring import Ensemble
        # Mock 2 member plugins to return different rankings
        class FakeA:
            name = "fa"
            kinds = ("author_similarity",)
            cost = "cheap"
            def compute(self, q):
                return [
                    ScoredItem(id="Wells", score=0.1),
                    ScoredItem(id="Stevenson", score=0.2),
                    ScoredItem(id="Wilde", score=0.3),
                ]
            def explain(self, s): return ""
        class FakeB:
            name = "fb"
            kinds = ("author_similarity",)
            cost = "cheap"
            def compute(self, q):
                return [
                    ScoredItem(id="Stevenson", score=0.1),
                    ScoredItem(id="Wells", score=0.2),
                    ScoredItem(id="Wilde", score=0.3),
                ]
            def explain(self, s): return ""
        with mock.patch.dict("scripts.v2.scoring.REGISTRY",
                              {"fa": FakeA(), "fb": FakeB()}):
            ens = Ensemble(["fa", "fb"])
            results = ens.compute(ScoringQuery(
                kind="author_similarity", target="^Doyle,",
                candidates=["Wells", "Stevenson", "Wilde"]))
            # Wells avg rank = (0+1)/2 = 0.5
            # Stevenson avg rank = (1+0)/2 = 0.5
            # Wilde avg rank = (2+2)/2 = 2.0
            # Wells and Stevenson tied at top
            ids_order = [r.id for r in results]
            self.assertEqual(set(ids_order[:2]), {"Wells", "Stevenson"})
            self.assertEqual(ids_order[2], "Wilde")


if __name__ == "__main__":
    unittest.main(verbosity=2)
