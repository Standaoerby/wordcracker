"""S-B8 readiness (2026-06-02) — /health.ready is an EXPLICIT signal gated on
_warmup completion (inverts the old S-R5 warm-before-bind sequencing).

Topology (by code): chat_server.main() now binds the listening socket FIRST,
then runs _warmup() in a background daemon thread. /health answers 200 for
liveness the moment the socket is up, but reports ``ready: false`` until the
warmup thread sets _READY at completion. The deploy gate (compose healthcheck
gating on ready + predeploy wait_for_health waiting on ready) holds off the
post-deploy probe until ready=true, so the probe still runs warm — without
warmup blocking the bind, and so the gate survives S-P2c trimming the per-tool
result-warming dead-weight (the heavy-model loads ARE the readiness definition).

Guards:
  1. test_binds_before_warmup_completes — pins the inverted invariant: main()
     binds + serves WITHOUT waiting for _warmup, and /health.ready stays false
     until the warmup thread finishes, then flips true. (Replaces the old
     test_main_warms_before_serving, which pinned the now-retired
     warm-before-bind ordering.)
  2. test_no_warmup_flag_skips_warmup — --no-warmup skips _warmup but still
     serves AND reports ready immediately (never stuck at ready=false).
  3. test_warmup_does_model_only_touches — pins that _warmup loads ONLY the
     P7/P8 MODELS (spaCy + an ollama keep_alive=-1 generate at aligned
     num_ctx) and dispatches NO tool (the result-cache warming is cut, S-P2c
     #4). Heavy neighbours (chromadb, BGE, spaCy, ollama) are stubbed so the
     test is corpus- and model-free.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))


class WarmupSequencing(unittest.TestCase):
    def setUp(self):
        try:
            import scripts.chat_server as cs  # noqa: F401
        except Exception as e:  # pragma: no cover - env without runtime deps
            self.skipTest(f"chat_server import unavailable here: {e}")
        self.cs = cs

    def test_binds_before_warmup_completes(self):
        """S-B8 inverted invariant: main() must bind the socket and enter
        serve_forever WITHOUT waiting for _warmup() to finish, and /health.ready
        (backed by _READY) must stay false while warmup runs, then flip true
        when it completes. A future refactor that moves _warmup() back in front
        of the bind, or that flips ready before warmup is done, breaks this."""
        order = []
        release = threading.Event()         # test holds warmup open
        warmup_entered = threading.Event()  # warmup thread signals it started

        orig_warmup = self.cs._warmup
        orig_server = self.cs.ThreadingHTTPServer
        orig_argv = sys.argv
        self.cs._READY.clear()

        def blocking_warmup():
            warmup_entered.set()
            order.append("warmup-start")
            release.wait(timeout=5)
            order.append("warmup-done")
            self.cs._READY.set()

        class _FakeServer:
            def __init__(self, *_a, **_k):
                order.append("server")

            def serve_forever(self):
                order.append("serve")  # no-op here; would block in prod

        try:
            self.cs._warmup = blocking_warmup
            self.cs.ThreadingHTTPServer = _FakeServer
            sys.argv = ["chat_server", "--port", "0"]
            self.cs.main()  # returns at once: fake serve_forever is a no-op

            # The socket bound and serve_forever ran while warmup is still
            # blocked -> the bind does NOT wait for warmup.
            self.assertTrue(warmup_entered.wait(timeout=5),
                            "warmup must run in a background thread")
            self.assertIn("server", order)
            self.assertIn("serve", order)
            self.assertFalse(self.cs._READY.is_set(),
                             "/health.ready must be false while warmup runs")

            release.set()  # let warmup finish
            for _ in range(100):
                if self.cs._READY.is_set():
                    break
                time.sleep(0.02)
            self.assertTrue(self.cs._READY.is_set(),
                            "/health.ready must flip true once warmup completes")
            self.assertEqual(order[-1], "warmup-done")
        finally:
            release.set()
            self.cs._warmup = orig_warmup
            self.cs.ThreadingHTTPServer = orig_server
            sys.argv = orig_argv
            self.cs._READY.set()  # leave the event set so other tests see ready

    def test_no_warmup_flag_skips_warmup(self):
        """--no-warmup must skip _warmup but still bring the server up — the
        emergency escape hatch must not regress into 'never serves'."""
        order: list[str] = []
        orig_warmup = self.cs._warmup
        orig_server = self.cs.ThreadingHTTPServer
        orig_argv = sys.argv

        class _FakeServer:
            def __init__(self, *_a, **_k):
                order.append("server")

            def serve_forever(self):
                order.append("serve")

        try:
            self.cs._warmup = lambda: order.append("warmup")
            self.cs.ThreadingHTTPServer = _FakeServer
            sys.argv = ["chat_server", "--no-warmup", "--port", "0"]
            self.cs.main()
        finally:
            self.cs._warmup = orig_warmup
            self.cs.ThreadingHTTPServer = orig_server
            sys.argv = orig_argv

        self.assertNotIn("warmup", order,
                         "--no-warmup must not call _warmup()")
        self.assertIn("server", order,
                      "--no-warmup must still start the server")
        self.assertTrue(self.cs._READY.is_set(),
                        "--no-warmup must report ready immediately — the server "
                        "must never get stuck at ready=false and block the gate")


class ChatEndpointGatedUntilReady(unittest.TestCase):
    """S-B8: do_POST must 503 on the chat endpoints while _READY is clear, so
    the (ungated) nginx proxy_pass can't forward cold/racing requests into the
    runtime during the background warmup window. Liveness GETs stay up."""

    def setUp(self):
        try:
            import scripts.chat_server as cs  # noqa: F401
        except Exception as e:  # pragma: no cover
            self.skipTest(f"chat_server import unavailable here: {e}")
        self.cs = cs

    def _post(self, path):
        # Bypass BaseHTTPRequestHandler.__init__ (which would run the socket
        # request cycle) — the readiness guard only touches self.path/_send.
        h = self.cs.Handler.__new__(self.cs.Handler)
        h.path = path
        captured = {}

        def fake_send(code, body, ctype):
            captured["code"] = code
            captured["body"] = body

        h._send = fake_send
        h._read_payload_capped = lambda: (None, "should not be reached")
        h.do_POST()
        return captured

    def test_chat_503_while_not_ready(self):
        self.cs._READY.clear()
        try:
            for path in ("/api/chat", "/api/chat/stream", "/api/chat?engine=v2"):
                cap = self._post(path)
                self.assertEqual(cap.get("code"), 503,
                                 f"{path} must 503 while not ready; got {cap}")
        finally:
            self.cs._READY.set()

    def test_chat_not_503_once_ready(self):
        # When ready, the guard is skipped and routing proceeds to the payload
        # read — our stub returns an error there, so we just assert it is NOT
        # the 503 readiness bounce.
        self.cs._READY.set()
        cap = self._post("/api/chat")
        self.assertNotEqual(cap.get("code"), 503,
                            "ready runtime must not bounce chat with 503")


class WarmupDoesModelOnlyTouches(unittest.TestCase):
    def setUp(self):
        try:
            import scripts.chat_server as cs  # noqa: F401
            import scripts.v2.scoring as scoring
            import scripts.v2.tool_registry as treg
        except Exception as e:  # pragma: no cover - env without runtime deps
            self.skipTest(f"chat_server import unavailable here: {e}")
        self.cs = cs
        self.scoring = scoring
        self.treg = treg

        # Record dispatch calls instead of running the real tools.
        self.dispatched: list[tuple[str, dict]] = []
        self._orig_dispatch = treg.dispatch
        treg.dispatch = lambda name, args=None, **k: (
            self.dispatched.append((name, args or {})) or None
        )

        # Skip the chroma block (wrapped in try/except in _warmup).
        self.warm_calls = []
        self._orig_warm = None
        try:
            import rag_tools as rt
            self._rt = rt
            self._orig_chroma = rt._get_chroma_collection_with_embedder
            rt._get_chroma_collection_with_embedder = lambda: (_ for _ in ()).throw(
                RuntimeError("stubbed: no chroma in test"))
            # E6 touch-warm — record the page-cache warm call, read no real files.
            self._orig_warm = getattr(rt, "warm_period_tokens", None)
            rt.warm_period_tokens = lambda **k: (self.warm_calls.append(k) or 0)
        except Exception:
            self._rt = None

        # Stub the BGE reranker so no 440 MB model loads.
        self._orig_plugin = scoring.REGISTRY.get("bge_reranker")

        class _StubReranker:
            def compute(self, _q):
                return []

        scoring.REGISTRY["bge_reranker"] = _StubReranker()

        # Fake `spacy` module so `import spacy; spacy.load(...)` is RECORDED,
        # not actually run (no model on the test box needed).
        import sys as _sys, types as _types
        self.spacy_loads = []
        _fake_spacy = _types.ModuleType("spacy")
        _fake_spacy.load = lambda name, **k: (self.spacy_loads.append(name)
                                              or object())
        self._orig_spacy = _sys.modules.get("spacy")
        _sys.modules["spacy"] = _fake_spacy

        # Stub requests.post (the ollama model-only touch) — record, don't call.
        import requests as _rq
        self._rq = _rq
        self.posts = []
        self._orig_post = _rq.post

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": ""}

        def _fake_post(url, json=None, timeout=None, **k):
            self.posts.append((url, json))
            return _Resp()

        _rq.post = _fake_post

        # Stub get_model_ctx so the warmup's num_ctx is deterministic.
        import scripts.v2.token_budget as _tb
        self._tb = _tb
        self._orig_ctx = _tb.get_model_ctx
        _tb.get_model_ctx = lambda _m: 4096

    def tearDown(self):
        import sys as _sys
        self.treg.dispatch = self._orig_dispatch
        self.scoring.REGISTRY["bge_reranker"] = self._orig_plugin
        if self._rt is not None:
            self._rt._get_chroma_collection_with_embedder = self._orig_chroma
            if self._orig_warm is not None:
                self._rt.warm_period_tokens = self._orig_warm
        if self._orig_spacy is not None:
            _sys.modules["spacy"] = self._orig_spacy
        else:
            _sys.modules.pop("spacy", None)
        self._rq.post = self._orig_post
        self._tb.get_model_ctx = self._orig_ctx

    def test_warmup_does_model_only_touches(self):
        """S-P2c #4 + S-E11 revert (2.6.45): _warmup loads ONLY the P7/P8 models
        (spaCy + an ollama keep_alive=-1 generate at aligned num_ctx) and
        dispatches NO tool. The S-P2c-cut result-warming
        (corpus_overview/top_authors/author_metadata +
        learning_words/hybrid_search/enrich_word/word_etymology) stays cut, AND
        the 2.6.44 retrieval warms — the E6 warm_period_tokens page-cache touch,
        the broad retrieval-store page-cache read, and the E11
        find_book_by_topic dispatch — are now REMOVED too (they added ~97.5s of
        churn that bought nothing: the 115GB store dwarfs RAM, so warmed pages
        were evicted before the probe ran; P6/P11 are ADVISORY in the gate
        instead). chroma + BGE stay (stubbed)."""
        self.cs._READY.clear()
        self.cs._warmup()

        # spaCy model loaded (P8 lemmatizer/POS + P7 word POS) — model-only.
        self.assertIn("en_core_web_sm", self.spacy_loads,
                      "warmup must load the spaCy model (model-only touch)")

        # ollama model-only generate: keep_alive=-1, num_ctx from get_model_ctx.
        self.assertTrue(self.posts,
                        "warmup must do an ollama /api/generate model touch")
        url, body = self.posts[-1]
        self.assertTrue(url.endswith("/api/generate"), f"unexpected url {url}")
        self.assertEqual(body.get("keep_alive"), -1,
                         "ollama touch must hold the runner (keep_alive=-1)")
        self.assertEqual(body.get("options", {}).get("num_ctx"), 4096,
                         "ollama num_ctx must come from get_model_ctx (S-P2 "
                         "ctx-thrash guard)")

        # S-E11 revert (2.6.45): the E6 period_vocab page-cache warm
        # (warm_period_tokens) is REMOVED — warmup must NOT call it.
        self.assertEqual(self.warm_calls, [],
                         f"S-E11 2.6.45: warmup must NOT call warm_period_tokens "
                         f"(E6 page-cache warm removed — the period_vocab scan is "
                         f"not warmable and is now ADVISORY); got {self.warm_calls}")

        # S-E11 revert (2.6.45): the E11 find_book_by_topic dispatch (and the
        # broad retrieval-store page-cache read) are REMOVED — warmup is
        # model-only readiness and dispatches NO tool at all. This subsumes the
        # S-P2c invariant that the pure-cache result-warming stays cut.
        names = [n for n, _ in self.dispatched]
        self.assertEqual(names, [],
                         f"S-E11 2.6.45: warmup must dispatch NO tool (E11 "
                         f"find_book_by_topic warm removed; the heavy-model loads "
                         f"ARE the readiness definition); got {names}")

        # readiness still flips after warmup completes.
        self.assertTrue(self.cs._READY.is_set(),
                        "warmup must still set _READY at completion")


if __name__ == "__main__":
    unittest.main(verbosity=2)
