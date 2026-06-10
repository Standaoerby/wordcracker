"""stream_answer contract (plan.md §4): event mapping, data_only,
stop_reason, the «stream always ends with done» invariant."""
from __future__ import annotations

import pytest

from scripts import api_loop
from scripts.v2._types import ToolResult


def _tr(tool="top_ngrams", data=None, ok=True):
    if data is None:
        data = [{"ngram": "of the", "count": 100}]
    if ok:
        return ToolResult.success(tool, data, query={"author": "Dickens"})
    r = ToolResult.success(tool, data, query={"author": "Dickens"})
    r.ok = False
    return r


def _fake_stream(events):
    def fake(question, **kwargs):
        fake.kwargs = kwargs
        yield from events
    return fake


def _run(monkeypatch, events, data_only=False):
    fake = _fake_stream(events)
    monkeypatch.setattr(api_loop.rag_v2, "ask_stream", fake)
    out = list(api_loop.stream_answer("вопрос", data_only=data_only))
    return out, fake


HAPPY = [
    {"event": "start"},
    {"event": "intent", "label": "top_ngrams", "confidence": 0.9},
    {"event": "plan", "steps": [{"tool": "top_ngrams", "args": {}}]},
    {"event": "tool_call", "name": "top_ngrams", "args": {"author": "Dickens"}},
    {"event": "tool_result", "name": "top_ngrams", "ms": 1234, "ok": True,
     "summary": "...", "_result": _tr()},
    {"event": "render_token", "delta": "Вот "},
    {"event": "render_token", "delta": "ответ."},
    {"event": "critic", "verified": True},
    {"event": "answer", "text": "Вот ответ.\n\n_критик ок_"},
    {"event": "done", "tool_calls": [], "iterations": 1, "elapsed_sec": 2.0},
]


class TestHappyPath:
    def test_event_sequence(self, monkeypatch):
        out, _ = _run(monkeypatch, HAPPY)
        kinds = [e["event"] for e in out]
        assert kinds == ["trace", "trace", "tool_call", "tool_result",
                         "table", "token", "token", "trace", "done"]

    def test_done_is_last_and_single(self, monkeypatch):
        out, _ = _run(monkeypatch, HAPPY)
        assert [e["event"] for e in out].count("done") == 1
        assert out[-1]["event"] == "done"

    def test_envelope(self, monkeypatch):
        out, _ = _run(monkeypatch, HAPPY)
        env = out[-1]["data"]
        assert env["stop_reason"] == "complete"
        assert env["answer_md"] == "Вот ответ.\n\n_критик ок_"  # authoritative
        assert env["entities"] == []
        assert env["data_only"] is False
        assert len(env["tables"]) == 1
        assert env["tables"][0]["rows"] == [["of the", 100]]
        assert env["query_id"]

    def test_tool_trace_pairing(self, monkeypatch):
        out, _ = _run(monkeypatch, HAPPY)
        trace = out[-1]["data"]["tool_trace"]
        assert trace == [{"name": "top_ngrams", "args": {"author": "Dickens"},
                          "elapsed": 1.234, "ok": True}]

    def test_streamed_tokens_not_duplicated_by_answer(self, monkeypatch):
        out, _ = _run(monkeypatch, HAPPY)
        tokens = [e["data"]["delta"] for e in out if e["event"] == "token"]
        assert tokens == ["Вот ", "ответ."]


class TestDataOnly:
    EVENTS = [
        {"event": "intent", "label": "top_ngrams", "confidence": 0.9},
        {"event": "tool_call", "name": "top_ngrams", "args": {}},
        {"event": "tool_result", "name": "top_ngrams", "ms": 10, "ok": True,
         "summary": "...", "_result": _tr()},
        {"event": "answer", "text": ""},
        {"event": "done", "tool_calls": [], "iterations": 1},
    ]

    def test_flags_passed(self, monkeypatch):
        _, fake = _run(monkeypatch, self.EVENTS, data_only=True)
        assert fake.kwargs["skip_render"] is True
        assert fake.kwargs["stream_render"] is True

    def test_no_tokens_table_present(self, monkeypatch):
        out, _ = _run(monkeypatch, self.EVENTS, data_only=True)
        kinds = [e["event"] for e in out]
        assert "token" not in kinds and "table" in kinds
        env = out[-1]["data"]
        assert env["answer_md"] == "" and env["data_only"] is True
        assert env["stop_reason"] == "complete"


class TestClarifyShortCircuit:
    def test_answer_becomes_single_token(self, monkeypatch):
        events = [
            {"event": "clarify", "question": "какого автора?"},
            {"event": "answer", "text": "какого автора?"},
            {"event": "done", "tool_calls": [], "iterations": 0},
        ]
        out, _ = _run(monkeypatch, events)
        tokens = [e for e in out if e["event"] == "token"]
        assert len(tokens) == 1
        assert tokens[0]["data"]["delta"] == "какого автора?"


class TestStopReason:
    def test_tool_error(self, monkeypatch):
        events = [
            {"event": "tool_call", "name": "t", "args": {}},
            {"event": "tool_result", "name": "t", "ms": 5, "ok": False,
             "summary": "fail", "_result": _tr(ok=False)},
            {"event": "answer", "text": "не вышло"},
            {"event": "done", "tool_calls": [], "iterations": 1},
        ]
        out, _ = _run(monkeypatch, events)
        assert out[-1]["data"]["stop_reason"] == "tool_error"
        # failed tool → no table event
        assert "table" not in [e["event"] for e in out]

    def test_error_event_then_done(self, monkeypatch):
        events = [
            {"event": "error", "kind": "renderer", "message": "ollama упал"},
            {"event": "answer", "text": "дружелюбный фолбэк"},
            {"event": "done", "tool_calls": [], "iterations": 0},
        ]
        out, _ = _run(monkeypatch, events)
        kinds = [e["event"] for e in out]
        assert kinds[0] == "error" and kinds[-1] == "done"
        assert out[-1]["data"]["stop_reason"] == "tool_error"

    def test_budget_exceeded_maps_to_max_iterations(self, monkeypatch):
        events = [
            {"event": "budget_exceeded", "spent_s": 120},
            {"event": "answer", "text": "частичный ответ"},
            {"event": "done", "tool_calls": [], "iterations": 3},
        ]
        out, _ = _run(monkeypatch, events)
        assert out[-1]["data"]["stop_reason"] == "max_iterations"


class TestDoneInvariant:
    def test_pipeline_exception_still_ends_with_done(self, monkeypatch):
        def boom(question, **kwargs):
            yield {"event": "intent", "label": "x", "confidence": 1}
            raise RuntimeError("pipeline died")
        monkeypatch.setattr(api_loop.rag_v2, "ask_stream", boom)
        out = list(api_loop.stream_answer("вопрос"))
        kinds = [e["event"] for e in out]
        assert kinds[-2:] == ["error", "done"]
        assert out[-1]["data"]["stop_reason"] == "tool_error"

    def test_inner_return_without_done(self, monkeypatch):
        events = [{"event": "intent", "label": "x", "confidence": 1}]
        out, _ = _run(monkeypatch, events)
        assert out[-1]["event"] == "done"

    def test_unknown_event_forwarded_as_trace(self, monkeypatch):
        events = [
            {"event": "brand_new_thing", "payload": 1},
            {"event": "done", "tool_calls": [], "iterations": 0},
        ]
        out, _ = _run(monkeypatch, events)
        assert out[0] == {"event": "trace",
                          "data": {"kind": "brand_new_thing", "payload": 1}}
