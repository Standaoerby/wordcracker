"""Tests for the v2 observability ring buffer + JSONL writer."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import observability as obs


class RingBuffer(unittest.TestCase):
    def setUp(self):
        obs._reset()

    def test_log_appends_to_ring(self):
        obs.log_request({"intent": "x"})
        records = obs.recent_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["intent"], "x")
        self.assertIn("ts", records[0])
        self.assertIn("request_id", records[0])

    def test_ring_max_size(self):
        # Default RING_SIZE is 256; push 300 and ensure cap holds.
        for i in range(300):
            obs.log_request({"intent": f"i{i}", "total_elapsed_ms": i})
        records = obs.recent_records()
        self.assertEqual(len(records), 256)
        # Latest entries kept
        self.assertEqual(records[-1]["intent"], "i299")


class Aggregate(unittest.TestCase):
    def setUp(self):
        obs._reset()
        for i, intent in enumerate(["a", "a", "b", "a", "c"]):
            obs.log_request({
                "intent": intent,
                "total_elapsed_ms": 100 * (i + 1),
                "tool_calls": [
                    {"name": "find_book", "runtime_ms": 50 * (i + 1),
                     "ok": True, "cache_hit": (i % 2 == 0)},
                ],
                "critic_unsupported_n": 1 if i == 0 else 0,
            })

    def test_intent_distribution(self):
        agg = obs.aggregate_recent()
        self.assertEqual(agg["total"], 5)
        self.assertEqual(agg["intents"]["a"], 3)
        self.assertEqual(agg["intents"]["b"], 1)

    def test_cache_hit_rate(self):
        agg = obs.aggregate_recent()
        # 3 of 5 calls are cache_hit (i=0,2,4)
        self.assertAlmostEqual(agg["cache_hit_rate"], 0.6)
        self.assertEqual(agg["cache_hits"], 3)
        self.assertEqual(agg["cache_calls"], 5)

    def test_slow_tools_sorted(self):
        agg = obs.aggregate_recent()
        self.assertEqual(agg["slow_tools"][0]["tool"], "find_book")
        self.assertGreater(agg["slow_tools"][0]["p95_ms"], 0)

    def test_critic_flagged_count(self):
        agg = obs.aggregate_recent()
        self.assertEqual(agg["critic_flagged"], 1)


class DiskWriter(unittest.TestCase):
    def setUp(self):
        obs._reset()
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = obs.LOG_DIR
        obs.LOG_DIR = Path(self.tmp.name)

    def tearDown(self):
        obs.LOG_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_writes_jsonl(self):
        obs.log_request({"intent": "diskcheck", "total_elapsed_ms": 100})
        # File created with today's UTC date
        files = list(Path(self.tmp.name).glob("queries-*.jsonl"))
        self.assertEqual(len(files), 1)
        line = files[0].read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        self.assertEqual(rec["intent"], "diskcheck")

    def test_unwriteable_dir_does_not_crash(self):
        obs.LOG_DIR = Path("/nope/cannot/write")
        # Should log warning, not raise.
        obs.log_request({"intent": "x"})
        # Ring still got it
        self.assertEqual(len(obs.recent_records()), 1)


class TopFailedPhrases(unittest.TestCase):
    """Sprint 14: aggregate failed queries to identify the «top 10 things
    to fix» — Stan's regex-synthesis pass."""

    def setUp(self):
        obs._reset()

    def test_empty(self):
        self.assertEqual(obs.top_failed_phrases(), [])

    def test_groups_by_normalized_phrase(self):
        for _ in range(3):
            obs.log_request({"is_failure": True, "failure_kind": "clarify",
                              "intent": "clarify",
                              "question_truncated": "Помоги"})
        obs.log_request({"is_failure": True, "failure_kind": "clarify",
                          "intent": "clarify",
                          "question_truncated": "помоги "})
        obs.log_request({"is_failure": True, "failure_kind": "out_of_scope",
                          "intent": "out_of_scope",
                          "question_truncated": "напиши стих"})
        top = obs.top_failed_phrases(top_n=10)
        # «Помоги», «помоги», «помоги » all normalize to "помоги"
        helpful = next(r for r in top if "помог" in r["phrase"].lower())
        self.assertEqual(helpful["count"], 4)
        self.assertEqual(helpful["kinds"], {"clarify": 4})

    def test_sorted_by_count(self):
        for _ in range(5):
            obs.log_request({"is_failure": True, "failure_kind": "clarify",
                              "intent": "clarify",
                              "question_truncated": "freq query"})
        for _ in range(2):
            obs.log_request({"is_failure": True, "failure_kind": "clarify",
                              "intent": "clarify",
                              "question_truncated": "rare query"})
        top = obs.top_failed_phrases()
        self.assertEqual(top[0]["count"], 5)
        self.assertEqual(top[1]["count"], 2)

    def test_excludes_successful_records(self):
        obs.log_request({"intent": "author_vocab"})  # success
        obs.log_request({"is_failure": True, "failure_kind": "clarify",
                          "question_truncated": "fail query"})
        top = obs.top_failed_phrases()
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["phrase"], "fail query")

    def test_respects_top_n_limit(self):
        for i in range(15):
            obs.log_request({"is_failure": True, "failure_kind": "clarify",
                              "intent": "clarify",
                              "question_truncated": f"unique q {i}"})
        top = obs.top_failed_phrases(top_n=5)
        self.assertEqual(len(top), 5)


class RecentFailures(unittest.TestCase):
    """v2.7: admin endpoint pulls is_failure rows from the ring buffer."""

    def setUp(self):
        obs._reset()

    def test_empty_when_no_fails(self):
        obs.log_request({"intent": "author_vocab"})
        self.assertEqual(obs.recent_failures(), [])

    def test_pulls_only_failures(self):
        obs.log_request({"intent": "author_vocab"})  # success
        obs.log_request({"intent": "clarify", "is_failure": True,
                         "failure_kind": "clarify",
                         "question_truncated": "ну привет"})
        obs.log_request({"intent": "out_of_scope", "is_failure": True,
                         "failure_kind": "out_of_scope",
                         "question_truncated": "напиши стих"})
        fails = obs.recent_failures()
        self.assertEqual(len(fails), 2)
        # newest first
        self.assertEqual(fails[0]["question_truncated"], "напиши стих")

    def test_respects_limit(self):
        for i in range(30):
            obs.log_request({"intent": "clarify", "is_failure": True,
                              "failure_kind": "clarify",
                              "question_truncated": f"q{i}"})
        fails = obs.recent_failures(limit=10)
        self.assertEqual(len(fails), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
