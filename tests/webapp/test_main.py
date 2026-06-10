"""api/main.py — health/ready (Q3), SSE shape, CORS, xlsx route.
Everything via TestClient, no Ollama / no Chroma / no GPU."""
from __future__ import annotations

import io
import json

import pytest

# Self-skip while fastapi/openpyxl are not yet in requirements.lock (CI
# installs the lock; the S6 lock regen on the prod host activates these).
openpyxl = pytest.importorskip(
    "openpyxl", reason="openpyxl not in requirements.lock yet (S6 regen)")
pytest.importorskip(
    "fastapi", reason="fastapi not in requirements.lock yet (S6 regen)")

from fastapi.testclient import TestClient

from api import main as api_main
from scripts import api_loop


@pytest.fixture()
def client():
    return TestClient(api_main.app)


class TestHealthReady:
    def test_health_is_pure_liveness(self, client, monkeypatch):
        # Even with Ollama unreachable, health must be 200 (Q3 — docker
        # healthcheck must not restart-loop on an Ollama restart).
        def dead(*a, **k):
            raise ConnectionError("no ollama")
        monkeypatch.setattr(api_main._requests, "get", dead)
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and "version" in body

    def test_ready_503_without_ollama(self, client, monkeypatch):
        def dead(*a, **k):
            raise ConnectionError("no ollama")
        monkeypatch.setattr(api_main._requests, "get", dead)
        r = client.get("/api/ready")
        assert r.status_code == 503
        assert r.json()["ready"] is False

    def test_ready_200_when_warm(self, client, monkeypatch):
        class Ok:
            ok = True
        monkeypatch.setattr(api_main._requests, "get", lambda *a, **k: Ok())
        monkeypatch.setitem(api_main._warm, "embedder", True)
        r = client.get("/api/ready")
        assert r.status_code == 200 and r.json()["ready"] is True


def _fake_stream(question, data_only=False, **kw):
    yield {"event": "token", "data": {"delta": "привет"}}
    yield {"event": "done", "data": {
        "query_id": "q1", "answer_md": "привет", "tables": [],
        "entities": [], "tool_trace": [], "data_only": data_only,
        "stop_reason": "complete"}}


class TestQuerySse:
    def test_content_type_and_frames(self, client, monkeypatch):
        monkeypatch.setattr(api_main, "stream_answer", _fake_stream)
        r = client.post("/api/query", json={"question": "тест"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = r.text
        assert "event: token" in text and "event: done" in text
        # Frame payloads are JSON after "data:"
        for line in text.splitlines():
            if line.startswith("data:"):
                json.loads(line[5:].strip())

    def test_empty_question_rejected(self, client):
        assert client.post("/api/query", json={"question": ""}).status_code == 422

    def test_history_accepted_and_ignored(self, client, monkeypatch):
        monkeypatch.setattr(api_main, "stream_answer", _fake_stream)
        r = client.post("/api/query", json={
            "question": "тест", "history": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200

    def test_cors_headers_for_dev_origin(self, client, monkeypatch):
        monkeypatch.setattr(api_main, "stream_answer", _fake_stream)
        r = client.post("/api/query", json={"question": "тест"},
                        headers={"Origin": "http://localhost:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


class TestExportRoute:
    def test_xlsx_roundtrip(self, client):
        r = client.post("/api/export/xlsx", json={
            "tables": [{"tool": "t", "columns": ["a", "b"], "rows": [[1, 2]]}],
        })
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        assert wb.sheetnames == ["t"]
        assert wb["t"].cell(row=2, column=1).value == 1

    def test_filename_sanitised(self, client):
        r = client.post("/api/export/xlsx", json={
            "tables": [], "filename": "../evil"})
        assert ".." not in r.headers["content-disposition"]
        assert r.headers["content-disposition"].endswith('.xlsx"')


class TestStreamAnswerIntegration:
    """stream_answer против настоящего api_loop (стаб только ask_stream) —
    проверяет, что main.py и api_loop сшиты правильно."""

    def test_full_chain(self, client, monkeypatch):
        def fake_ask(question, **kwargs):
            yield {"event": "intent", "label": "x", "confidence": 1}
            yield {"event": "answer", "text": "ответ"}
            yield {"event": "done", "tool_calls": [], "iterations": 0}
        monkeypatch.setattr(api_loop.rag_v2, "ask_stream", fake_ask)
        r = client.post("/api/query", json={"question": "тест"})
        assert r.status_code == 200
        assert "event: done" in r.text
        assert "stop_reason" in r.text
