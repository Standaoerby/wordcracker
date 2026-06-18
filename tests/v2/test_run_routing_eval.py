"""WP-1 routing eval runner — hermetic unit tests (RAG_TASK_WP1).

Exercises the scoring/aggregation/report logic WITHOUT the heavy engine by
injecting a fake `ask_fn` into `run_eval`. The live `rag_v2.ask` path is the
SOW-only default (not tested here — it needs torch/ollama/chromadb). Mirrors
the eval-tripwire's injectable-seam test discipline.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.run_routing_eval import (
    run_eval, load_cases, format_report, write_report, CaseResult, EvalReport,
)


# --- synthetic cases with known asserts ------------------------------------

CASE_PASS = {
    "id": "c-pass",
    "question": "самые частотные слова в Dracula",
    "assert": [
        {"kind": "intent_not_in", "values": ["clarify"]},
        {"kind": "tool_called", "name": "top_ngrams_by_book"},
    ],
}
CASE_FAIL = {
    "id": "c-fail",
    "question": "частотные слова в «Dracula»",
    "_visa": "confirmed-2026-06-18",
    "assert": [
        {"kind": "tool_called", "name": "top_ngrams_by_book",
         "reason": "quoted freq is still raw frequency"},
    ],
}
CASE_HISTORY = {
    "id": "c-history",
    "question": "а в Frankenstein?",
    "history": [
        {"role": "user", "content": "самые частотные слова в Dracula"},
        {"role": "assistant", "content": "… PG345 …"},
    ],
    "assert": [{"kind": "answer_not_empty"}],
}
CASE_BOOM = {
    "id": "c-boom",
    "question": "explode please",
    "assert": [{"kind": "answer_not_empty"}],
}


def _fake(seen: dict):
    """ask_fn that routes each question to a canned payload (or raises)."""
    def ask(question: str, history=None) -> dict:
        seen[question] = history
        if question == CASE_BOOM["question"]:
            raise RuntimeError("ollama down")
        if question == CASE_PASS["question"]:
            return {"answer": "слова: the, and",
                    "intent": "author_top_words",
                    "tool_calls": [{"name": "top_ngrams_by_book",
                                    "args": {"pg_id": "PG345"}}]}
        if question == CASE_FAIL["question"]:
            # mislabel: routes to affinity, NOT top_ngrams_by_book → FAILs
            return {"answer": "affinity words",
                    "intent": "book_vocab",
                    "tool_calls": [{"name": "affinity_by_book", "args": {}}]}
        return {"answer": "a non-empty answer", "intent": "x", "tool_calls": []}
    return ask


def test_aggregates_pass_rate_and_verdicts():
    seen: dict = {}
    cases = [CASE_PASS, CASE_FAIL, CASE_HISTORY, CASE_BOOM]
    report = run_eval(cases, ask_fn=_fake(seen))

    assert report.total == 4
    assert report.passed == 2                       # PASS + HISTORY
    assert report.pass_rate == 0.5
    by_id = {r.id: r for r in report.results}
    assert by_id["c-pass"].passed is True
    assert by_id["c-fail"].passed is False
    assert by_id["c-history"].passed is True
    assert by_id["c-boom"].passed is False


def test_fail_surfaces_reasons_and_metadata():
    report = run_eval([CASE_FAIL], ask_fn=_fake({}))
    r = report.results[0]
    assert not r.passed
    assert r.reasons, "a fail must carry at least one reason (the headroom)"
    assert any("top_ngrams_by_book" in reason for reason in r.reasons)
    assert r.intent == "book_vocab"
    assert r.tools == ["affinity_by_book"]
    assert r.visa == "confirmed-2026-06-18"


def test_history_is_passed_through_to_ask():
    seen: dict = {}
    run_eval([CASE_HISTORY, CASE_PASS], ask_fn=_fake(seen))
    # cross-turn case forwards its history; single-turn case forwards None
    assert seen[CASE_HISTORY["question"]] == CASE_HISTORY["history"]
    assert seen[CASE_PASS["question"]] is None


def test_engine_exception_is_fail_closed():
    report = run_eval([CASE_BOOM], ask_fn=_fake({}))
    r = report.results[0]
    assert not r.passed
    assert any("transport" in reason for reason in r.reasons)
    assert "ollama down" in " ".join(r.reasons)


def test_format_report_has_header_table_and_fails(monkeypatch):
    monkeypatch.delenv("WC_ORCHESTRATOR_FORMAT", raising=False)
    report = run_eval([CASE_PASS, CASE_FAIL], ask_fn=_fake({}))
    text = format_report(report)
    assert "ROUTING EVAL  1/2 (50%)" in text
    assert "format=json" in text
    assert "PASS  c-pass" in text
    assert "FAIL  c-fail" in text
    assert "FAILS (1)" in text
    assert "top_ngrams_by_book" in text          # the fail reason is shown


def test_format_report_labels_schema_arm(monkeypatch):
    monkeypatch.setenv("WC_ORCHESTRATOR_FORMAT", "schema")
    report = run_eval([CASE_PASS], ask_fn=_fake({}))
    assert "format=schema" in format_report(report)


def test_write_report_emits_json(tmp_path, monkeypatch):
    monkeypatch.delenv("WC_ORCHESTRATOR_FORMAT", raising=False)
    report = run_eval([CASE_PASS, CASE_FAIL], ask_fn=_fake({}))
    now = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
    path = write_report(report, report_dir=tmp_path, now=now)
    assert path.exists()
    assert path.name == "routing_eval_json_20260618T120000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["passed"] == 1 and data["total"] == 2
    assert data["pass_rate"] == 0.5
    assert data["orchestrator_format"] == "json"
    ids = {row["id"] for row in data["results"]}
    assert ids == {"c-pass", "c-fail"}


def test_runs_over_committed_eval_set_through_real_matcher():
    """End-to-end hermetic: load the REAL ≥40-case file and push every case
    through MY runner + the real matcher with a uniform empty payload. No case
    may trip the matcher's 'unknown rule kind' sentinel (would mean a runner /
    matcher contract break), and the count must meet the brief's floor."""
    cases = load_cases()
    report = run_eval(
        cases,
        ask_fn=lambda q, h=None: {"answer": "", "intent": "clarify", "tool_calls": []},
    )
    assert report.total >= 40
    assert isinstance(report, EvalReport)
    for r in report.results:
        for reason in r.reasons:
            assert "unknown rule kind" not in reason, f"{r.id}: {reason}"
