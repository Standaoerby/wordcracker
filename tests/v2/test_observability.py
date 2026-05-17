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


if __name__ == "__main__":
    unittest.main(verbosity=2)
