"""W-18 tests — pre-deploy 12-probe taxonomy suite (`scripts/predeploy_probe_suite.py`).

Covers the parts that have no network dependency:
    - `_match_one` for every supported rule kind (universal_pass_when grammar)
    - `evaluate_probe` combining universal + per-probe rules + transport errors
    - `detect_regressions` (only PASS->FAIL counts, not FAIL->PASS / FAIL->FAIL)
    - `load_config` (12-probe shape, P1..P12 ids, no duplicates)
    - `check_config_filled` (__FILL_FROM_SOURCE__ slots flagged)
    - baseline write/read roundtrip
    - `_read_version` reads ANALYTICS_VERSION from scripts/v2/__version__.py

`fire_probe` / `wait_for_health` are not covered — they are I/O wrappers, the
real test of those is the live probe run itself.

R10: this file must import cleanly so `pytest tests/v2` collects all 12+
probes' worth of test classes here without errors.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.predeploy_probe_suite import (
    _match_one,
    _read_version,
    check_config_filled,
    detect_regressions,
    evaluate_across_runs,
    evaluate_probe,
    load_baseline,
    load_config,
    write_baseline,
)


# ---------------------------------------------------------------------------
# _match_one: every rule kind, positive + negative
# ---------------------------------------------------------------------------

class MatchOneAnswerNotEmpty(unittest.TestCase):
    def test_pass_when_answer_has_content(self):
        r = _match_one({"kind": "answer_not_empty"}, {"answer": "hello"}, 1.0)
        self.assertTrue(r.ok)

    def test_fail_when_answer_blank(self):
        r = _match_one({"kind": "answer_not_empty"}, {"answer": "   "}, 1.0)
        self.assertFalse(r.ok)

    def test_fail_when_answer_missing(self):
        r = _match_one({"kind": "answer_not_empty"}, {}, 1.0)
        self.assertFalse(r.ok)


class MatchOneContains(unittest.TestCase):
    def test_pass(self):
        r = _match_one({"kind": "contains", "value": "Wilde"},
                       {"answer": "Oscar Wilde was here"}, 0.5)
        self.assertTrue(r.ok)

    def test_fail(self):
        r = _match_one({"kind": "contains", "value": "Wilde"},
                       {"answer": "no such name"}, 0.5)
        self.assertFalse(r.ok)


class MatchOneNotContains(unittest.TestCase):
    def test_pass(self):
        r = _match_one({"kind": "not_contains", "value": "$s2.words"},
                       {"answer": "real word"}, 0.5)
        self.assertTrue(r.ok)

    def test_fail_when_forbidden_literal_present(self):
        r = _match_one({"kind": "not_contains", "value": "$s2.words"},
                       {"answer": "### $s2.words[4]"}, 0.5)
        self.assertFalse(r.ok)


class MatchOneRegexMatch(unittest.TestCase):
    def test_pass(self):
        r = _match_one({"kind": "regex_match", "pattern": r"IPA:"},
                       {"answer": "IPA: /sword/"}, 0.5)
        self.assertTrue(r.ok)

    def test_fail(self):
        r = _match_one({"kind": "regex_match", "pattern": r"IPA:"},
                       {"answer": "no transcription"}, 0.5)
        self.assertFalse(r.ok)


class MatchOneRegexNoMatch(unittest.TestCase):
    """The two universal guards from predeploy_probes.json — keep them honest."""

    def test_plan_spec_variable_forbidden(self):
        rule = {"kind": "regex_no_match", "pattern": r"\$s\d+\.[A-Za-z_][A-Za-z0-9_]*"}
        ok = _match_one(rule, {"answer": "clean reply"}, 0.5)
        fail = _match_one(rule, {"answer": "### $s2.words[0]"}, 0.5)
        self.assertTrue(ok.ok)
        self.assertFalse(fail.ok)
        self.assertIn("$s2.words", fail.reason)

    def test_literal_none_forbidden(self):
        rule = {"kind": "regex_no_match", "pattern": r"(?<![A-Za-z])None(?![A-Za-z])"}
        # Standalone None (Phase 6 violation) — fail.
        self.assertFalse(_match_one(rule, {"answer": "value: None"}, 0.5).ok)
        # Embedded in a word — pass (e.g., "Noneness" would be a real word).
        self.assertTrue(_match_one(rule, {"answer": "Nonentity"}, 0.5).ok)
        # Clean reply — pass.
        self.assertTrue(_match_one(rule, {"answer": "value: 42"}, 0.5).ok)


class MatchOneIntent(unittest.TestCase):
    def test_intent_in_pass(self):
        r = _match_one({"kind": "intent_in", "values": ["out_of_scope"]},
                       {"intent": "OUT_OF_SCOPE"}, 0.1)
        self.assertTrue(r.ok)  # case-insensitive

    def test_intent_in_fail(self):
        r = _match_one({"kind": "intent_in", "values": ["out_of_scope"]},
                       {"intent": "clarify"}, 0.1)
        self.assertFalse(r.ok)

    def test_intent_not_in_blocks_clarify(self):
        r = _match_one({"kind": "intent_not_in", "values": ["clarify"]},
                       {"intent": "clarify"}, 0.1)
        self.assertFalse(r.ok)
        r = _match_one({"kind": "intent_not_in", "values": ["clarify"]},
                       {"intent": "author_vocab"}, 0.1)
        self.assertTrue(r.ok)


class MatchOneLatency(unittest.TestCase):
    def test_under_cap_passes(self):
        r = _match_one({"kind": "latency_under_s", "value": 60}, {}, elapsed=45.0)
        self.assertTrue(r.ok)

    def test_at_or_over_cap_fails(self):
        r = _match_one({"kind": "latency_under_s", "value": 60}, {}, elapsed=60.0)
        self.assertFalse(r.ok)
        r = _match_one({"kind": "latency_under_s", "value": 60}, {}, elapsed=120.0)
        self.assertFalse(r.ok)


class MatchOneToolCalled(unittest.TestCase):
    def test_called_pass(self):
        r = _match_one({"kind": "tool_called", "name": "affinity_by_author"},
                       {"tool_calls": [{"name": "affinity_by_author"}]}, 0.1)
        self.assertTrue(r.ok)

    def test_called_fail(self):
        r = _match_one({"kind": "tool_called", "name": "affinity_by_author"},
                       {"tool_calls": [{"name": "word_contexts"}]}, 0.1)
        self.assertFalse(r.ok)

    def test_not_called_blocks_forbidden_tool(self):
        r = _match_one({"kind": "tool_not_called", "name": "rag_query"},
                       {"tool_calls": [{"name": "rag_query"}]}, 0.1)
        self.assertFalse(r.ok)
        r = _match_one({"kind": "tool_not_called", "name": "rag_query"},
                       {"tool_calls": []}, 0.1)
        self.assertTrue(r.ok)


class MatchOneUnknownKind(unittest.TestCase):
    def test_unknown_kind_fails_loudly(self):
        r = _match_one({"kind": "made_up_rule"}, {}, 0.1)
        self.assertFalse(r.ok)
        self.assertIn("unknown rule kind", r.reason)


# ---------------------------------------------------------------------------
# evaluate_probe
# ---------------------------------------------------------------------------

class EvaluateProbeCombinesUniversalAndPerProbe(unittest.TestCase):
    """Universal guards apply first, then per-probe `pass_when`. All must hold."""

    UNIVERSAL = [
        {"kind": "answer_not_empty"},
        {"kind": "regex_no_match", "field": "answer", "pattern": r"\$s\d+\."},
    ]

    def test_passes_when_universal_and_probe_specific_both_hold(self):
        probe = {
            "id": "P12",
            "error_class": "E13",
            "pass_when": [{"kind": "intent_not_in", "values": ["clarify"]}],
        }
        payload = {"answer": "Wilde's signature words: ...", "intent": "author_vocab"}
        passed, reasons = evaluate_probe(probe, self.UNIVERSAL, payload, 5.0, None)
        self.assertTrue(passed, reasons)

    def test_fails_on_universal_alone(self):
        probe = {"id": "P1", "error_class": "E1", "pass_when": []}
        payload = {"answer": "### $s2.words[0]", "intent": "ok"}
        passed, reasons = evaluate_probe(probe, self.UNIVERSAL, payload, 5.0, None)
        self.assertFalse(passed)
        self.assertEqual(len(reasons), 1)

    def test_collects_all_failure_reasons(self):
        probe = {
            "id": "P12",
            "error_class": "E13",
            "pass_when": [{"kind": "intent_not_in", "values": ["clarify"]}],
        }
        payload = {"answer": "$s2.words", "intent": "clarify"}
        passed, reasons = evaluate_probe(probe, self.UNIVERSAL, payload, 5.0, None)
        self.assertFalse(passed)
        # Two failures: universal regex_no_match + probe intent_not_in.
        self.assertEqual(len(reasons), 2)

    def test_transport_error_short_circuits(self):
        probe = {"id": "P1", "error_class": "E1", "pass_when": []}
        passed, reasons = evaluate_probe(probe, self.UNIVERSAL, {}, 5.0, "HTTP 502")
        self.assertFalse(passed)
        self.assertEqual(reasons, ["transport: HTTP 502"])


# ---------------------------------------------------------------------------
# detect_regressions: ONLY PASS->FAIL counts, per W-18 acceptance criterion
# ---------------------------------------------------------------------------

class DetectRegressions(unittest.TestCase):
    def _results(self, mapping: dict) -> list[dict]:
        return [{"id": pid, "passed": v == "PASS"} for pid, v in mapping.items()]

    def test_no_baseline_no_regressions(self):
        results = self._results({"P1": "PASS", "P2": "FAIL"})
        self.assertEqual(detect_regressions(None, results), [])

    def test_pass_to_fail_is_regression(self):
        baseline = {"verdicts": {"P1": "PASS", "P2": "PASS"}}
        results = self._results({"P1": "PASS", "P2": "FAIL"})
        self.assertEqual(detect_regressions(baseline, results), ["P2"])

    def test_fail_to_pass_is_not_regression(self):
        baseline = {"verdicts": {"P1": "FAIL"}}
        results = self._results({"P1": "PASS"})
        self.assertEqual(detect_regressions(baseline, results), [])

    def test_fail_to_fail_is_not_regression(self):
        """Long-broken probes don't count — W-18 blocks only on transitions."""
        baseline = {"verdicts": {"P1": "FAIL"}}
        results = self._results({"P1": "FAIL"})
        self.assertEqual(detect_regressions(baseline, results), [])

    def test_new_probe_id_not_in_baseline_is_not_regression(self):
        baseline = {"verdicts": {"P1": "PASS"}}
        results = self._results({"P1": "PASS", "P12": "FAIL"})
        self.assertEqual(detect_regressions(baseline, results), [])


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _minimal_probe(pid: str, klass: str, question: str = "filled") -> dict:
    return {"id": pid, "error_class": klass, "title": "t", "question": question, "pass_when": []}


def _write_json(tmpdir: Path, name: str, payload: dict) -> Path:
    p = tmpdir / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class LoadConfig(unittest.TestCase):
    def test_loads_well_formed_config_with_12_probes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_payload = {
                "schema_version": 1,
                "universal_pass_when": [],
                "probes": [_minimal_probe(f"P{i}", f"E{i}") for i in range(1, 13)],
            }
            p = _write_json(tdp, "cfg.json", cfg_payload)
            cfg = load_config(p)
            self.assertEqual(len(cfg["probes"]), 12)

    def test_dies_on_wrong_count(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            payload = {"probes": [_minimal_probe("P1", "E1")]}
            p = _write_json(tdp, "cfg.json", payload)
            with self.assertRaises(SystemExit):
                load_config(p)

    def test_dies_on_bad_probe_id(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            probes = [_minimal_probe(f"P{i}", f"E{i}") for i in range(1, 13)]
            probes[0]["id"] = "P13"  # out of range
            payload = {"probes": probes}
            p = _write_json(tdp, "cfg.json", payload)
            with self.assertRaises(SystemExit):
                load_config(p)

    def test_dies_on_duplicate_probe_id(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            probes = [_minimal_probe(f"P{i}", f"E{i}") for i in range(1, 13)]
            probes[1]["id"] = "P1"  # dup
            payload = {"probes": probes}
            p = _write_json(tdp, "cfg.json", payload)
            with self.assertRaises(SystemExit):
                load_config(p)

    def test_dies_on_missing_file(self):
        with self.assertRaises(SystemExit):
            load_config(Path("/no/such/file.json"))


class CheckConfigFilled(unittest.TestCase):
    def test_flags_fill_from_source_slots(self):
        cfg = {
            "probes": [
                {"id": "P1", "question": "real text"},
                {"id": "P2", "question": "__FILL_FROM_SOURCE__"},
                {"id": "P3", "question": ""},
                {"id": "P4", "question": "another real one"},
            ],
        }
        self.assertEqual(check_config_filled(cfg), ["P2", "P3"])

    def test_returns_empty_when_all_filled(self):
        cfg = {"probes": [{"id": f"P{i}", "question": f"q{i}"} for i in range(1, 13)]}
        self.assertEqual(check_config_filled(cfg), [])


# ---------------------------------------------------------------------------
# Baseline roundtrip
# ---------------------------------------------------------------------------

class BaselineRoundtrip(unittest.TestCase):
    def test_write_then_load_preserves_verdicts_and_version(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            results = [
                {"id": "P1", "passed": True},
                {"id": "P2", "passed": False},
            ]
            write_baseline(p, "2.6.2", results)
            loaded = load_baseline(p)
            self.assertEqual(loaded["version"], "2.6.2")
            self.assertEqual(loaded["verdicts"], {"P1": "PASS", "P2": "FAIL"})
            self.assertIn("recorded_at", loaded)

    def test_load_missing_baseline_returns_none(self):
        self.assertIsNone(load_baseline(Path("/no/such/baseline.json")))

    def test_load_corrupt_baseline_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            p.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(load_baseline(p))


# ---------------------------------------------------------------------------
# Version reader
# ---------------------------------------------------------------------------

class ReadVersion(unittest.TestCase):
    def test_reads_scripts_v2_version(self):
        v = _read_version()
        # The repo ships ANALYTICS_VERSION as a dotted string like "2.6.1".
        self.assertRegex(v, r"^\d+\.\d+(\.\d+)?")


# ---------------------------------------------------------------------------
# Repo-config sanity: the shipped scripts/predeploy_probes.json must be a
# valid 12-probe config — even if the question slots are still
# __FILL_FROM_SOURCE__ today, the shape itself shouldn't drift.
# ---------------------------------------------------------------------------

class ShippedConfigShape(unittest.TestCase):
    def test_shipped_config_loads_and_has_12_probes(self):
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        self.assertEqual([p["id"] for p in cfg["probes"]],
                         [f"P{i}" for i in range(1, 13)])

    def test_shipped_config_has_universal_guards(self):
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        kinds = {rule.get("kind") for rule in cfg.get("universal_pass_when", [])}
        self.assertIn("regex_no_match", kinds)
        self.assertIn("answer_not_empty", kinds)

    def test_shipped_config_p12_uses_repeat_for_determinism(self):
        """P12 (E12 — renderer non-determinism) must run 3 times and check
        determinism across runs. Source: error_taxonomy_probe_suite §3 table."""
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        p12 = next(p for p in cfg["probes"] if p["id"] == "P12")
        self.assertEqual(p12.get("repeat"), 3)
        across_kinds = {r["kind"] for r in p12.get("pass_when_across_runs", [])}
        self.assertIn("same_intent_across_runs", across_kinds)


# ---------------------------------------------------------------------------
# S-R5-E11 — tool_called / tool_not_called rules must name a REGISTERED tool.
#
# The probe matcher checks `rule["name"] in [tc["name"] for tc in tool_calls]`
# (predeploy_probe_suite._match_one, kind="tool_called"). `tool_calls` carries
# the names of the tools the dispatcher actually executed — i.e. registry tool
# names. So a probe that asserts a name no tool is registered under can NEVER
# pass: it is dead-on-arrival, not a real gate.
#
# E11 shipped exactly that bug: P11 asserted `tool_called: book_similar`, but
# `book_similar` is an *intent label*, not a tool — `_plan_book_similar`
# dispatches the registered tool `find_book_by_topic` (topic-enriched). This
# test fails on the pre-fix config (book_similar ∉ REGISTRY) and passes once
# the probe names the tool that actually runs.
# ---------------------------------------------------------------------------

class ToolCalledNamesAreRegistered(unittest.TestCase):
    _TOOL_RULE_KINDS = {"tool_called", "tool_not_called"}

    def _registry_names(self):
        import scripts.v2.tools  # noqa: F401  triggers @tool registration
        from scripts.v2.tool_registry import REGISTRY
        return set(REGISTRY.keys())

    def _tool_rules(self, cfg):
        """Yield (probe_id, rule) for every tool_called/tool_not_called rule
        across per-probe pass_when, pass_when_across_runs, and universal."""
        for rule in cfg.get("universal_pass_when", []):
            if rule.get("kind") in self._TOOL_RULE_KINDS:
                yield "universal", rule
        for probe in cfg.get("probes", []):
            pid = probe.get("id")
            for key in ("pass_when", "pass_when_across_runs"):
                for rule in probe.get(key, []):
                    if rule.get("kind") in self._TOOL_RULE_KINDS:
                        yield pid, rule

    def test_every_tool_rule_names_a_registered_tool(self):
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        names = self._registry_names()
        offenders = [
            (pid, rule["name"]) for pid, rule in self._tool_rules(cfg)
            if rule.get("name") not in names
        ]
        self.assertEqual(
            offenders, [],
            f"probe tool_called/tool_not_called rules name unregistered tools "
            f"(intent labels are not tools): {offenders}. "
            f"Assert the tool the dispatcher actually runs.",
        )

    def test_p11_routes_to_find_book_by_topic(self):
        """Regression guard for S-R5-E11: P11 must assert the tool that
        _plan_book_similar dispatches (find_book_by_topic), not the intent
        name book_similar."""
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        p11 = next(p for p in cfg["probes"] if p["id"] == "P11")
        tool_rules = [r for r in p11["pass_when"] if r.get("kind") == "tool_called"]
        self.assertEqual(
            [r["name"] for r in tool_rules], ["find_book_by_topic"],
            "P11 must assert tool_called=find_book_by_topic (book_similar is an "
            "intent, not a tool — it can never appear in tool_calls).",
        )
        self.assertNotIn(
            "book_similar", [r.get("name") for r in tool_rules],
            "book_similar is an intent label, not a registry tool name.",
        )


# ---------------------------------------------------------------------------
# S-R5-E8 — P8 «0 слов» guard must have a left word-boundary.
#
# P8 asks for «20 слов уровня B2 …». Its regex_no_match guard rejects
# empty-result answers («0 слов», «не найдено», …). The original
# alternative `0 слов` had no left boundary, so it matched the substring
# inside «20 слов» / «100 слов» — i.e. a healthy answer echoing the count
# tripped the guard (probe false-positive; learning pipeline was fine).
# Fix: `(?<!\d)0 слов`. This test runs the SHIPPED P8 rule through
# _match_one and asserts the false-positive cases pass while a standalone
# «0 слов» (a real empty result) still fails. Fails on the pre-fix pattern.
# ---------------------------------------------------------------------------

class P8EmptyGuardHasLeftBoundary(unittest.TestCase):
    def _p8_no_match_rule(self):
        repo_cfg = Path(__file__).resolve().parents[2] / "scripts" / "predeploy_probes.json"
        cfg = load_config(repo_cfg)
        p8 = next(p for p in cfg["probes"] if p["id"] == "P8")
        rule = next(r for r in p8["pass_when"]
                    if r.get("kind") == "regex_no_match" and r.get("field") == "answer")
        return rule

    def test_count_echo_does_not_trip_guard(self):
        """«20 слов»/«100 слов» — healthy answers echoing the requested
        count must PASS the no-match guard (ok=True)."""
        rule = self._p8_no_match_rule()
        for answer in ("Вот 20 слов уровня B2 из Pride and Prejudice: ...",
                       "Нашёл 100 слов, экспортирую."):
            with self.subTest(answer=answer):
                r = _match_one(rule, {"answer": answer}, 1.0)
                self.assertTrue(
                    r.ok,
                    f"P8 guard false-positive on count echo: {answer!r} "
                    f"({r.reason})",
                )

    def test_standalone_zero_still_caught(self):
        """A genuine «0 слов» empty result must still FAIL (ok=False) —
        the fix narrows the boundary, it does not disarm the guard."""
        rule = self._p8_no_match_rule()
        for answer in ("Найдено: 0 слов уровня B2.", "0 слов"):
            with self.subTest(answer=answer):
                r = _match_one(rule, {"answer": answer}, 1.0)
                self.assertFalse(
                    r.ok,
                    f"P8 guard must still catch a real empty: {answer!r}",
                )

    def test_other_empty_markers_still_caught(self):
        """The other alternatives are unaffected by the lookbehind."""
        rule = self._p8_no_match_rule()
        for answer in ("К сожалению, не найдено подходящих слов.",
                       "Результат: пусто.", "no words matched"):
            with self.subTest(answer=answer):
                r = _match_one(rule, {"answer": answer}, 1.0)
                self.assertFalse(r.ok, f"empty marker not caught: {answer!r}")


# ---------------------------------------------------------------------------
# Determinism matchers (P12 class)
# ---------------------------------------------------------------------------

class MatchAcrossRunsSameIntent(unittest.TestCase):
    def _eval(self, intents: list[str]) -> tuple[bool, list[str]]:
        probe = {"pass_when_across_runs": [{"kind": "same_intent_across_runs"}]}
        payloads = [{"intent": i} for i in intents]
        reasons = evaluate_across_runs(probe, payloads)
        return (not reasons), reasons

    def test_all_same_intent_passes(self):
        ok, _ = self._eval(["learning", "learning", "learning"])
        self.assertTrue(ok)

    def test_intent_flip_fails(self):
        ok, reasons = self._eval(["learning", "export_to_markdown", "learning"])
        self.assertFalse(ok)
        self.assertIn("flipped", reasons[0])

    def test_case_insensitive(self):
        ok, _ = self._eval(["Learning", "learning", "LEARNING"])
        self.assertTrue(ok)


class MatchAcrossRunsSameContains(unittest.TestCase):
    def _eval(self, answers: list[str], value: str) -> tuple[bool, list[str]]:
        probe = {"pass_when_across_runs": [
            {"kind": "same_contains_across_runs", "field": "answer", "value": value},
        ]}
        payloads = [{"answer": a} for a in answers]
        reasons = evaluate_across_runs(probe, payloads)
        return (not reasons), reasons

    def test_always_present_passes(self):
        ok, _ = self._eval([
            "архаизм disclosure here",
            "архаизм disclosure again",
            "архаизм note shown",
        ], "архаизм")
        self.assertTrue(ok)

    def test_always_absent_passes(self):
        ok, _ = self._eval(["plain", "plain again", "plain still"], "архаизм")
        self.assertTrue(ok)

    def test_flicker_fails(self):
        ok, reasons = self._eval([
            "архаизм present",
            "no note",
            "архаизм again",
        ], "архаизм")
        self.assertFalse(ok)
        self.assertIn("flips across runs", reasons[0])


class EvaluateAcrossRunsIsNoopForSingleRun(unittest.TestCase):
    def test_no_rules_returns_empty(self):
        probe = {}
        self.assertEqual(evaluate_across_runs(probe, [{"intent": "x"}]), [])

    def test_single_payload_skips_check(self):
        probe = {"pass_when_across_runs": [{"kind": "same_intent_across_runs"}]}
        self.assertEqual(evaluate_across_runs(probe, [{"intent": "x"}]), [])


class MatchAcrossRunsUnknownKind(unittest.TestCase):
    def test_unknown_kind_fails(self):
        probe = {"pass_when_across_runs": [{"kind": "made_up"}]}
        reasons = evaluate_across_runs(probe, [{"intent": "a"}, {"intent": "a"}])
        self.assertEqual(len(reasons), 1)
        self.assertIn("unknown across-runs", reasons[0])


if __name__ == "__main__":
    unittest.main()
