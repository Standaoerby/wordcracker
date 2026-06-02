"""S-P2c (#1) — structured per-call LLM latency stderr line.

`log_llm_latency` emits ONE greppable line per ollama generate/chat call
(renderer, critic, llm_intent, llm_planner, translate, enrich_word, agent,
warmup) carrying tool/model/num_ctx/load_ms/eval_count/prompt_eval/total_ms.
This replaces the coarse `_elapsed_s`-in-fixture-body signal removed in #2.

The helper is best-effort and MUST never raise into the LLM call path.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.observability import log_llm_latency


class LlmLatencyLog(unittest.TestCase):
    def _capture(self, *args) -> str:
        buf = io.StringIO()
        orig = sys.stderr
        try:
            sys.stderr = buf
            log_llm_latency(*args)
        finally:
            sys.stderr = orig
        return buf.getvalue()

    def test_structured_fields_present(self):
        body = {
            "load_duration": 1_234_000_000,   # ns -> 1234 ms
            "eval_count": 512,
            "prompt_eval_count": 2048,
            "total_duration": 8_765_000_000,  # ns -> 8765 ms
        }
        out = self._capture("renderer", "wordcracker:v2", 8192, body)
        self.assertIn("[llm]", out)
        for kv in ("tool=renderer", "model=wordcracker:v2", "num_ctx=8192",
                   "load_ms=1234", "eval_count=512", "prompt_eval=2048",
                   "total_ms=8765"):
            self.assertIn(kv, out, f"missing {kv!r} in: {out!r}")
        self.assertEqual(out.count("\n"), 1, "must be exactly one line")

    def test_missing_fields_render_as_question_mark(self):
        out = self._capture("critic", "m", None, {})
        for kv in ("num_ctx=?", "load_ms=?", "eval_count=?",
                   "prompt_eval=?", "total_ms=?"):
            self.assertIn(kv, out, f"missing {kv!r} in: {out!r}")

    def test_never_raises_on_bad_input(self):
        # exception-safe: None body, non-dict body, weird types must not raise
        for bad in (None, "not-a-dict", 123, []):
            try:
                self._capture("x", "m", 1, bad)
            except Exception as e:  # pragma: no cover
                self.fail(f"log_llm_latency raised on body={bad!r}: {e}")


if __name__ == "__main__":
    unittest.main()
