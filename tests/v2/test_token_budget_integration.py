"""Sprint 22+ alpha5 — integration: TokenBudget wired into renderer
+ critic. Reproduces the Stan 2026-05-20 «200 фирменных слов Кристи»
case and verifies the budget actively shrinks before Ollama call.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class ChristieSizePayloadShrinks(unittest.TestCase):
    """200 affinity items × full word objects = ~10k tokens. With
    16k ctx + 1.5k headroom, budget=14.5k → fits without shrink.
    With 8k ctx (default), needs shrink. Test both."""

    def _build_christie_payload(self) -> dict:
        """Mimic the actual affinity_by_author result shape."""
        return {
            "intent": "author_vocab",
            "tool_results": [{
                "tool": "affinity_by_author",
                "data": {
                    "author_regex": "^Christie,",
                    "top_words": [
                        {"word": f"word_{i}",
                         "author_count": (i * 7) % 200,
                         "corpus_count": (i * 23) % 5000,
                         "affinity": float(i % 30) + 0.5,
                         "pos": "ADJ",
                         "cefr": "B2"}
                        for i in range(200)
                    ],
                    "top_requested": 200,
                    "top_returned": 200,
                },
                "warnings": [],
                "ok": True,
            }],
        }

    def test_with_16k_ctx_fits_without_shrink(self):
        from scripts.v2.token_budget import TokenBudget
        with mock.patch.dict(os.environ,
                              {"WC_OLLAMA_NUM_CTX": "16384"}):
            budget = TokenBudget(model="qwen3:14b")
            payload = self._build_christie_payload()
            out, report = budget.shrink_to_fit(payload)
            # 16k ctx - 1.5k headroom = ~14.5k tokens budget
            # 200 items × ~30 chars JSON ≈ 6k chars = 2k tokens. Fits.
            self.assertTrue(report.fits)
            self.assertEqual(report.actions, [])

    def test_with_8k_ctx_shrinks_via_ladder(self):
        from scripts.v2.token_budget import TokenBudget
        with mock.patch.dict(os.environ,
                              {"WC_OLLAMA_NUM_CTX": "8192"}):
            budget = TokenBudget(model="qwen3:14b")
            # Even 200 items might fit 8k. Build a bigger payload
            # to force shrink — 500 items with verbose strings.
            payload = {
                "tool_results": [{
                    "tool": "affinity_by_author",
                    "data": {
                        "top_words": [
                            {"word": f"verbose_word_token_{i}_with_padding",
                             "snippet": "x" * 500,  # padding to force shrink
                             "affinity": float(i)}
                            for i in range(500)
                        ],
                    },
                    "warnings": [],
                    "ok": True,
                }],
            }
            out, report = budget.shrink_to_fit(payload)
            # Either fits after shrink OR ladder exhausted — but
            # MUST have applied at least one action
            self.assertGreater(len(report.actions), 0,
                              msg=f"expected ladder actions, got {report.actions}")
            # And final tokens lower than initial
            self.assertLess(report.final_tokens, report.initial_tokens)


class RendererSendsNumCtx(unittest.TestCase):
    """Renderer Ollama call must include `options.num_ctx` so Ollama
    actually allocates the bigger window. Stan deployed alpha5
    expects this is set."""

    def test_render_payload_includes_num_ctx(self):
        from scripts.v2 import rag_v2
        from scripts.v2._types import ToolResult, Coverage
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities

        plan = QueryPlan(intent="author_vocab",
                         entities=Entities(author_regex="^Christie,"),
                         steps=[])
        result = ToolResult.success(
            tool="affinity_by_author",
            data={"top_words": [{"word": "x"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        captured: dict = {}
        def fake_post(url, json=None, timeout=None, **kw):
            captured["payload"] = json
            class FakeResp:
                def raise_for_status(self): pass
                def iter_lines(self):
                    import json as _j
                    return [_j.dumps(self.json()).encode()]
                def close(self): pass
                def json(self):
                    return {"message": {"content": "ok"},
                            "prompt_eval_count": 100,
                            "eval_count": 50}
            return FakeResp()
        with mock.patch("scripts.v2.rag_v2.requests.post",
                         side_effect=fake_post):
            text, meta = rag_v2._llm_render(
                "запрос", plan, [result],
                model="qwen3:14b",
                ollama_host="http://x",
            )
        self.assertIn("options", captured["payload"])
        self.assertIn("num_ctx", captured["payload"]["options"])
        self.assertGreaterEqual(captured["payload"]["options"]["num_ctx"], 8192)

    def test_render_meta_includes_budget_fields(self):
        """Observability: meta dict gets budget telemetry."""
        from scripts.v2 import rag_v2
        from scripts.v2._types import ToolResult, Coverage
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities

        plan = QueryPlan(intent="author_vocab",
                         entities=Entities(author_regex="^Christie,"),
                         steps=[])
        result = ToolResult.success(
            tool="affinity_by_author",
            data={"top_words": [{"word": "x"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        def fake_post(url, json=None, timeout=None, **kw):
            class FakeResp:
                def raise_for_status(self): pass
                def iter_lines(self):
                    import json as _j
                    return [_j.dumps(self.json()).encode()]
                def close(self): pass
                def json(self):
                    return {"message": {"content": "ok"},
                            "prompt_eval_count": 1234, "eval_count": 567}
            return FakeResp()
        with mock.patch("scripts.v2.rag_v2.requests.post",
                         side_effect=fake_post):
            text, meta = rag_v2._llm_render(
                "запрос", plan, [result],
                model="qwen3:14b",
                ollama_host="http://x",
            )
        # Existing fields
        self.assertEqual(meta["prompt_tokens"], 1234)
        self.assertEqual(meta["eval_tokens"], 567)
        # New budget fields
        for key in ("budget_input", "budget_ctx", "budget_utilization_pct",
                     "shrink_applied", "confabulation_risk", "budget_fits"):
            self.assertIn(key, meta)


if __name__ == "__main__":
    unittest.main(verbosity=2)
