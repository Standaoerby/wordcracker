"""Unit tests for the critic pass — verifies the no-LLM bits and the
network-failure fallback. The full LLM round-trip stays behind the
RUN_LLM_TESTS flag because it costs Ollama time."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import critic
from scripts.v2.critic import (
    CriticVerdict, _build_payload_for_critic, _shrink,
    annotate_answer, review,
)


class CriticVerdictBuilders(unittest.TestCase):
    def test_trust_default(self):
        v = CriticVerdict.trust()
        self.assertTrue(v.verified)
        self.assertEqual(v.unsupported_claims, [])
        self.assertFalse(v.has_issues())

    def test_has_issues_when_claims(self):
        v = CriticVerdict(verified=False, unsupported_claims=["frequency 8099"],
                          missing_caveats=[], summary="bad number")
        self.assertTrue(v.has_issues())


class AnnotateAnswer(unittest.TestCase):
    def test_clean_verdict_no_changes(self):
        answer = "Topic: foo"
        v = CriticVerdict.trust()
        self.assertEqual(annotate_answer(answer, v), answer)

    def test_unsupported_claim_appended(self):
        v = CriticVerdict(verified=False,
                          unsupported_claims=["Holmes appears 4045 times",
                                              "ratio 122.21"],
                          missing_caveats=[], summary="numbers unverified")
        out = annotate_answer("Conan Doyle uses Holmes a lot.", v)
        self.assertIn("⚠️", out)
        self.assertIn("4045", out)
        self.assertIn("122.21", out)
        self.assertIn("Critic:", out)

    def test_caveat_block(self):
        v = CriticVerdict(verified=True, unsupported_claims=[],
                          missing_caveats=["only 5 books matched"],
                          summary="low coverage")
        out = annotate_answer("Some answer.", v)
        self.assertIn("ℹ️", out)
        self.assertIn("only 5 books", out)

    def test_at_most_5_claims_shown(self):
        v = CriticVerdict(verified=False,
                          unsupported_claims=[f"claim {i}" for i in range(10)],
                          missing_caveats=[], summary="lots")
        out = annotate_answer("Answer.", v)
        # claim 6..9 omitted to keep the block readable
        self.assertIn("claim 0", out)
        self.assertIn("claim 4", out)
        self.assertNotIn("claim 9", out)


class PayloadBuilder(unittest.TestCase):
    def test_shrinks_large_data(self):
        big = {"items": list(range(1000))}
        out = _build_payload_for_critic("answer", [
            {"tool": "x", "ok": True, "data": big, "coverage": {}, "warnings": []}
        ], intent="test")
        # Data field should have been shrunk to a string
        self.assertEqual(len(out["tool_results"]), 1)
        v = out["tool_results"][0]["data"]
        if isinstance(v, str):
            self.assertIn("truncated", v)
        else:
            # If it stayed as dict, it must serialize to <= 600 chars
            self.assertLessEqual(len(json.dumps(v, default=str)), 600)

    def test_caps_tool_results_at_tool_calls_max(self):
        """B119 (R-28): cap 8 → 12 (= RequestBudget.tool_calls_max) —
        старый [:8] молча выкидывал хвост 11-шагового learning_books
        плана из trusted set критика."""
        many = [{"tool": f"t{i}", "ok": True, "data": {},
                 "coverage": {}, "warnings": []} for i in range(20)]
        out = _build_payload_for_critic("a", many, intent="x")
        self.assertEqual(len(out["tool_results"]), 12)

    def test_truncates_long_answer(self):
        very_long = "x" * 10000
        out = _build_payload_for_critic(very_long, [
            {"tool": "x", "ok": True, "data": {}, "coverage": {}, "warnings": []}
        ], intent="t")
        self.assertEqual(len(out["answer"]), 3000)


class ReviewFallbacks(unittest.TestCase):
    """Cover the early-return paths so we never accidentally break on prod."""

    def test_disabled_returns_trust(self):
        with mock.patch.object(critic, "CRITIC_ENABLED", False):
            v = review("hello", [{"tool": "x", "data": {}}])
            self.assertTrue(v.verified)

    def test_empty_answer_returns_trust(self):
        v = review("", [{"tool": "x", "data": {}}])
        self.assertTrue(v.verified)

    def test_no_tool_results_returns_trust(self):
        """Intro / clarify / out_of_scope answers have no tool data to verify
        against — critic is a no-op for them."""
        v = review("intro text", [])
        self.assertTrue(v.verified)

    def test_network_error_falls_through(self):
        import requests
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post",
                           side_effect=requests.exceptions.ConnectionError("boom")):
            v = review("answer", [{"tool": "x", "data": {"y": 1},
                                   "coverage": {}, "warnings": []}],
                       intent="test")
            self.assertTrue(v.verified)  # trust fallback
            self.assertEqual(v.summary, "(critic disabled)")

    def test_bad_json_falls_through(self):
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": "not json at all"}}
            v = review("answer", [{"tool": "x", "data": {"y": 1},
                                   "coverage": {}, "warnings": []}],
                       intent="test")
            self.assertTrue(v.verified)


class CriticOverflagGuard(unittest.TestCase):
    """When the critic returns >4 unsupported claims, suppress the warning —
    it's almost certainly confused on a table-heavy answer."""

    def test_over_threshold_returns_trust(self):
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": json.dumps({
                    "verified": False,
                    "unsupported_claims": [f"claim {i}" for i in range(8)],
                    "missing_caveats": [],
                    "summary": "many things wrong",
                })}
            }
            v = review("table with 10 rows",
                       [{"tool": "affinity_by_author", "data": {"top_words": []},
                         "coverage": {}, "warnings": []}],
                       intent="author_vocab")
            # Guard activates: verified=True despite 8 flags, claims emptied,
            # summary mentions the suppression count.
            self.assertTrue(v.verified)
            self.assertEqual(v.unsupported_claims, [])
            self.assertIn("8", v.summary)
            self.assertIn("suppressed", v.summary)

    def test_intent_skip_returns_trust(self):
        """Sprint 11.1: critic skips entirely for table-data intents."""
        for intent in ("learning", "top_authors_books", "vocab_passport"):
            v = review("table answer",
                       [{"tool": "x", "data": {"rows": []},
                         "coverage": {}, "warnings": []}],
                       intent=intent)
            self.assertTrue(v.verified, msg=f"intent={intent} should skip")
            self.assertIn("skipped", v.summary)

    def test_borderline_3_flags_now_trusted(self):
        """MAX_FLAGS tightened 4 → 2 in Sprint 11.1 — even 3 flags = trust."""
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": json.dumps({
                    "verified": False,
                    "unsupported_claims": ["a", "b", "c"],
                    "missing_caveats": [],
                    "summary": "three weak",
                })}
            }
            v = review("hello",
                       [{"tool": "x", "data": {"y": 1},
                         "coverage": {}, "warnings": []}],
                       intent="author_compare")
            self.assertTrue(v.verified)
            self.assertEqual(v.unsupported_claims, [])

    def test_under_threshold_keeps_flags(self):
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": json.dumps({
                    "verified": False,
                    "unsupported_claims": ["one real claim"],
                    "missing_caveats": [],
                    "summary": "one bad number",
                })}
            }
            v = review("hello",
                       [{"tool": "x", "data": {"y": 1},
                         "coverage": {}, "warnings": []}],
                       intent="test")
            self.assertFalse(v.verified)
            self.assertEqual(len(v.unsupported_claims), 1)


class ReviewHappyPath(unittest.TestCase):
    def test_parses_verdict_json(self):
        with mock.patch.object(critic, "CRITIC_ENABLED", True), \
                mock.patch("scripts.v2.critic.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": json.dumps({
                    "verified": False,
                    "unsupported_claims": ["frequency 9999 not in data"],
                    "missing_caveats": ["coverage only 12 books"],
                    "summary": "two numeric claims unverified",
                })}
            }
            v = review("Holmes used 9999 times.",
                       [{"tool": "affinity_by_author", "data": {"top_words": []},
                         "coverage": {}, "warnings": []}],
                       intent="author_vocab")
            self.assertFalse(v.verified)
            self.assertEqual(len(v.unsupported_claims), 1)
            self.assertIn("9999", v.unsupported_claims[0])
            self.assertEqual(len(v.missing_caveats), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
