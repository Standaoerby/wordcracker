"""Characterization tests for the smoke-as-code battery (AUTONOMY_RUNBOOK_R-30
§6). WP-0 #3.

Pins:
  - the matcher (the rule kinds the smoke probes use) passes/fails correctly;
  - the shipped S1 probes are well-formed and assert the real S1 invariant
    (book → top_ngrams_by_book, never the author aggregate / affinity; author
    → top_ngrams_by_author);
  - the battery aggregates FAIL-CLOSED: all-good payloads pass; a book query
    routed to the author aggregate fails; a clarify bounce fails; a transport
    error fails; an empty battery fails;
  - the runner's run_smoke seam maps the battery report → StepResult and is
    fail-closed on any battery error.

No live endpoint: the HTTP `fire` is injected with a fake, so the suite runs on
bare ubuntu-latest in the predeploy CI job.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy.smoke import (  # noqa: E402
    SMOKE_PROBES,
    SmokeReport,
    evaluate_probe,
    run_smoke_battery,
    _match,
)
from scripts.autonomy.deploy_runner import DeployRunner  # noqa: E402
import scripts.autonomy.smoke as smoke_mod  # noqa: E402


# --- canned /api/chat payloads ---------------------------------------------
GOOD_BOOK = {"answer": "blood, night, dark", "intent": "author_top_words",
             "tool_calls": [{"name": "top_ngrams_by_book"}]}
GOOD_AUTHOR = {"answer": "heart, hound, mystery", "intent": "author_top_words",
               "tool_calls": [{"name": "top_ngrams_by_author"}]}
# the S1 bug-A regression: a named book collapses into the author aggregate
BUG_BOOK_AS_AUTHOR = {"answer": "x y z", "intent": "author_top_words",
                      "tool_calls": [{"name": "top_ngrams_by_author"}]}
# the S1 bug variant: raw-frequency request routed to corpus-relative affinity
BUG_BOOK_AS_AFFINITY = {"answer": "x y z", "intent": "author_top_words",
                        "tool_calls": [{"name": "affinity_by_book"}]}
CLARIFY_BOUNCE = {"answer": "уточните книгу", "intent": "clarify", "tool_calls": []}


def _fire_map(mapping, *, err_for=None, elapsed=1.0):
    """A fake fire_fn mapping a question substring → canned payload."""
    def fn(base_url, question, engine, timeout):
        if err_for is not None and err_for in question:
            return {}, elapsed, "URLError: connection refused"
        for key, payload in mapping.items():
            if key in question:
                return payload, elapsed, None
        return {}, elapsed, "no canned payload for question"
    return fn


BOTH_GOOD = {"Dracula": GOOD_BOOK, "Дойла": GOOD_AUTHOR}


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

class Matcher(unittest.TestCase):
    def test_answer_not_empty(self):
        self.assertIsNone(_match({"kind": "answer_not_empty"}, {"answer": "hi"}, 1.0))
        self.assertIsNotNone(_match({"kind": "answer_not_empty"}, {"answer": "  "}, 1.0))

    def test_intent_not_in(self):
        rule = {"kind": "intent_not_in", "values": ["clarify"]}
        self.assertIsNone(_match(rule, {"intent": "author_top_words"}, 1.0))
        self.assertIsNotNone(_match(rule, {"intent": "clarify"}, 1.0))

    def test_intent_in(self):
        rule = {"kind": "intent_in", "values": ["out_of_scope"]}
        self.assertIsNone(_match(rule, {"intent": "out_of_scope"}, 1.0))
        self.assertIsNotNone(_match(rule, {"intent": "author_top_words"}, 1.0))

    def test_tool_called(self):
        rule = {"kind": "tool_called", "name": "top_ngrams_by_book"}
        self.assertIsNone(_match(rule, GOOD_BOOK, 1.0))
        self.assertIsNotNone(_match(rule, GOOD_AUTHOR, 1.0))

    def test_tool_not_called(self):
        rule = {"kind": "tool_not_called", "name": "top_ngrams_by_author"}
        self.assertIsNone(_match(rule, GOOD_BOOK, 1.0))
        self.assertIsNotNone(_match(rule, GOOD_AUTHOR, 1.0))

    def test_contains_and_not_contains(self):
        self.assertIsNone(_match({"kind": "contains", "value": "blood"}, GOOD_BOOK, 1.0))
        self.assertIsNotNone(_match({"kind": "contains", "value": "zzz"}, GOOD_BOOK, 1.0))
        self.assertIsNone(_match({"kind": "not_contains", "value": "zzz"}, GOOD_BOOK, 1.0))

    def test_regex_match_and_no_match(self):
        self.assertIsNone(_match({"kind": "regex_match", "pattern": "blo+d"}, GOOD_BOOK, 1.0))
        self.assertIsNotNone(_match({"kind": "regex_no_match", "pattern": "blo+d"}, GOOD_BOOK, 1.0))

    def test_latency_under(self):
        self.assertIsNone(_match({"kind": "latency_under_s", "value": 5.0}, GOOD_BOOK, 1.0))
        self.assertIsNotNone(_match({"kind": "latency_under_s", "value": 5.0}, GOOD_BOOK, 9.0))

    def test_unknown_kind_is_a_failure(self):
        self.assertIsNotNone(_match({"kind": "no_such_kind"}, GOOD_BOOK, 1.0))


class EvaluateProbe(unittest.TestCase):
    def test_transport_error_fails_closed(self):
        probe = SMOKE_PROBES[0]
        ok, reasons = evaluate_probe(probe, {}, 1.0, "URLError: refused")
        self.assertFalse(ok)
        self.assertTrue(reasons[0].startswith("transport:"))

    def test_good_book_payload_passes_book_probe(self):
        probe = next(p for p in SMOKE_PROBES if p["id"] == "S1-book-scope")
        ok, reasons = evaluate_probe(probe, GOOD_BOOK, 1.0, None)
        self.assertTrue(ok, reasons)

    def test_book_as_author_fails_book_probe(self):
        probe = next(p for p in SMOKE_PROBES if p["id"] == "S1-book-scope")
        ok, reasons = evaluate_probe(probe, BUG_BOOK_AS_AUTHOR, 1.0, None)
        self.assertFalse(ok)
        joined = " ".join(reasons)
        self.assertIn("top_ngrams_by_book", joined)   # not called
        self.assertIn("top_ngrams_by_author", joined)  # forbidden, was called


# ---------------------------------------------------------------------------
# Shipped S1 probe definitions
# ---------------------------------------------------------------------------

class ShippedProbes(unittest.TestCase):
    def test_probes_wellformed_and_unique(self):
        ids = [p["id"] for p in SMOKE_PROBES]
        self.assertEqual(len(ids), len(set(ids)), f"probe ids not unique: {ids}")
        for p in SMOKE_PROBES:
            self.assertTrue(p.get("question"), f"{p['id']} missing question")
            self.assertTrue(p.get("assert"), f"{p['id']} missing assertions")

    def test_book_probe_pins_book_scope_invariant(self):
        p = next(p for p in SMOKE_PROBES if p["id"] == "S1-book-scope")
        called = {r["name"] for r in p["assert"] if r["kind"] == "tool_called"}
        not_called = {r["name"] for r in p["assert"] if r["kind"] == "tool_not_called"}
        self.assertIn("top_ngrams_by_book", called)
        self.assertIn("top_ngrams_by_author", not_called)
        self.assertIn("affinity_by_book", not_called)

    def test_author_probe_pins_author_scope_invariant(self):
        p = next(p for p in SMOKE_PROBES if p["id"] == "S1-author-scope")
        called = {r["name"] for r in p["assert"] if r["kind"] == "tool_called"}
        not_called = {r["name"] for r in p["assert"] if r["kind"] == "tool_not_called"}
        self.assertIn("top_ngrams_by_author", called)
        self.assertIn("top_ngrams_by_book", not_called)


# ---------------------------------------------------------------------------
# Battery aggregation — FAIL-CLOSED
# ---------------------------------------------------------------------------

class Battery(unittest.TestCase):
    def test_all_good_passes(self):
        rep = run_smoke_battery("http://x", fire_fn=_fire_map(BOTH_GOOD))
        self.assertTrue(rep.ok)
        self.assertEqual((rep.passed, rep.total), (2, 2))
        self.assertEqual(rep.failures, [])

    def test_book_routed_to_author_fails(self):
        rep = run_smoke_battery("http://x", fire_fn=_fire_map(
            {"Dracula": BUG_BOOK_AS_AUTHOR, "Дойла": GOOD_AUTHOR}))
        self.assertFalse(rep.ok)
        self.assertEqual([pid for pid, _ in rep.failures], ["S1-book-scope"])

    def test_book_routed_to_affinity_fails(self):
        rep = run_smoke_battery("http://x", fire_fn=_fire_map(
            {"Dracula": BUG_BOOK_AS_AFFINITY, "Дойла": GOOD_AUTHOR}))
        self.assertFalse(rep.ok)
        self.assertIn("affinity_by_book", rep.detail)

    def test_clarify_bounce_fails(self):
        rep = run_smoke_battery("http://x", fire_fn=_fire_map(
            {"Dracula": CLARIFY_BOUNCE, "Дойла": GOOD_AUTHOR}))
        self.assertFalse(rep.ok)
        self.assertEqual([pid for pid, _ in rep.failures], ["S1-book-scope"])

    def test_transport_error_fail_closed(self):
        rep = run_smoke_battery("http://x", fire_fn=_fire_map(BOTH_GOOD, err_for="Dracula"))
        self.assertFalse(rep.ok)
        pid, reasons = rep.failures[0]
        self.assertEqual(pid, "S1-book-scope")
        self.assertTrue(reasons[0].startswith("transport:"))

    def test_empty_battery_fail_closed(self):
        rep = run_smoke_battery("http://x", probes=[], fire_fn=_fire_map(BOTH_GOOD))
        self.assertFalse(rep.ok)
        self.assertEqual(rep.total, 0)
        self.assertIn("no smoke probes", rep.detail)

    def test_detail_carries_count_and_sha(self):
        rep = run_smoke_battery("http://x", expected_sha="abc123",
                                fire_fn=_fire_map(BOTH_GOOD))
        self.assertIn("2/2", rep.detail)
        self.assertIn("abc123", rep.detail)


# ---------------------------------------------------------------------------
# Runner wiring — run_smoke maps the battery report, fail-closed on error
# ---------------------------------------------------------------------------

class RunnerSmokeWiring(unittest.TestCase):
    def setUp(self):
        self._orig = smoke_mod.run_smoke_battery

    def tearDown(self):
        smoke_mod.run_smoke_battery = self._orig

    def _runner(self):
        return DeployRunner(repo_root=Path("."), log=lambda *a, **k: None)

    def test_maps_battery_ok(self):
        smoke_mod.run_smoke_battery = lambda *a, **k: SmokeReport(
            ok=True, passed=2, total=2, detail="smoke 2/2 probes pass")
        res = self._runner().run_smoke("sha1")
        self.assertTrue(res.ok)
        self.assertEqual(res.name, "smoke")
        self.assertIn("2/2", res.detail)

    def test_maps_battery_failure(self):
        smoke_mod.run_smoke_battery = lambda *a, **k: SmokeReport(
            ok=False, passed=1, total=2, detail="smoke 1/2 pass — FAIL: S1-book-scope")
        res = self._runner().run_smoke("sha1")
        self.assertFalse(res.ok)
        self.assertIn("FAIL", res.detail)

    def test_fail_closed_on_battery_exception(self):
        def _boom(*a, **k):
            raise RuntimeError("kaboom")
        smoke_mod.run_smoke_battery = _boom
        res = self._runner().run_smoke("sha1")
        self.assertFalse(res.ok)
        self.assertIn("smoke battery error", res.detail)


if __name__ == "__main__":
    unittest.main()
