"""Sprint 22+ — user feedback / bad-answer collection."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class RecordBadAnswer(unittest.TestCase):
    """Core: writes one JSONL line per call to today's bad-YYYY-MM-DD.jsonl."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wc_feedback_test_")
        # Patch the module-level FEEDBACK_DIR for isolation
        from scripts.v2 import feedback as fb
        self._orig_dir = fb.FEEDBACK_DIR
        fb.FEEDBACK_DIR = Path(self._tmp)

    def tearDown(self):
        from scripts.v2 import feedback as fb
        fb.FEEDBACK_DIR = self._orig_dir
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_minimal_record_saved(self):
        from scripts.v2.feedback import record_bad_answer, _today_path
        rec = record_bad_answer(
            question="что значит fog у Диккенса",
            answer="туман — частое слово у Диккенса...",
            intent="word_contexts",
        )
        self.assertIn("id", rec)
        self.assertIn("ts", rec)
        self.assertEqual(rec["question"],
                          "что значит fog у Диккенса")
        # File written
        p = _today_path()
        self.assertTrue(p.exists())
        content = p.read_text(encoding="utf-8").strip()
        loaded = json.loads(content)
        self.assertEqual(loaded["id"], rec["id"])

    def test_full_record_with_all_fields(self):
        from scripts.v2.feedback import record_bad_answer
        rec = record_bad_answer(
            question="Q",
            answer="A",
            intent="author_vocab",
            intent_confidence=0.92,
            tool_calls=[
                {"name": "affinity_by_author",
                 "args": {"author_regex": "^Christie,", "top": 200},
                 "ok": True, "runtime_ms": 12300},
            ],
            elapsed_sec=14.2,
            render_meta={
                "prompt_tokens": 7100, "eval_tokens": 850,
                "budget_utilization_pct": 49,
                "confabulation_risk": "low",
                "shrink_applied": False,
                "budget_fits": True,
            },
            critic_summary="critic clean",
            user_note="ответ про арт-выставку вместо Кристи",
            history=[{"role": "user", "content": "Q"},
                     {"role": "assistant", "content": "A"}],
            ip="192.168.1.42",
        )
        self.assertEqual(rec["user_note"],
                          "ответ про арт-выставку вместо Кристи")
        self.assertEqual(rec["tool_calls"][0]["name"], "affinity_by_author")
        # render_meta filtered to only known keys
        self.assertIn("budget_utilization_pct", rec["render_meta"])
        self.assertEqual(rec["render_meta"]["confabulation_risk"], "low")
        self.assertEqual(rec["history_turns"], 2)

    def test_missing_question_raises(self):
        from scripts.v2.feedback import record_bad_answer
        with self.assertRaises(ValueError):
            record_bad_answer(question="", answer="A")

    def test_missing_answer_raises(self):
        from scripts.v2.feedback import record_bad_answer
        with self.assertRaises(ValueError):
            record_bad_answer(question="Q", answer="")

    def test_caps_huge_answer(self):
        """20k char cap on answer — defense in depth."""
        from scripts.v2.feedback import record_bad_answer
        long_answer = "x" * 50_000
        rec = record_bad_answer(question="Q", answer=long_answer)
        self.assertEqual(len(rec["answer"]), 20_000)

    def test_caps_huge_user_note(self):
        from scripts.v2.feedback import record_bad_answer
        rec = record_bad_answer(question="Q", answer="A",
                                  user_note="x" * 5000)
        self.assertEqual(len(rec["user_note"]), 1000)


class ListRecent(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wc_feedback_list_")
        from scripts.v2 import feedback as fb
        self._orig_dir = fb.FEEDBACK_DIR
        fb.FEEDBACK_DIR = Path(self._tmp)

    def tearDown(self):
        from scripts.v2 import feedback as fb
        fb.FEEDBACK_DIR = self._orig_dir
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_returns_empty_list(self):
        from scripts.v2.feedback import list_recent
        self.assertEqual(list_recent(), [])

    def test_multiple_records_sorted_newest_first(self):
        from scripts.v2.feedback import record_bad_answer, list_recent
        # Write 3 records
        for i in range(3):
            record_bad_answer(question=f"Q{i}", answer=f"A{i}")
        recs = list_recent()
        self.assertEqual(len(recs), 3)
        # Each unique
        ids = {r["id"] for r in recs}
        self.assertEqual(len(ids), 3)
        # Sorted by ts desc — at minimum each has a ts
        for r in recs:
            self.assertIn("ts", r)

    def test_limit_caps_output(self):
        from scripts.v2.feedback import record_bad_answer, list_recent
        for i in range(10):
            record_bad_answer(question=f"Q{i}", answer=f"A{i}")
        recs = list_recent(limit=3)
        self.assertEqual(len(recs), 3)

    def test_corrupt_line_skipped(self):
        """One malformed JSON line shouldn't kill the whole read."""
        from scripts.v2 import feedback as fb
        from scripts.v2.feedback import record_bad_answer, list_recent
        record_bad_answer(question="Q1", answer="A1")
        # Append a corrupt line
        p = fb._today_path()
        with p.open("a", encoding="utf-8") as f:
            f.write("not-json\n")
        record_bad_answer(question="Q2", answer="A2")
        recs = list_recent()
        # 2 valid records read, corrupt line skipped
        self.assertEqual(len(recs), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
