"""S-P2b — orphaned-generation cancel.

When the SSE client disconnects, chat_server._stream_chat sets a
cancel_event. _llm_render streams the render (stream=True) and, on the
next chunk, must close the Ollama socket and raise RenderCancelled so the
single runner slot frees (~2.7s, repro R-25 2026-06-02) instead of
finishing a 60-120s orphaned generation.

Pre-S-P2b code did requests.post(stream=False) — uncancellable; these are
NEGATIVE tests that only pass once the streaming + cancel path exists.
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401  (wires scripts/ path)
from scripts.v2 import rag_v2
from scripts.v2._types import ToolResult, Coverage
from scripts.v2.planner.plan import QueryPlan
from scripts.v2.planner.entities import Entities


def _plan():
    return QueryPlan(intent="author_vocab", entities=Entities(), steps=[])


def _result():
    return ToolResult.success(
        tool="affinity_by_author",
        data={"top_words": [{"word": "x"}]},
        coverage=Coverage(books_matched=1, books_total=1),
    )


class _StreamResp:
    """Fake streaming requests.Response. Records whether close() ran."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for c in self._chunks:
            yield json.dumps({"message": {"content": c}}).encode()
        yield json.dumps(
            {"done": True, "prompt_eval_count": 10, "eval_count": 5}
        ).encode()

    def close(self):
        self.closed = True


class OrphanCancel(unittest.TestCase):
    def test_cancel_set_closes_socket_and_raises(self):
        """cancel already set -> first chunk triggers close()+RenderCancelled."""
        ev = threading.Event()
        ev.set()
        resp = _StreamResp(["a", "b", "c"])
        with mock.patch("scripts.v2.rag_v2.requests.post", return_value=resp):
            with self.assertRaises(rag_v2.RenderCancelled):
                rag_v2._llm_render(
                    "q", _plan(), [_result()],
                    model="qwen3:14b", ollama_host="http://x",
                    cancel_event=ev,
                )
        self.assertTrue(resp.closed, "socket not closed -> runner slot would hang")

    def test_cancel_midstream_stops_iteration(self):
        """Client drops after the first chunk -> render aborts, socket closed."""
        ev = threading.Event()

        class _Resp(_StreamResp):
            def iter_lines(self):
                for c in ["a", "b", "c", "d"]:
                    yield json.dumps({"message": {"content": c}}).encode()
                    ev.set()  # disconnect mid-stream

        resp = _Resp([])
        with mock.patch("scripts.v2.rag_v2.requests.post", return_value=resp):
            with self.assertRaises(rag_v2.RenderCancelled):
                rag_v2._llm_render(
                    "q", _plan(), [_result()],
                    model="qwen3:14b", ollama_host="http://x",
                    cancel_event=ev,
                )
        self.assertTrue(resp.closed)

    def test_no_cancel_streams_full_answer(self):
        """No cancel -> chunks accumulate, meta read from the done chunk."""
        ev = threading.Event()
        resp = _StreamResp(["Hello", " ", "world"])
        with mock.patch("scripts.v2.rag_v2.requests.post", return_value=resp):
            text, meta = rag_v2._llm_render(
                "q", _plan(), [_result()],
                model="qwen3:14b", ollama_host="http://x",
                cancel_event=ev,
            )
        self.assertEqual(text, "Hello world")
        self.assertEqual(meta["prompt_tokens"], 10)
        self.assertEqual(meta["eval_tokens"], 5)
        self.assertTrue(resp.closed)


if __name__ == "__main__":
    unittest.main()
