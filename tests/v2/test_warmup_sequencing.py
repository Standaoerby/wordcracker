"""S-R5 warmup-sequencing (2026-05-31) — readiness gates on _warmup, and
_warmup pre-runs the P7/P8 deploy-probe tool paths.

Diagnosis (by code): chat_server.main() calls _warmup() BEFORE constructing
ThreadingHTTPServer — the socket only binds after warmup returns, so
:8890/health is connection-refused (compose healthcheck "starting") until
warmup completes. Every deploy gate (compose healthcheck → verify
poll_until_healthy → predeploy wait_for_health) therefore already waits for
warmup; the post-deploy probe always runs warm w.r.t. warmup. The cold-probe
latency was the FIRST call into tool paths _warmup never touched:
  * P7 «что значит X» bundle → hybrid_search + enrich_word + word_etymology
  * P8 «N слов уровня B2 из <book>» → learning_words (spaCy band-pass)

Two guards:
  1. test_main_warms_before_serving — pins the sequencing invariant so a
     future refactor can't move _warmup() after the bind (which would let
     /health report ready mid-warmup — the regression this whole line of work
     is about, and which a prior finisher walked back).
  2. test_warmup_drives_probe_paths — pins that _warmup dispatches the P7/P8
     tools, so the probe paths actually get warmed. Heavy neighbours
     (chromadb, BGE) are stubbed so the test is corpus- and model-free.
"""
from __future__ import annotations

import sys
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

    def test_main_warms_before_serving(self):
        """main() must call _warmup() BEFORE it constructs the HTTP server.
        The server binds+listens in ThreadingHTTPServer.__init__, so warming
        first is what keeps /health connection-refused until warmup is done."""
        order: list[str] = []

        orig_warmup = self.cs._warmup
        orig_server = self.cs.ThreadingHTTPServer
        orig_argv = sys.argv

        class _FakeServer:
            def __init__(self, *_a, **_k):
                # records the bind/listen moment
                order.append("server")

            def serve_forever(self):
                order.append("serve")  # would block in prod; no-op here

        try:
            self.cs._warmup = lambda: order.append("warmup")
            self.cs.ThreadingHTTPServer = _FakeServer
            sys.argv = ["chat_server", "--port", "0"]
            self.cs.main()
        finally:
            self.cs._warmup = orig_warmup
            self.cs.ThreadingHTTPServer = orig_server
            sys.argv = orig_argv

        self.assertEqual(order[:2], ["warmup", "server"],
                         "main() must warm up before binding the listening "
                         f"socket; got order={order}")

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


class WarmupDrivesProbePaths(unittest.TestCase):
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
        try:
            import rag_tools as rt
            self._rt = rt
            self._orig_chroma = rt._get_chroma_collection_with_embedder
            rt._get_chroma_collection_with_embedder = lambda: (_ for _ in ()).throw(
                RuntimeError("stubbed: no chroma in test"))
        except Exception:
            self._rt = None

        # Stub the BGE reranker so no 440 MB model loads.
        self._orig_plugin = scoring.REGISTRY.get("bge_reranker")

        class _StubReranker:
            def compute(self, _q):
                return []

        scoring.REGISTRY["bge_reranker"] = _StubReranker()

    def tearDown(self):
        self.treg.dispatch = self._orig_dispatch
        self.scoring.REGISTRY["bge_reranker"] = self._orig_plugin
        if self._rt is not None:
            self._rt._get_chroma_collection_with_embedder = self._orig_chroma

    def test_warmup_drives_probe_paths(self):
        """_warmup must dispatch the P7 (word bundle) + P8 (learning) tool
        paths so the deploy probe finds spaCy / FTS5 / Wiktionary / ollama
        warm. Pre-fix it warmed only corpus_overview / top_authors / Doyle."""
        self.cs._warmup()
        names = [n for n, _ in self.dispatched]

        # P8 — learning_words on a concrete book scope.
        self.assertIn("learning_words", names,
                      "warmup must warm the learning_words (P8) path")
        lw_args = next(a for n, a in self.dispatched if n == "learning_words")
        self.assertEqual(lw_args.get("scope"), {"book": "PG1342"},
                         "learning_words warmup must use a concrete book scope")
        # Must NOT pre-cache the exact P8 probe result (probe uses top=20).
        self.assertNotEqual(lw_args.get("top"), 20,
                            "warmup must not pre-fill the P8 probe's exact "
                            "cache key (top=20) — keep the probe a live test")

        # P7 — word bundle component tools.
        for tool_name in ("hybrid_search", "enrich_word", "word_etymology"):
            self.assertIn(tool_name, names,
                          f"warmup must warm the {tool_name} (P7 bundle) path")
        hs_args = next(a for n, a in self.dispatched if n == "hybrid_search")
        self.assertEqual(hs_args.get("rerank_with"), "bge_reranker",
                         "hybrid_search warmup must exercise the BGE rerank leg")
        # Representative word, not the probe word 'ajar'.
        for tool_name in ("enrich_word", "word_etymology"):
            a = next(ar for n, ar in self.dispatched if n == tool_name)
            self.assertNotEqual((a.get("word") or "").lower(), "ajar",
                                f"{tool_name} warmup must not use the probe "
                                "word 'ajar' (would game P7)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
