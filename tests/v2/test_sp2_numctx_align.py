"""S-P2 — num_ctx alignment across every wordcracker:v2 Ollama caller.

Root cause (outputs/S-P2_wedge_analysis_2026-05-31.md): the renderer,
critic, planner and llm_intent sized the Ollama context window via
`TokenBudget(model).ctx`, but enrich_word, _maybe_translate and the live
intent path (classify_and_extract) sent NO num_ctx — so Ollama fell back
to the Modelfile default (8192). Ollama keeps a SINGLE KvSize per runner,
so every 8192↔16384 flip rebuilt the runner (~7s reload). A single word
query flipped 2-4 times → 100-420s pseudo-wedge.

Invariant under test: EVERY payload sent to wordcracker:v2 carries an
IDENTICAL num_ctx, sourced from the same `get_model_ctx`/`TokenBudget`
helper as the renderer — so there is no source for them to drift.

These are NEGATIVE tests in the R5 sense: they fail on the pre-S-P2 code
(payloads with no num_ctx key) and pass after.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Importing the v2 tool package wires scripts/ onto sys.path so the v1
# layer (bare `from rag_tools import ...` inside learning_tools) resolves.
from scripts.v2 import tools as _tools  # noqa: F401

# Sentinel ctx so the assertions prove the payload READ the shared source
# rather than coincidentally matching a default. 13371 is not any baked
# default (Modelfile 8192 / MODEL_CTX_DEFAULTS 16384 / DEFAULT_CTX 8192).
_SENTINEL_CTX = 13371


class _CapturePost:
    """A requests.post stand-in that records the last JSON payload and
    returns a minimal valid-ish response. We only care about the payload
    that WOULD have gone to Ollama; the response body is irrelevant."""

    def __init__(self, response_json):
        self.captured: dict = {}
        self._response_json = response_json

    def __call__(self, url, json=None, timeout=None, **kw):
        self.captured["payload"] = json
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: self._response_json
        return resp


class EnrichWordSendsAlignedNumCtx(unittest.TestCase):
    """enrich_word (learning_tools) must carry num_ctx == the renderer's
    TokenBudget(model).ctx, and keep_alive == -1 (S-P2 hygiene)."""

    def test_enrich_payload_num_ctx_and_keep_alive(self):
        import scripts.learning_tools as lt
        from scripts.v2.token_budget import get_model_ctx

        post = _CapturePost({"response": '{"definition": "x"}'})
        with mock.patch.dict(os.environ, {"WC_OLLAMA_NUM_CTX": str(_SENTINEL_CTX)}), \
             mock.patch.object(lt, "_load_word_dict", return_value={}), \
             mock.patch.object(lt, "_save_word_dict", lambda *a, **k: None), \
             mock.patch("requests.post", post):
            lt.enrich_word("serendipity", force_refresh=True)
            model = post.captured["payload"]["model"]
            # byte-identical to what the renderer computes for this model
            # under the SAME env (must be read inside the override context)
            reference = get_model_ctx(model)

        opts = post.captured["payload"]["options"]
        self.assertIn("num_ctx", opts, "enrich_word lost num_ctx (S-P2 regression)")
        self.assertEqual(opts["num_ctx"], _SENTINEL_CTX)
        self.assertEqual(opts["num_ctx"], reference)
        # keep_alive must hold the shared runner, not evict it
        self.assertEqual(post.captured["payload"]["keep_alive"], -1)


class MaybeTranslateSendsAlignedNumCtx(unittest.TestCase):
    """_maybe_translate (rag_tools) must carry num_ctx == renderer's."""

    def test_translate_payload_num_ctx(self):
        import scripts.rag_tools as rt
        from scripts.v2.token_budget import get_model_ctx

        post = _CapturePost({"response": "Jeeves"})
        with mock.patch.dict(os.environ, {"WC_OLLAMA_NUM_CTX": str(_SENTINEL_CTX)}), \
             mock.patch("requests.post", post):
            # Cyrillic input so the translate hop actually fires.
            rt._maybe_translate("кто такой Дживс")
            model = post.captured["payload"]["model"]
            reference = get_model_ctx(model)

        opts = post.captured["payload"]["options"]
        self.assertIn("num_ctx", opts, "_maybe_translate lost num_ctx (S-P2 regression)")
        self.assertEqual(opts["num_ctx"], _SENTINEL_CTX)
        self.assertEqual(opts["num_ctx"], reference)
        self.assertEqual(post.captured["payload"]["keep_alive"], -1)


class LlmIntentLivePathAligned(unittest.TestCase):
    """The LIVE intent path is classify_and_extract (classify_with_llm is
    dead — only referenced in a comment). It must carry both num_ctx (so
    it stops thrashing the runner) and think:false (no hidden reasoning
    prefill), matching the renderer (rag_v2:829)."""

    def test_classify_and_extract_payload(self):
        from scripts.v2.planner import llm_intent
        from scripts.v2.token_budget import get_model_ctx

        post = _CapturePost({"message": {"content": '{"intent": "other"}'}})
        with mock.patch.dict(os.environ, {"WC_OLLAMA_NUM_CTX": str(_SENTINEL_CTX)}), \
             mock.patch("requests.post", post):
            llm_intent.classify_and_extract("привет, что ты умеешь?")
            model = post.captured["payload"]["model"]
            reference = get_model_ctx(model)

        payload = post.captured["payload"]
        self.assertIn("num_ctx", payload["options"],
                      "classify_and_extract lost num_ctx (S-P2 regression)")
        self.assertEqual(payload["options"]["num_ctx"], _SENTINEL_CTX)
        self.assertEqual(payload["options"]["num_ctx"], reference)
        self.assertIs(payload.get("think"), False,
                      "classify_and_extract must send think:false (S-P2)")


class LlmIntentTimeoutDefault(unittest.TestCase):
    """WC_LLM_INTENT_TIMEOUT_S default raised 8→20 so the ~3k-token full
    intent prefill survives a post-reload prefill (S-P2)."""

    def test_default_timeout_is_20(self):
        import importlib
        from scripts.v2.planner import llm_intent
        # Reload with the env unset so the module-level default is exercised.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WC_LLM_INTENT_TIMEOUT_S", None)
            importlib.reload(llm_intent)
            self.assertEqual(llm_intent.LLM_INTENT_TIMEOUT, 20.0)


if __name__ == "__main__":
    unittest.main()
