"""S-R5 coldstart P11 (2026-05-31) — BGE reranker startup warmup.

Probe P11 «что почитать после "Преступления и наказания"» routes through
the planner's book_similar path → find_book_by_topic with
rerank_with='bge_reranker' (BAAI/bge-reranker-base, ~440 MB cross-
encoder). That model lazy-loads on its first compute()
(scripts/v2/scoring/__init__.py:BGEReranker._load). Cold it ran 128s
(probe rule latency_under_s=60 → FAIL) vs 7.7s warm.

Two-part fix:
  * Dockerfile bakes the model into the image so `--force-recreate`
    can't wipe it (the HF cache otherwise lives in the writable layer —
    docker-compose app-volumes mounts no HF cache).
  * chat_server._warmup() loads it into RAM at startup, BEFORE
    serve_forever, so the deploy probe's first find_book_by_topic call
    finds it warm.

This guard pins the warmup half: _warmup must drive a retrieval_rerank
compute() on the bge_reranker plugin. The heavy neighbours (chromadb,
v2 dispatch) are stubbed so the test neither needs the corpus nor loads
the real 440 MB model.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))


class _StubReranker:
    """Records compute() calls instead of loading the real model."""

    def __init__(self):
        self.calls: list = []

    def compute(self, query):
        self.calls.append(query)
        return []


class BGEStartupWarmup(unittest.TestCase):
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

        # Stub the heavy neighbours so _warmup is fast and corpus-free.
        self._orig_dispatch = treg.dispatch
        treg.dispatch = lambda *a, **k: None

        # Make the chroma warm a no-op-by-exception (it's wrapped in
        # try/except in _warmup, so this just skips that block).
        try:
            import rag_tools as rt
            self._rt = rt
            self._orig_chroma = rt._get_chroma_collection_with_embedder

            def _boom():
                raise RuntimeError("stubbed: no chroma in test")

            rt._get_chroma_collection_with_embedder = _boom
        except Exception:
            self._rt = None

        # Swap in the stub reranker.
        self._orig_plugin = scoring.REGISTRY.get("bge_reranker")
        self.stub = _StubReranker()
        scoring.REGISTRY["bge_reranker"] = self.stub

    def tearDown(self):
        self.treg.dispatch = self._orig_dispatch
        self.scoring.REGISTRY["bge_reranker"] = self._orig_plugin
        if self._rt is not None:
            self._rt._get_chroma_collection_with_embedder = self._orig_chroma

    def test_warmup_drives_bge_reranker(self):
        """_warmup must run one retrieval_rerank compute() on bge_reranker
        — that's what loads the cross-encoder into RAM before /health.
        Pre-fix _warmup never touched the reranker → stub.calls empty."""
        self.cs._warmup()
        self.assertEqual(len(self.stub.calls), 1,
                         "_warmup must warm the BGE reranker exactly once")
        q = self.stub.calls[0]
        self.assertEqual(q.kind, "retrieval_rerank",
                         "warmup must use the reranker's retrieval_rerank kind")
        self.assertTrue(q.candidates,
                        "warmup needs ≥1 candidate or predict() is skipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
