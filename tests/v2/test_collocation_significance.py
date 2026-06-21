"""feat/collocation-significance — G² (loglikelihood) + logDice metrics.

Ground-truth math (window=4 → W=8, N=1_000_000, c_target=500), hand-
computed and cross-checked, asserted against the plugin output ±0.01:

  pair          c_pair  c_neighbor    G²       logDice   (contrast: PMI / NPMI)
  fog,thick       40       800       129.01     8.093     (3.64 / 0.207)
  fog,wraith       3         5        24.33     4.617     (7.23 / 0.339)
  fog,<thin>       1         1         9.18       —          —    /   —

Discriminator (the whole point of adding G²): under G², thick (129) ranks
ABOVE wraith (24); under NPMI the order flips (wraith 0.339 > thick 0.207).
Significance ≠ association.

The math classes import only scripts.v2.scoring (no corpus / no rag_tools),
so they run anywhere. The wrapper classes mock v1 + marginals exactly like
test_word_collocates_metric.py.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# --- shared ground-truth scope ---
_N = 1_000_000
_C_TARGET = 500
_WINDOW = 4

_THICK = {"word": "thick", "c_pair": 40, "c_neighbor": 800}
_WRAITH = {"word": "wraith", "c_pair": 3, "c_neighbor": 5}


def _q(candidates):
    from scripts.v2.scoring import ScoringQuery
    return ScoringQuery(
        kind="word_pair", target="fog", candidates=candidates,
        options={"c_target": _C_TARGET, "N": _N, "window": _WINDOW},
    )


class LogLikelihoodMath(unittest.TestCase):
    def test_thick_ground_truth(self):
        from scripts.v2.scoring import LogLikelihood
        [r] = LogLikelihood().compute(_q([_THICK]))
        self.assertEqual(r.id, "thick")
        self.assertAlmostEqual(r.score, 129.01, delta=0.01)
        self.assertEqual(r.direction, "higher_better")
        self.assertTrue(r.extra["over"])
        self.assertAlmostEqual(r.extra["expected"], 3.2, delta=0.01)
        self.assertEqual(r.extra["observed"], 40)

    def test_wraith_ground_truth(self):
        from scripts.v2.scoring import LogLikelihood
        [r] = LogLikelihood().compute(_q([_WRAITH]))
        self.assertAlmostEqual(r.score, 24.33, delta=0.01)
        self.assertTrue(r.extra["over"])

    def test_wrong_kind_returns_empty(self):
        from scripts.v2.scoring import LogLikelihood, ScoringQuery
        self.assertEqual(LogLikelihood().compute(ScoringQuery(
            kind="author_similarity", target="^Doyle,")), [])

    def test_missing_c_target_returns_empty(self):
        from scripts.v2.scoring import LogLikelihood, ScoringQuery
        self.assertEqual(LogLikelihood().compute(ScoringQuery(
            kind="word_pair", target="fog", candidates=[_THICK],
            options={"N": _N, "window": _WINDOW})), [])  # no c_target

    def test_skips_malformed_candidates(self):
        from scripts.v2.scoring import LogLikelihood
        results = LogLikelihood().compute(_q([
            _THICK,
            {"word": "", "c_pair": 10, "c_neighbor": 10},      # empty word
            {"word": "z", "c_pair": 0, "c_neighbor": 10},      # zero c_pair
            "not a dict",                                       # wrong type
        ]))
        self.assertEqual([r.id for r in results], ["thick"])


class LogDiceMath(unittest.TestCase):
    def test_thick_ground_truth(self):
        from scripts.v2.scoring import LogDice
        [r] = LogDice().compute(_q([_THICK]))
        self.assertAlmostEqual(r.score, 8.093, delta=0.01)
        self.assertEqual(r.direction, "higher_better")

    def test_wraith_ground_truth(self):
        from scripts.v2.scoring import LogDice
        [r] = LogDice().compute(_q([_WRAITH]))
        self.assertAlmostEqual(r.score, 4.617, delta=0.01)

    def test_wrong_kind_returns_empty(self):
        from scripts.v2.scoring import LogDice, ScoringQuery
        self.assertEqual(LogDice().compute(ScoringQuery(
            kind="author_similarity", target="^Doyle,")), [])


class SignificanceIsNotAssociation(unittest.TestCase):
    """G² and NPMI disagree on thick vs wraith — the discriminating test."""

    def test_g2_ranks_thick_first_npmi_ranks_wraith_first(self):
        from scripts.v2.scoring import LogLikelihood, NPMI
        cands = [_THICK, _WRAITH]
        g2 = LogLikelihood().compute(_q(cands))
        npmi = NPMI().compute(_q(cands))
        self.assertEqual(g2[0].id, "thick",
                         "G² must rank thick (129) above wraith (24)")
        self.assertEqual(npmi[0].id, "wraith",
                         "NPMI must rank wraith (0.339) above thick (0.207)")


class ProtocolConformance(unittest.TestCase):
    def test_both_registered_and_conform(self):
        from scripts.v2.scoring import REGISTRY, ScoringPlugin
        for name in ("loglikelihood", "logdice"):
            with self.subTest(plugin=name):
                self.assertIn(name, REGISTRY)
                p = REGISTRY[name]
                self.assertIsInstance(p, ScoringPlugin)
                self.assertEqual(p.name, name)
                self.assertIn("word_pair", p.kinds)
                self.assertIn(p.cost, ("cheap", "medium", "heavy"))


# --- wrapper integration (mock v1 + marginals, per test_word_collocates_metric) ---

_V1_RAW = {
    "scope": "author:^X,", "word": "fog", "window": 4,
    "total_occurrences": 500, "books_with_hits": 30,
    "top_collocates": [
        {"word": "thick",  "count": 40},
        {"word": "wraith", "count": 3},
    ],
}


class WrapperDispatchAndLabel(unittest.TestCase):
    def _run(self, metric):
        from scripts.v2.tools.words.collocates import word_collocates
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=dict(_V1_RAW)), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals",
                        return_value=([dict(_THICK), dict(_WRAITH)],
                                      _C_TARGET, _N, 30)):
            return word_collocates({"author": "^X,"}, "fog", top=10,
                                    metric=metric, min_cooccurrence=1)

    def test_loglikelihood_reranks_and_labels_g2(self):
        r = self._run("loglikelihood")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["metric"], "loglikelihood")
        rows = r.data["top_collocates"]
        self.assertEqual(rows[0]["word"], "thick")   # G² 129 > wraith 24
        for c in rows:
            self.assertIn("loglikelihood", c)
        self.assertIsNotNone(r.view)
        self.assertEqual(r.view.payload["metric_label"], "G²")

    def test_logdice_reranks_and_labels(self):
        r = self._run("logdice")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["metric"], "logdice")
        for c in r.data["top_collocates"]:
            self.assertIn("logdice", c)
        self.assertIsNotNone(r.view)
        self.assertEqual(r.view.payload["metric_label"], "logDice")


class MinLLFloor(unittest.TestCase):
    def test_thin_pair_dropped_by_min_ll(self):
        """A (c_pair=1, c_neighbor=1) pair has G²≈9.18 < 15.13 → cut by the
        min_ll floor. min_cooccurrence=1 lets it past the count floor first,
        so the drop is provably attributable to min_ll, not min_cooccurrence.
        """
        from scripts.v2.scoring import LogLikelihood
        from scripts.v2.tools.words.collocates import word_collocates
        thin = {"word": "thin", "c_pair": 1, "c_neighbor": 1}
        # Confirm the cause: plugin G²(1,1) is below the default floor.
        [r_thin] = LogLikelihood().compute(_q([thin]))
        self.assertAlmostEqual(r_thin.score, 9.18, delta=0.05)
        self.assertLess(r_thin.score, 15.13)

        v1 = {**_V1_RAW, "top_collocates": [
            {"word": "thick", "count": 40}, {"word": "thin", "count": 1}]}
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=v1), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals",
                        return_value=([dict(_THICK), thin],
                                      _C_TARGET, _N, 30)):
            r = word_collocates({"author": "^X,"}, "fog", top=10,
                                 metric="loglikelihood", min_cooccurrence=1)
        words = [c["word"] for c in r.data["top_collocates"]]
        self.assertIn("thick", words)             # G² 129 survives
        self.assertNotIn("thin", words)           # G² 9.18 < 15.13 dropped
        self.assertEqual(r.data.get("min_ll"), 15.13)


if __name__ == "__main__":
    unittest.main(verbosity=2)
