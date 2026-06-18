"""WP-1 routing eval-set structural validator (RAG_TASK_WP1, D-R30-9).

Mock/fixture-only (R-MOCK-FROM-FIXTURE): validates the committed eval-set DATA
file (`scripts/eval/routing_eval_set.json`) WITHOUT a live model / Ollama /
Chroma. It guards against the classic eval-set rot that silently false-fails
every candidate:

  * a typo'd tool name in a tool_called/tool_not_called/tool_arg_contains assert
  * an assert `kind` the real matcher does not recognize

Both are checked against ground truth: rule kinds are fed through the ACTUAL
self-protected matcher (`scripts.autonomy.smoke` — stdlib-only, cheap + safe to
import), and tool names are scanned statically from the registry sources so the
test stays hermetic and always COLLECTS (R10).
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_SET = REPO_ROOT / "scripts" / "eval" / "routing_eval_set.json"

# Rule kinds the self-protected matcher (scripts/autonomy/smoke.py::_match)
# recognizes. Mirrored here for a readable allow-list AND cross-checked by
# actually running evaluate_probe below, so a matcher change that drops a kind
# fails this test loudly rather than silently passing a dead assert.
SUPPORTED_KINDS = {
    "answer_not_empty", "contains", "not_contains", "regex_match",
    "regex_no_match", "intent_in", "intent_not_in", "tool_called",
    "tool_not_called", "tool_arg_contains", "no_tool_arg_contains",
    "latency_under_s",
}

TOOL_ASSERT_KINDS = {"tool_called", "tool_not_called", "tool_arg_contains"}

# Kinds that pin an actual routing/intent/content expectation (vs. the
# universal answer_not_empty / a bare latency cap).
DISCRIMINATING_KINDS = SUPPORTED_KINDS - {"answer_not_empty", "latency_under_s"}


def _load() -> dict:
    return json.loads(EVAL_SET.read_text(encoding="utf-8"))


def _cases() -> list:
    return _load()["cases"]


def _valid_tool_names() -> set:
    """Statically collect registered tool names — v2 `@tool(name="…")` plus the
    v1 TOOLS_SPEC `"name": "…"` — without importing the heavy engine (torch /
    chromadb), so the test is hermetic and never breaks collection."""
    names: set = set()
    v2_tools = REPO_ROOT / "scripts" / "v2" / "tools"
    for p in v2_tools.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        names.update(re.findall(r'name=["\']([a-z_]+)["\']', text))
    for fn in ("rag_tools.py", "learning_tools.py"):
        p = REPO_ROOT / "scripts" / fn
        if p.exists():
            text = p.read_text(encoding="utf-8")
            names.update(re.findall(r'"name":\s*"([a-z_]+)"', text))
    return names


def test_eval_set_loads_and_meets_min_count():
    cases = _cases()
    assert len(cases) >= 40, (
        f"WP-1 brief requires >=40 cases, got {len(cases)}")


def test_case_ids_unique_and_questions_present():
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"duplicate case ids in {ids}"
    for c in cases:
        assert str(c.get("question", "")).strip(), f"{c['id']}: empty question"


def test_every_assert_kind_supported():
    for c in _cases():
        for rule in c.get("assert", []):
            assert rule.get("kind") in SUPPORTED_KINDS, (
                f"{c['id']}: unsupported assert kind {rule.get('kind')!r} "
                f"(matcher would return 'unknown rule kind')")


def test_every_case_pins_a_ground_truth():
    """Each case must assert SOMETHING discriminating (tool/intent/content) —
    a case with only answer_not_empty measures nothing."""
    for c in _cases():
        kinds = {r.get("kind") for r in c.get("assert", [])}
        assert kinds & DISCRIMINATING_KINDS, (
            f"{c['id']}: no ground-truth assert — measures nothing")


def test_referenced_tool_names_exist():
    """Every tool named in a routing assert must be a real registered tool —
    a typo here silently fails the candidate on that case forever."""
    valid = _valid_tool_names()
    # Sanity-check the scanner itself found the registry.
    assert "top_ngrams_by_book" in valid, "static tool-name scan came up empty"
    unknown = [
        (c["id"], rule["name"])
        for c in _cases()
        for rule in c.get("assert", [])
        if rule.get("kind") in TOOL_ASSERT_KINDS and rule.get("name") not in valid
    ]
    assert not unknown, f"eval-set references unknown tool names: {unknown}"


def test_rules_run_through_real_matcher():
    """Feed every rule to the REAL self-protected matcher with a synthetic
    payload and assert it never returns the 'unknown rule kind' sentinel. This
    is the authoritative compatibility check (the SUPPORTED_KINDS set above is
    just documentation)."""
    from scripts.autonomy.smoke import evaluate_probe

    dummy = {
        "answer": "placeholder answer mentioning heart and heart",
        "intent": "clarify",
        "tool_calls": [{"name": "top_ngrams_by_book", "args": {"id": "PG345"}}],
    }
    for c in _cases():
        _ok, reasons = evaluate_probe(c, dummy, 1.0, None)
        for r in reasons:
            assert "unknown rule kind" not in r, f"{c['id']}: {r}"


def test_visa_pending_cases_are_documented():
    """Contested ground-truth cases must carry _proposed + _alternatives so the
    visa decision is reviewable (D-R30-9: Stan confirms ambiguous labels; the
    agent does not guess intent on ambiguous routing)."""
    for c in _cases():
        if c.get("_visa") == "pending":
            assert c.get("_proposed") and c.get("_alternatives"), (
                f"{c['id']}: visa-pending case missing _proposed/_alternatives")
