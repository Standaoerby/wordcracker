"""Characterization tests for the eval-tripwire (AUTONOMY_RUNBOOK_R-30 §6). WP-0 #4.

Pins:
  - run_eval aggregates to a pass-rate + per-case verdicts (transport error
    fails the case);
  - check_tripwire is a pure decision: trips iff pass-rate < baseline − ε;
    FAIL-SAFE — an absent OR unstamped (pass_rate=null) baseline never trips;
  - baseline load/missing/corrupt + re-stamp (write_baseline) round-trip;
  - the SHIPPED eval cases are well-formed and the SHIPPED baseline ships
    UNSTAMPED so the tripwire can't false-rollback before a prod re-stamp;
  - the runner's run_eval_tripwire maps the report → StepResult, fail-closed on
    any tripwire error.

No live endpoint: the HTTP fire is injected; the trip logic is pure.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy.eval_tripwire import (  # noqa: E402
    DEFAULT_BASELINE,
    EVAL_CASES,
    EvalReport,
    TripwireReport,
    check_tripwire,
    load_baseline,
    run_eval,
    run_eval_tripwire_battery,
    write_baseline,
)
from scripts.autonomy.deploy_runner import DeployRunner  # noqa: E402
import scripts.autonomy.eval_tripwire as et_mod  # noqa: E402


# --- synthetic eval cases + fake fire --------------------------------------
CASES = [
    {"id": "c-ok", "question": "QOK", "assert": [{"kind": "tool_called", "name": "t1"}]},
    {"id": "c-bad", "question": "QBAD", "assert": [{"kind": "tool_called", "name": "t1"}]},
]


def _fire_map(mapping, *, err_for=None, elapsed=1.0):
    def fn(base_url, question, engine, timeout):
        if err_for is not None and err_for in question:
            return {}, elapsed, "URLError: refused"
        for key, payload in mapping.items():
            if key in question:
                return payload, elapsed, None
        return {}, elapsed, "no canned payload"
    return fn


FIRE_BOTH = _fire_map({
    "QOK": {"answer": "hi", "tool_calls": [{"name": "t1"}]},
    "QBAD": {"answer": "hi", "tool_calls": [{"name": "t2"}]},
})


# ---------------------------------------------------------------------------
# run_eval
# ---------------------------------------------------------------------------

class RunEval(unittest.TestCase):
    def test_pass_rate_and_verdicts(self):
        rep = run_eval("http://x", cases=CASES, fire_fn=FIRE_BOTH)
        self.assertEqual((rep.passed, rep.total), (1, 2))
        self.assertAlmostEqual(rep.pass_rate, 0.5)
        self.assertEqual(rep.verdicts, {"c-ok": "PASS", "c-bad": "FAIL"})
        self.assertEqual([cid for cid, _ in rep.failures], ["c-bad"])

    def test_all_pass(self):
        rep = run_eval("http://x", cases=[CASES[0]], fire_fn=FIRE_BOTH)
        self.assertEqual(rep.pass_rate, 1.0)

    def test_transport_error_counts_as_fail(self):
        rep = run_eval("http://x", cases=CASES, fire_fn=_fire_map(
            {"QOK": {"answer": "hi", "tool_calls": [{"name": "t1"}]}}, err_for="QBAD"))
        self.assertEqual(rep.verdicts["c-bad"], "FAIL")
        self.assertTrue(rep.failures[0][1][0].startswith("transport:"))


# ---------------------------------------------------------------------------
# check_tripwire — pure decision, FAIL-SAFE
# ---------------------------------------------------------------------------

def _rep(rate):
    return EvalReport(passed=int(rate * 10), total=10, pass_rate=rate)


class CheckTripwire(unittest.TestCase):
    def test_below_floor_trips(self):
        tripped, detail = check_tripwire(_rep(0.70), {"pass_rate": 0.90}, epsilon=0.05)
        self.assertTrue(tripped)
        self.assertIn("TRIP", detail)

    def test_at_or_above_floor_does_not_trip(self):
        # baseline 0.90, ε 0.05 → floor 0.85; 0.87 ≥ 0.85
        self.assertFalse(check_tripwire(_rep(0.87), {"pass_rate": 0.90}, epsilon=0.05)[0])
        self.assertFalse(check_tripwire(_rep(0.90), {"pass_rate": 0.90}, epsilon=0.05)[0])

    def test_absent_baseline_never_trips(self):
        tripped, detail = check_tripwire(_rep(0.10), None)
        self.assertFalse(tripped)
        self.assertIn("armed-pending", detail)

    def test_unstamped_baseline_never_trips(self):
        # the SHIPPING state: pass_rate is null → fail-safe, no trip even at 0%
        tripped, detail = check_tripwire(_rep(0.0), {"pass_rate": None})
        self.assertFalse(tripped)
        self.assertIn("armed-pending", detail)


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

class BaselineIO(unittest.TestCase):
    def test_missing_is_none(self):
        self.assertIsNone(load_baseline(Path("/no/such/baseline.json")))

    def test_corrupt_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_baseline(p))

    def test_write_then_load_roundtrip_and_note_carried(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text(json.dumps({"_note": "keep me", "pass_rate": None}), encoding="utf-8")
            rep = EvalReport(passed=2, total=2, pass_rate=1.0,
                             verdicts={"c-ok": "PASS", "c2": "PASS"})
            write_baseline(p, "9.9.9", rep)
            loaded = load_baseline(p)
            self.assertEqual(loaded["_note"], "keep me")        # carried forward
            self.assertEqual(loaded["version"], "9.9.9")
            self.assertEqual(loaded["pass_rate"], 1.0)
            self.assertEqual(loaded["verdicts"], {"c-ok": "PASS", "c2": "PASS"})


# ---------------------------------------------------------------------------
# Shipped artifacts
# ---------------------------------------------------------------------------

class ShippedArtifacts(unittest.TestCase):
    def test_eval_cases_wellformed_and_unique(self):
        ids = [c["id"] for c in EVAL_CASES]
        self.assertEqual(len(ids), len(set(ids)), f"eval case ids not unique: {ids}")
        for c in EVAL_CASES:
            self.assertTrue(c.get("question"), f"{c['id']} missing question")
            self.assertTrue(c.get("assert"), f"{c['id']} missing assertions")

    def test_eval_set_covers_s1_book_and_author_scope(self):
        # The headline S1 routing nest must be in the seed set.
        called = {r.get("name") for c in EVAL_CASES for r in c["assert"] if r["kind"] == "tool_called"}
        self.assertIn("top_ngrams_by_book", called)
        self.assertIn("top_ngrams_by_author", called)

    def test_shipped_baseline_is_unstamped_failsafe(self):
        # The committed baseline ships with pass_rate=null so the tripwire is
        # armed-pending and CANNOT false-rollback before a prod re-stamp.
        baseline = load_baseline(DEFAULT_BASELINE)
        self.assertIsNotNone(baseline, f"shipped baseline missing at {DEFAULT_BASELINE}")
        self.assertIsNone(baseline.get("pass_rate"),
                          "shipped baseline must be UNSTAMPED (pass_rate=null) — fail-safe")
        # and the fail-safe is real: even a 0% run does not trip against it
        self.assertFalse(check_tripwire(_rep(0.0), baseline)[0])


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

class Battery(unittest.TestCase):
    def _baseline_file(self, td, payload):
        p = Path(td) / "b.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_unstamped_baseline_ok_even_with_failures(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._baseline_file(td, {"pass_rate": None})
            rep = run_eval_tripwire_battery("http://x", cases=CASES, fire_fn=FIRE_BOTH,
                                            baseline_path=p)
            self.assertTrue(rep.ok)            # armed-pending → never trips
            self.assertFalse(rep.tripped)
            self.assertIn("50%", rep.detail)   # 1/2 cases pass

    def test_stamped_baseline_trips_on_drop(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._baseline_file(td, {"pass_rate": 1.0})  # baseline expected 100%
            rep = run_eval_tripwire_battery("http://x", cases=CASES, fire_fn=FIRE_BOTH,
                                            baseline_path=p, epsilon=0.05)
            self.assertFalse(rep.ok)           # 50% < 100% − 5% → TRIP
            self.assertTrue(rep.tripped)

    def test_stamped_baseline_holds_when_matching(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._baseline_file(td, {"pass_rate": 0.5})
            rep = run_eval_tripwire_battery("http://x", cases=CASES, fire_fn=FIRE_BOTH,
                                            baseline_path=p, epsilon=0.05)
            self.assertTrue(rep.ok)            # 50% ≥ 50% − 5%


# ---------------------------------------------------------------------------
# Runner wiring — run_eval_tripwire maps the report, fail-closed on error
# ---------------------------------------------------------------------------

class RunnerEvalWiring(unittest.TestCase):
    def setUp(self):
        self._orig = et_mod.run_eval_tripwire_battery

    def tearDown(self):
        et_mod.run_eval_tripwire_battery = self._orig

    def _runner(self):
        return DeployRunner(repo_root=Path("."), log=lambda *a, **k: None)

    def test_maps_ok(self):
        et_mod.run_eval_tripwire_battery = lambda *a, **k: TripwireReport(
            ok=True, tripped=False, detail="eval 9/9 (100%); pass-rate ≥ baseline")
        res = self._runner().run_eval_tripwire("sha1")
        self.assertTrue(res.ok)
        self.assertEqual(res.name, "eval_tripwire")

    def test_maps_trip(self):
        et_mod.run_eval_tripwire_battery = lambda *a, **k: TripwireReport(
            ok=False, tripped=True, detail="eval 5/9 (56%); pass-rate < baseline → TRIP")
        res = self._runner().run_eval_tripwire("sha1")
        self.assertFalse(res.ok)
        self.assertIn("TRIP", res.detail)

    def test_fail_closed_on_exception(self):
        def _boom(*a, **k):
            raise RuntimeError("kaboom")
        et_mod.run_eval_tripwire_battery = _boom
        res = self._runner().run_eval_tripwire("sha1")
        self.assertFalse(res.ok)
        self.assertIn("eval-tripwire error", res.detail)


if __name__ == "__main__":
    unittest.main()
