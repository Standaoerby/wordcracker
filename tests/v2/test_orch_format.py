"""WC_ORCHESTRATOR_FORMAT toggle — unit + call-site snapshot tests (WP-1 PR-1).

Proves the no-op default (env unset / "json" ⇒ payload `format` stays the
literal "json" at BOTH live call sites), that schema-mode swaps in the right
schema, that the toggle touches NOTHING ELSE in the payload (S-P2: num_ctx /
temperature / keep_alive are byte-for-byte equal across modes), and that the
two schemas can't silently drift from the contracts they mirror.

Stdlib-only (mock + monkeypatch); the heavy registry walk in the planner's
`_system_prompt` is stubbed so the test stays hermetic and never needs Ollama.
"""
from __future__ import annotations

import dataclasses
import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import orch_format as of
from scripts.v2.planner import plan_spec
from scripts.v2.planner.intent import INTENTS


# ---------------------------------------------------------------------------
# orch_format() / is_schema_mode() — the switch itself
# ---------------------------------------------------------------------------

def test_default_unset_is_json_noop(monkeypatch):
    monkeypatch.delenv(of.ENV_VAR, raising=False)
    assert of.is_schema_mode() is False
    assert of.orch_format(of.PLANNER_SCHEMA) == "json"
    assert of.orch_format(of.INTENT_SCHEMA) == "json"


def test_explicit_json_is_noop(monkeypatch):
    monkeypatch.setenv(of.ENV_VAR, "json")
    assert of.orch_format(of.PLANNER_SCHEMA) == "json"


def test_garbage_value_is_noop(monkeypatch):
    # Anything that isn't "schema" must fall back to the incumbent literal.
    for val in ("", "JSO N", "yaml", "schemaa", "0", "off"):
        monkeypatch.setenv(of.ENV_VAR, val)
        assert of.orch_format(of.PLANNER_SCHEMA) == "json", val


def test_schema_mode_returns_schema_object(monkeypatch):
    monkeypatch.setenv(of.ENV_VAR, "schema")
    assert of.is_schema_mode() is True
    assert of.orch_format(of.PLANNER_SCHEMA) is of.PLANNER_SCHEMA
    assert of.orch_format(of.INTENT_SCHEMA) is of.INTENT_SCHEMA


def test_schema_mode_is_case_and_space_insensitive(monkeypatch):
    for val in ("schema", "SCHEMA", " Schema ", "schema\n"):
        monkeypatch.setenv(of.ENV_VAR, val)
        assert of.is_schema_mode() is True, repr(val)


# ---------------------------------------------------------------------------
# Anti-drift — schemas must track the contracts they mirror
# ---------------------------------------------------------------------------

def test_intent_enum_is_the_live_taxonomy():
    """The label enum is derived from INTENTS, never hand-copied — so a
    schema-constrained label is always a real one the router can dispatch."""
    enum = of.INTENT_SCHEMA["properties"]["intent"]["enum"]
    assert set(enum) == set(INTENTS)
    # The eval-set asserts these two intents directly; schema-mode must be able
    # to emit them or it would break the out-of-scope / clarify routing cases.
    assert "out_of_scope" in enum
    assert "clarify" in enum
    assert of.INTENT_SCHEMA["required"] == ["intent"]


def test_intent_schema_slots_match_parser_fields():
    """Entity slots must be exactly the keys classify_and_extract reads."""
    props = set(of.INTENT_SCHEMA["properties"])
    expected = {"intent", "author", "book_title", "word",
                "year_from", "year_to", "country"}
    assert props == expected


def test_planner_schema_keys_are_real_planspec_fields():
    """No schema key may be foreign to the dataclasses from_json builds — a
    typo'd or stale property would constrain generation to the wrong shape."""
    top = set(of.PLANNER_SCHEMA["properties"])
    plan_fields = {f.name for f in dataclasses.fields(plan_spec.PlanSpec)}
    assert top <= plan_fields, top - plan_fields

    step_props = set(
        of.PLANNER_SCHEMA["properties"]["steps"]["items"]["properties"])
    step_fields = {f.name for f in dataclasses.fields(plan_spec.PlanStepSpec)}
    assert step_props <= step_fields, step_props - step_fields


def test_planner_schema_example_roundtrips_through_from_json():
    """A document shaped per PLANNER_SCHEMA must parse cleanly into a PlanSpec —
    proves the schema describes a from_json-parseable plan, not a fiction."""
    example = {
        "intent_hint": "book_readability",
        "rationale": "named book → readability",
        "steps": [
            {"id": "s1", "tool": "resolve_book_title",
             "args": {"query": "Pride and Prejudice"}},
            {"id": "s2", "tool": "book_readability",
             "args": {"pg_id": "$s1.pg_id"}, "needs": ["s1"]},
        ],
        "expected_cost": "cheap",
    }
    plan = plan_spec.from_json(example)
    assert [s.tool for s in plan.steps] == ["resolve_book_title",
                                            "book_readability"]
    assert plan.steps[1].needs == ["s1"]

    # clarify-only is a valid terminal output the schema must also admit
    # (no top-level `required`, so a steps-less object is legal).
    clarify = plan_spec.from_json({"clarify": "which book?"})
    assert clarify.clarify == "which book?"
    assert clarify.steps == []


# ---------------------------------------------------------------------------
# Call-site snapshots — the wiring at the two live Ollama calls
# ---------------------------------------------------------------------------

def _capture_planner_payload() -> dict:
    """Build the planner payload via the REAL _call_ollama with the heavy
    system-prompt builder + HTTP stubbed; return the captured request body."""
    from scripts.v2.planner import llm_planner
    captured: dict = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "{}"}}

    def _fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        return _Resp()

    with mock.patch.object(llm_planner, "_system_prompt", return_value="SYS"), \
            mock.patch("scripts.v2.planner.llm_planner.requests.post",
                       side_effect=_fake_post):
        llm_planner._call_ollama("some user message")
    return captured["payload"]


def _capture_intent_payload() -> dict:
    """Same, for the intent classify_and_extract call."""
    from scripts.v2.planner import llm_intent
    llm_intent._reset_cache_for_tests()
    captured: dict = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": '{"intent": "corpus_meta"}'}}

    def _fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        return _Resp()

    with mock.patch("scripts.v2.planner.llm_intent.requests.post",
                    side_effect=_fake_post):
        # unique text per call so the LRU never short-circuits the HTTP build
        llm_intent.classify_and_extract("snapshot probe сколько книг тут")
    return captured["payload"]


def test_planner_default_payload_format_is_json(monkeypatch):
    monkeypatch.delenv(of.ENV_VAR, raising=False)
    payload = _capture_planner_payload()
    assert payload["format"] == "json"


def test_planner_schema_payload_format_is_schema(monkeypatch):
    monkeypatch.setenv(of.ENV_VAR, "schema")
    payload = _capture_planner_payload()
    assert payload["format"] == of.PLANNER_SCHEMA


def test_intent_default_payload_format_is_json(monkeypatch):
    monkeypatch.delenv(of.ENV_VAR, raising=False)
    payload = _capture_intent_payload()
    assert payload["format"] == "json"


def test_intent_schema_payload_format_is_schema(monkeypatch):
    monkeypatch.setenv(of.ENV_VAR, "schema")
    payload = _capture_intent_payload()
    assert payload["format"] == of.INTENT_SCHEMA


def test_toggle_changes_only_format_planner(monkeypatch):
    """S-P2: the toggle must touch ONLY `format` — num_ctx / temperature /
    keep_alive (the shared-runner-sensitive knobs) stay byte-for-byte equal."""
    monkeypatch.delenv(of.ENV_VAR, raising=False)
    base = _capture_planner_payload()
    monkeypatch.setenv(of.ENV_VAR, "schema")
    schema = _capture_planner_payload()
    for k in ("model", "options", "keep_alive", "stream", "messages"):
        assert base[k] == schema[k], f"toggle changed {k!r}"
    assert base["format"] != schema["format"]


def test_toggle_changes_only_format_intent(monkeypatch):
    monkeypatch.delenv(of.ENV_VAR, raising=False)
    base = _capture_intent_payload()
    monkeypatch.setenv(of.ENV_VAR, "schema")
    schema = _capture_intent_payload()
    for k in ("model", "options", "keep_alive", "stream", "think"):
        assert base[k] == schema[k], f"toggle changed {k!r}"
    assert base["format"] != schema["format"]
