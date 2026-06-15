#!/usr/bin/env python3
"""Smoke-as-code battery — S1-style probes as live asserts (AUTONOMY_RUNBOOK_R-30
§6). WP-0 deliverable #3.

A fast, focused battery of INVARIANT probes fired at the live chat endpoint
(`POST /api/chat`) after a deploy. Unlike the 12-probe error-taxonomy suite
(`scripts/predeploy_probe_suite.py`, which gates on regression-vs-baseline),
the smoke battery asserts ABSOLUTE invariants that must always hold — a broken
smoke invariant trips the tripwire (§6) and rolls the deploy back (the runner's
`run_smoke` seam, §4). The committed probe definitions ARE the baseline.

Scope for #3 = S1 (R-29 book-frequency, #53): a named BOOK routes to the
book-scoped raw-frequency tool `top_ngrams_by_book`, never to the author
aggregate `top_ngrams_by_author` and never to corpus-relative `affinity_by_book`;
an author-only query keeps the `top_ngrams_by_author` path. The two probes below
prove that book-vs-author scope distinction is live.

Self-contained + stdlib-only, like `deploy_runner.py`: the matcher and the HTTP
fire live INSIDE this self-protected package (`scripts/autonomy/**` is a
scope-fence DENY path), so the smoke gate's pass/fail semantics cannot be
silently weakened from agent-editable app code. Only Stan, by hand, changes the
fence-protected battery. Every external effect (the HTTP `fire`) is injectable
so the battery is unit-tested with no live endpoint.

The runner runs `verify_deployed_image.sh` (which gates /health.git_sha ==
deployed SHA) BEFORE smoke, so by the time the battery fires the runtime
identity is already proven; the battery only asserts behaviour. It is
FAIL-CLOSED: a transport error, an empty battery, or any failed invariant →
the battery is not green → the runner rolls back.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT_S = 90
DEFAULT_ENGINE = "v2"

# Universal invariants applied to every probe (cf. predeploy's
# `universal_pass_when`): a live, answered response is the floor.
UNIVERSAL: list[dict] = [
    {"kind": "answer_not_empty"},
]

# --- S1 smoke probes (R-29 book-frequency, #53) ----------------------------
# The routing assertions (tool_called / tool_not_called) are the precise S1
# invariant and are deterministic (the planner route is fixed once the intent
# resolves), so they are robust against LLM answer-text variation.
SMOKE_PROBES: list[dict] = [
    {
        "id": "S1-book-scope",
        "title": "named book routes to book-scoped raw frequency (top_ngrams_by_book)",
        "question": "самые частотные слова в «Dracula»",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"],
             "reason": "a named book must resolve, not bounce to clarify"},
            {"kind": "tool_called", "name": "top_ngrams_by_book",
             "reason": "S1: a named book routes to the book-scoped raw-frequency tool"},
            {"kind": "tool_not_called", "name": "top_ngrams_by_author",
             "reason": "S1 bug A: book scope must NOT collapse into the author aggregate"},
            {"kind": "tool_not_called", "name": "affinity_by_book",
             "reason": "S1: a raw-frequency request must NOT route to corpus-relative affinity"},
        ],
    },
    {
        "id": "S1-author-scope",
        "title": "author-only top words keeps the author aggregate (top_ngrams_by_author)",
        "question": "самые частотные слова у Дойла",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "tool_called", "name": "top_ngrams_by_author",
             "reason": "S1 control: an author-only query keeps the author-aggregate path"},
            {"kind": "tool_not_called", "name": "top_ngrams_by_book",
             "reason": "S1 control: no book named → must NOT use the book-scoped tool"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Matcher — the subset of predeploy's rule kinds the smoke probes use, kept
# byte-for-byte compatible in semantics so probe authors share one model.
# ---------------------------------------------------------------------------

def _value_at(payload: dict, field_name: str) -> Any:
    if field_name == "answer":
        return payload.get("answer") or ""
    if field_name == "intent":
        return (payload.get("intent") or "").lower()
    return payload.get(field_name)


def _tool_names(payload: dict) -> list[str]:
    return [tc.get("name") for tc in (payload.get("tool_calls") or [])]


def _match(rule: dict, payload: dict, elapsed: float) -> Optional[str]:
    """Return None if the rule holds, else a human-readable failure reason."""
    kind = rule.get("kind")
    suffix = f" ({rule['reason']})" if rule.get("reason") else ""

    if kind == "answer_not_empty":
        ans = (payload.get("answer") or "").strip()
        return None if ans else "answer is empty" + suffix

    if kind == "contains":
        fld = rule.get("field", "answer")
        needle = rule["value"]
        return None if needle in str(_value_at(payload, fld) or "") \
            else f"{fld} does not contain {needle!r}{suffix}"

    if kind == "not_contains":
        fld = rule.get("field", "answer")
        needle = rule["value"]
        return None if needle not in str(_value_at(payload, fld) or "") \
            else f"{fld} contains forbidden {needle!r}{suffix}"

    if kind == "regex_match":
        fld = rule.get("field", "answer")
        pat = rule["pattern"]
        return None if re.search(pat, str(_value_at(payload, fld) or "")) \
            else f"{fld} did not match /{pat}/{suffix}"

    if kind == "regex_no_match":
        fld = rule.get("field", "answer")
        pat = rule["pattern"]
        m = re.search(pat, str(_value_at(payload, fld) or ""))
        return f"{fld} matched forbidden /{pat}/: {m.group(0)!r}{suffix}" if m else None

    if kind == "intent_in":
        got = (payload.get("intent") or "").lower()
        want = {v.lower() for v in rule["values"]}
        return None if got in want else f"intent={got!r} not in {sorted(want)}{suffix}"

    if kind == "intent_not_in":
        got = (payload.get("intent") or "").lower()
        forbid = {v.lower() for v in rule["values"]}
        return None if got not in forbid else f"intent={got!r} is forbidden {sorted(forbid)}{suffix}"

    if kind == "tool_called":
        names = _tool_names(payload)
        return None if rule["name"] in names else f"tool {rule['name']!r} not called (got {names!r}){suffix}"

    if kind == "tool_not_called":
        names = _tool_names(payload)
        return None if rule["name"] not in names else f"tool {rule['name']!r} was called (forbidden){suffix}"

    if kind == "latency_under_s":
        return None if elapsed < float(rule["value"]) \
            else f"elapsed {elapsed:.1f}s >= cap {rule['value']}s{suffix}"

    return f"unknown rule kind: {kind!r}"


def evaluate_probe(probe: dict, payload: dict, elapsed: float,
                   transport_error: Optional[str]) -> tuple[bool, list[str]]:
    """Return (passed, reasons). FAIL-CLOSED: a transport error fails the probe
    outright (a deploy whose smoke probe can't even reach the endpoint is not
    healthy)."""
    if transport_error:
        return False, [f"transport: {transport_error}"]
    reasons: list[str] = []
    for rule in UNIVERSAL + (probe.get("assert") or []):
        reason = _match(rule, payload, elapsed)
        if reason:
            reasons.append(reason)
    return (not reasons), reasons


# ---------------------------------------------------------------------------
# HTTP fire — injectable seam (tests pass a fake)
# ---------------------------------------------------------------------------

def fire(base_url: str, question: str, engine: str, timeout: int
         ) -> tuple[dict, float, Optional[str]]:
    """POST one question to /api/chat. Returns (payload, elapsed_s, error|None).
    Mirrors predeploy_probe_suite.fire_probe so a probe behaves identically
    under both harnesses."""
    body = json.dumps({"question": question, "engine": engine}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        elapsed = time.perf_counter() - t0
        try:
            return json.loads(raw), elapsed, None
        except json.JSONDecodeError as e:
            return {}, elapsed, f"non-JSON response: {e}"
    except urllib.error.HTTPError as e:
        return {}, time.perf_counter() - t0, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return {}, time.perf_counter() - t0, f"URLError: {e.reason}"
    except Exception as e:  # noqa: BLE001 — any transport failure is fail-closed
        return {}, time.perf_counter() - t0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

@dataclass
class SmokeReport:
    ok: bool
    passed: int
    total: int
    failures: list[tuple[str, list[str]]] = field(default_factory=list)
    detail: str = ""


def run_smoke_battery(
    base_url: str,
    *,
    expected_sha: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    engine: str = DEFAULT_ENGINE,
    probes: Optional[list[dict]] = None,
    fire_fn: Optional[Callable[..., tuple[dict, float, Optional[str]]]] = None,
) -> SmokeReport:
    """Fire every smoke probe at `base_url` and aggregate. FAIL-CLOSED:
    `ok` is True only when there is at least one probe AND every probe passes
    every invariant. `expected_sha` is informational (the runner's verify step
    already gated runtime identity); it is threaded into the report detail."""
    probe_list = probes if probes is not None else SMOKE_PROBES
    do_fire = fire_fn if fire_fn is not None else fire

    if not probe_list:
        return SmokeReport(ok=False, passed=0, total=0,
                           detail="no smoke probes configured — fail-closed")

    failures: list[tuple[str, list[str]]] = []
    passed = 0
    for probe in probe_list:
        payload, elapsed, err = do_fire(base_url, probe["question"], engine, timeout)
        ok, reasons = evaluate_probe(probe, payload, elapsed, err)
        if ok:
            passed += 1
        else:
            failures.append((probe.get("id", "?"), reasons))

    total = len(probe_list)
    all_ok = passed == total
    sha_tag = f" @ {expected_sha}" if expected_sha else ""
    if all_ok:
        detail = f"smoke {passed}/{total} probes pass{sha_tag}"
    else:
        broken = "; ".join(f"{pid}: {', '.join(rs)}" for pid, rs in failures)
        detail = f"smoke {passed}/{total} pass{sha_tag} — FAIL: {broken}"
    return SmokeReport(ok=all_ok, passed=passed, total=total,
                       failures=failures, detail=detail)


if __name__ == "__main__":  # pragma: no cover — ad-hoc manual run
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Smoke-as-code battery (runbook §6)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8890")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    args = ap.parse_args()
    report = run_smoke_battery(args.base_url, timeout=args.timeout)
    print(report.detail)
    sys.exit(0 if report.ok else 1)
