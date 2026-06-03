"""Pre-deploy 12-probe error-taxonomy suite (W-18).

Fires the 12 probes from `scripts/predeploy_probes.json` at the live chat API,
checks each response against the PASS/FAIL criteria of that probe, prints a
per-probe verdict and an `N/12 PASS` rollup, and exits non-zero if any probe
regressed from PASS in the previous baseline to FAIL now — which is the signal
the deploy script uses to block the rollout.

One command, stdlib-only, no pytest harness — designed to live in front of
`docker compose up` / systemd restart in the deploy guide.

Usage
-----

    # Live prod target, default config + baseline:
    python scripts/predeploy_probe_suite.py

    # Local dev target:
    python scripts/predeploy_probe_suite.py --base-url http://127.0.0.1:8890

    # Record the current run as the new baseline. Writes the REAL per-probe
    # warm verdicts (not a 12/12 constant) as long as the run did not regress
    # vs the prior baseline and the version label bumped. Recording a run that
    # regressed is refused, so a regression can never be baked into the
    # baseline and thereby silenced:
    python scripts/predeploy_probe_suite.py --update-baseline

    # Just probes P1, P12 (debugging a specific class):
    python scripts/predeploy_probe_suite.py --probes P1,P12

Exit codes
----------
    0  — all probes PASS, no regressions, version label bumped (deploy OK)
    1  — script-level error (missing config, network setup, etc.)
    2  — at least one regression PASS->FAIL vs baseline (deploy BLOCKED)
    3  — version label did not bump vs baseline (deploy BLOCKED;
         only when --require-version-bump is set, which is the default)
    4  — health check never came up OR /health.git_sha did not match
         the SHA passed via --expected-sha / WC_PROBE_EXPECTED_SHA.
         Both classes are "the runtime under test is not the runtime
         the caller asked for" — see D-SB4-1.
    5  — probe config has empty/__FILL_FROM_SOURCE__ slots (probes not
         configured — fill in scripts/predeploy_probes.json from the source
         taxonomy file before using this in CD)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "scripts" / "predeploy_probes.json"
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "predeploy_baseline.json"
DEFAULT_BASE_URL = "https://slovoeb.net"
PROBE_ID_RE = re.compile(r"^P([1-9]|1[0-2])$")


def _read_version() -> str:
    """Read scripts/v2/__version__.ANALYTICS_VERSION without importing the package.

    We avoid `import scripts.v2.__version__` because the probe runner runs at
    deploy-time on machines that may not have the project on PYTHONPATH yet.
    """
    vfile = REPO_ROOT / "scripts" / "v2" / "__version__.py"
    try:
        text = vfile.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r"ANALYTICS_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------
# Verdict matchers
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    ok: bool
    reason: str = ""


def _value_at(payload: dict, field_name: str) -> Any:
    """Pick a string field out of the chat-API response. Limited to top-level
    fields the runner actually inspects: answer, intent, intent_confidence."""
    if field_name == "answer":
        return payload.get("answer") or ""
    if field_name == "intent":
        return (payload.get("intent") or "").lower()
    if field_name == "intent_confidence":
        return payload.get("intent_confidence") or 0
    return payload.get(field_name)


def _match_one(rule: dict, payload: dict, elapsed: float) -> MatchResult:
    kind = rule.get("kind")
    reason_suffix = f" ({rule['reason']})" if rule.get("reason") else ""

    if kind == "answer_not_empty":
        ans = (payload.get("answer") or "").strip()
        return MatchResult(bool(ans), "answer is empty" + reason_suffix if not ans else "")

    if kind == "contains":
        field_name = rule.get("field", "answer")
        needle = rule["value"]
        hay = str(_value_at(payload, field_name) or "")
        ok = needle in hay
        return MatchResult(ok, f"{field_name!s} does not contain {needle!r}{reason_suffix}" if not ok else "")

    if kind == "not_contains":
        field_name = rule.get("field", "answer")
        needle = rule["value"]
        hay = str(_value_at(payload, field_name) or "")
        ok = needle not in hay
        return MatchResult(ok, f"{field_name!s} contains forbidden {needle!r}{reason_suffix}" if not ok else "")

    if kind == "regex_match":
        field_name = rule.get("field", "answer")
        pattern = rule["pattern"]
        hay = str(_value_at(payload, field_name) or "")
        ok = re.search(pattern, hay) is not None
        return MatchResult(ok, f"{field_name!s} did not match /{pattern}/{reason_suffix}" if not ok else "")

    if kind == "regex_no_match":
        field_name = rule.get("field", "answer")
        pattern = rule["pattern"]
        hay = str(_value_at(payload, field_name) or "")
        m = re.search(pattern, hay)
        if m:
            return MatchResult(False, f"{field_name!s} matched forbidden /{pattern}/ at {m.start()}: {m.group(0)!r}{reason_suffix}")
        return MatchResult(True)

    if kind == "intent_in":
        got = (payload.get("intent") or "").lower()
        want = {v.lower() for v in rule["values"]}
        ok = got in want
        return MatchResult(ok, f"intent={got!r} not in {sorted(want)}{reason_suffix}" if not ok else "")

    if kind == "intent_not_in":
        got = (payload.get("intent") or "").lower()
        forbid = {v.lower() for v in rule["values"]}
        ok = got not in forbid
        return MatchResult(ok, f"intent={got!r} is in forbidden {sorted(forbid)}{reason_suffix}" if not ok else "")

    if kind == "latency_under_s":
        ok = elapsed < float(rule["value"])
        return MatchResult(ok, f"elapsed {elapsed:.1f}s >= cap {rule['value']}s{reason_suffix}" if not ok else "")

    if kind == "tool_called":
        names = [tc.get("name") for tc in payload.get("tool_calls", [])]
        ok = rule["name"] in names
        return MatchResult(ok, f"tool {rule['name']!r} not called (got {names!r}){reason_suffix}" if not ok else "")

    if kind == "tool_not_called":
        names = [tc.get("name") for tc in payload.get("tool_calls", [])]
        ok = rule["name"] not in names
        return MatchResult(ok, f"tool {rule['name']!r} was called (forbidden){reason_suffix}" if not ok else "")

    return MatchResult(False, f"unknown rule kind: {kind!r}")


def evaluate_probe(probe: dict, universal: list[dict], payload: dict, elapsed: float,
                   transport_error: str | None) -> tuple[bool, list[str]]:
    """Return (passed, reasons_failed) for a single run of one probe."""
    reasons: list[str] = []

    if transport_error:
        return False, [f"transport: {transport_error}"]

    for rule in (universal or []) + (probe.get("pass_when") or []):
        res = _match_one(rule, payload, elapsed)
        if not res.ok:
            reasons.append(res.reason)

    return (not reasons), reasons


def _match_across(rule: dict, payloads: list[dict]) -> MatchResult:
    """Determinism checks across multiple runs of the same probe (P12 class)."""
    kind = rule.get("kind")
    reason_suffix = f" ({rule['reason']})" if rule.get("reason") else ""

    if kind == "same_intent_across_runs":
        intents = [(p.get("intent") or "").lower() for p in payloads]
        unique = sorted(set(intents))
        if len(unique) == 1:
            return MatchResult(True)
        return MatchResult(False, f"intent flipped across runs: {intents}{reason_suffix}")

    if kind == "same_contains_across_runs":
        field_name = rule.get("field", "answer")
        needle = rule["value"]
        flags = [needle in str(_value_at(p, field_name) or "") for p in payloads]
        if len(set(flags)) == 1:
            return MatchResult(True)
        return MatchResult(False, f"presence of {needle!r} in {field_name!s} flips across runs: {flags}{reason_suffix}")

    return MatchResult(False, f"unknown across-runs rule kind: {kind!r}")


def evaluate_across_runs(probe: dict, payloads: list[dict]) -> list[str]:
    """Apply `pass_when_across_runs` rules. Returns list of failure reasons (empty = OK)."""
    rules = probe.get("pass_when_across_runs") or []
    if not rules or len(payloads) < 2:
        return []
    reasons: list[str] = []
    for rule in rules:
        res = _match_across(rule, payloads)
        if not res.ok:
            reasons.append(res.reason)
    return reasons


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def wait_for_health(base_url: str, timeout_total_s: int = 600,
                    liveness_timeout_s: int = 75,
                    require_ready: bool = True) -> tuple[bool, dict | None]:
    """Poll /health until 200 OK *and* ready=true (or timeout). Returns (ok, body_dict).

    body_dict is the parsed JSON when /health responded with a JSON
    payload (post-S-B3 contract — see ADR-B3 / D-SB3-2). On older
    runtimes that return bare ``b"ok"``, parsing fails and body_dict is
    None — `_check_expected_sha` treats that as "cannot verify" and
    the caller decides.

    Readiness gate (S-B8): /health now answers 200 for *liveness* the moment
    the socket binds, but reports ``ready: false`` until the background
    _warmup() finishes loading the heavy models. We must wait for ready=true
    before firing latency-sensitive probes (P6/P11) — otherwise the first call
    pays the cold model-load cost inside the probe and the gate reads a false
    regression. A runtime that predates the ``ready`` key (older image, or the
    admin server) omits it → ``body.get("ready", True)`` treats it as ready, so
    this stays backward-compatible.

    Two budgets, because the async topology makes liveness and readiness very
    different timescales: the socket binds immediately, so a real runtime
    answers 200 within seconds. ``liveness_timeout_s`` (75s) bounds the wait
    for the FIRST 200 — if nothing ever answers (wrong/unbound base-url) we
    bail there rather than burning the whole readiness budget (and overrunning
    callers' subprocess timeouts, e.g. test_deploy_b4's 120s). Once a 200 is
    seen, ``timeout_total_s`` (180s) bounds the wait for ready=true — long
    enough for a cold warmup (BGE ~440MB + spaCy + ollama), matching verify's
    180s poll budget.

    On timeout we return (False, last_body): when last_body is not None and its
    ``ready`` is False, the caller can tell "came up but warmup never finished"
    apart from "never came up".
    """
    url = f"{base_url.rstrip('/')}/health"
    start = time.time()
    deadline = start + timeout_total_s
    liveness_deadline = start + liveness_timeout_s
    last_body: dict | None = None
    seen_200 = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    seen_200 = True
                    raw = r.read()
                    try:
                        body = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        # Older runtime returning bare "ok" — up but
                        # identity-opaque and readiness-opaque. Caller decides.
                        return True, None
                    last_body = body
                    if not require_ready or body.get("ready", True):
                        return True, body
                    # 200 but ready=false — warmup still running; keep waiting.
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, OSError):
            pass
        # Never saw a 200 within the liveness window → it's not coming up;
        # don't sit on the full readiness budget.
        if not seen_200 and time.time() >= liveness_deadline:
            break
        time.sleep(2)
    return False, last_body


def _check_expected_sha(body: dict | None, expected: str) -> tuple[bool, str]:
    """D-SB4-1: after /health is up, assert its reported git_sha matches
    the SHA the caller is about to probe. Without this, the 12 probes
    can be 12/12 PASS against the *previous* image (silent-failure
    deploy: tag bumped, container not actually replaced) — exactly the
    class of failure runs 2-5 of the 2026-05-22 deploy epic exhibited.

    Returns (ok, message). ok=False means "do not run probes; the
    runtime is not what the caller asked for".
    """
    if body is None:
        return False, ("/health did not return JSON; cannot verify git_sha "
                       "matches expected. Older runtime? Bump runtime to "
                       "post-S-B3 (ADR-B3 / D-SB3-2) so /health emits JSON.")
    got = body.get("git_sha")
    if not got:
        return False, (f"/health JSON has no 'git_sha' key (got keys: "
                       f"{sorted(body.keys())!r}); cannot verify against "
                       f"expected {expected!r}.")
    if got != expected:
        return False, (f"/health.git_sha mismatch: expected {expected!r}, "
                       f"got {got!r}. The runtime is not the SHA the deploy "
                       f"asked for — probes would run against the wrong image.")
    return True, f"/health.git_sha={got} matches expected"


def fire_probe(base_url: str, question: str, engine: str, timeout: int) -> tuple[dict, float, str | None]:
    payload = json.dumps({"question": question, "engine": engine}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        elapsed = time.perf_counter() - t0
        try:
            return json.loads(body), elapsed, None
        except json.JSONDecodeError as e:
            return {}, elapsed, f"non-JSON response: {e}"
    except urllib.error.HTTPError as e:
        return {}, time.perf_counter() - t0, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except urllib.error.URLError as e:
        return {}, time.perf_counter() - t0, f"URLError: {e.reason}"
    except Exception as e:
        return {}, time.perf_counter() - t0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Config + baseline I/O
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        die(1, f"probe config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(1, f"probe config is not valid JSON ({path}): {e}")
    probes = cfg.get("probes")
    if not isinstance(probes, list) or len(probes) != 12:
        die(1, f"probe config must have exactly 12 probes (got {len(probes) if isinstance(probes, list) else 'non-list'})")
    seen_ids: set[str] = set()
    for p in probes:
        pid = p.get("id", "")
        if not PROBE_ID_RE.match(pid):
            die(1, f"probe id must be P1..P12 (got {pid!r})")
        if pid in seen_ids:
            die(1, f"duplicate probe id: {pid}")
        seen_ids.add(pid)
    return cfg


def check_config_filled(cfg: dict) -> list[str]:
    """Return list of probe IDs whose `question` is still `__FILL_FROM_SOURCE__`."""
    unfilled: list[str] = []
    for p in cfg["probes"]:
        q = (p.get("question") or "").strip()
        if not q or q == "__FILL_FROM_SOURCE__":
            unfilled.append(p["id"])
    return unfilled


def load_baseline(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_baseline(path: Path, version: str, results: list[dict]) -> None:
    payload = {
        "version": version,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdicts": {r["id"]: ("PASS" if r["passed"] else "FAIL") for r in results},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------

def detect_regressions(baseline: dict | None, results: list[dict]) -> list[str]:
    """Return probe IDs that were PASS in baseline and are FAIL now."""
    if not baseline:
        return []
    old = baseline.get("verdicts", {})
    regressed: list[str] = []
    for r in results:
        pid = r["id"]
        was = old.get(pid)
        now = "PASS" if r["passed"] else "FAIL"
        if was == "PASS" and now == "FAIL":
            regressed.append(pid)
    return regressed


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def die(code: int, msg: str) -> None:
    print(f"[predeploy] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def print_per_probe(results: list[dict]) -> None:
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        head = f"  {r['id']} ({r['error_class']:<3}) {status:<4} {r['elapsed']:>5.1f}s  intent={r['intent']!r:<22}"
        print(head, flush=True)
        if not r["passed"]:
            for reason in r["reasons"]:
                print(f"      - {reason}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="W-18: 12-probe pre-deploy taxonomy suite")
    ap.add_argument("--base-url", default=os.environ.get("WC_PROBE_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--engine", default="v2", choices=("v1", "v2"))
    ap.add_argument("--timeout", type=int, default=180, help="per-probe HTTP timeout, seconds")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--probes", default=None, help="comma-separated subset, e.g. P1,P12 (default: all 12)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="record the current run's REAL per-probe verdicts as the new baseline "
                         "(allowed when the run did not regress vs the prior baseline and the "
                         "version label bumped; a 12/12 sweep is NOT required — the baseline "
                         "captures the true warm count so a later drop is a detectable regression)")
    ap.add_argument("--no-health", action="store_true", help="skip /health poll")
    ap.add_argument("--expected-sha", default=os.environ.get("WC_PROBE_EXPECTED_SHA"),
                    help="D-SB4-1: after /health is up, assert its reported git_sha equals "
                         "this value before firing any probe. Without this, 12 probes can "
                         "PASS against the *previous* image after a silent-failure deploy. "
                         "deploy.sh always passes WC_IMAGE_TAG here; ad-hoc runs may omit.")
    ap.add_argument("--require-version-bump", dest="require_version_bump", action="store_true", default=True,
                    help="block deploy if ANALYTICS_VERSION did not change vs baseline (default ON)")
    ap.add_argument("--no-require-version-bump", dest="require_version_bump", action="store_false",
                    help="allow re-running on the same version (useful for debugging the probe runner itself)")
    ap.add_argument("--report", default=None, help="optional markdown report path")
    args = ap.parse_args(argv)

    config_path = Path(args.config)
    baseline_path = Path(args.baseline)

    cfg = load_config(config_path)
    unfilled = check_config_filled(cfg)
    if unfilled:
        die(5, f"probe config has unfilled slots {unfilled} — fill from "
               f"docs/test_external_claude_2026-05-22_error_taxonomy_probe_suite.md")

    universal = cfg.get("universal_pass_when", [])
    probes_all: list[dict] = cfg["probes"]

    if args.probes:
        want = {x.strip() for x in args.probes.split(",")}
        probes = [p for p in probes_all if p["id"] in want]
        if not probes:
            die(1, f"--probes {args.probes!r} matched none of {[p['id'] for p in probes_all]}")
    else:
        probes = probes_all

    # Health check (skipped on subset runs by default? no — always run unless --no-health).
    if not args.no_health:
        print(f"[predeploy] waiting for {args.base_url}/health (200 + ready) ...", flush=True)
        ok, health_body = wait_for_health(args.base_url)
        if not ok:
            if health_body is not None and health_body.get("ready") is False:
                die(4, f"{args.base_url}/health came up but ready never became "
                       f"true within the budget — _warmup() did not finish, so a "
                       f"probe would measure cold-model latency. Inspect the chat "
                       f"container's warmup-thread logs.")
            die(4, f"health check never came up at {args.base_url}/health")
        # D-SB4-1: identity gate before behaviour gate.
        if args.expected_sha:
            ok_sha, msg = _check_expected_sha(health_body, args.expected_sha)
            if not ok_sha:
                die(4, msg)
            print(f"[predeploy] {msg}", flush=True)
        print(f"[predeploy] chat up — running {len(probes)} probe(s) against {args.base_url}", flush=True)

    results: list[dict] = []
    for p in probes:
        repeat = int(p.get("repeat", 1))
        runs: list[tuple[dict, float, str | None]] = []
        run_reasons: list[str] = []
        run_passes: list[bool] = []
        for k in range(repeat):
            tag = f"#{k + 1}/{repeat}" if repeat > 1 else ""
            print(f"[predeploy] {p['id']} ({p['error_class']}) {tag} ...", flush=True)
            payload, elapsed, transport_err = fire_probe(
                args.base_url, p["question"], args.engine, args.timeout
            )
            runs.append((payload, elapsed, transport_err))
            passed_k, reasons_k = evaluate_probe(p, universal, payload, elapsed, transport_err)
            run_passes.append(passed_k)
            run_reasons.extend(f"run {k + 1}: {r}" for r in reasons_k) if repeat > 1 else run_reasons.extend(reasons_k)

        # First run carries the primary intent/elapsed shown in the verdict line.
        first_payload, first_elapsed, _ = runs[0]
        # Determinism check across runs (no-op for repeat=1).
        across_reasons = evaluate_across_runs(p, [pl for pl, _, _ in runs])
        run_reasons.extend(across_reasons)
        passed = all(run_passes) and not across_reasons

        results.append({
            "id": p["id"],
            "error_class": p["error_class"],
            "passed": passed,
            "elapsed": first_elapsed,
            "intent": (first_payload.get("intent") or "?"),
            "reasons": run_reasons,
            "repeat": repeat,
            "_payload": first_payload,
            "_transport": runs[0][2],
        })

    # --- summary ---
    n_pass = sum(1 for r in results if r["passed"])
    n = len(results)
    print("", flush=True)
    print(f"[predeploy] === per-probe verdicts ===", flush=True)
    print_per_probe(results)
    print("", flush=True)
    print(f"[predeploy] === summary: {n_pass}/{n} PASS ===", flush=True)

    baseline = load_baseline(baseline_path)
    regressions = detect_regressions(baseline, results)
    if regressions:
        print(f"[predeploy] REGRESSIONS PASS->FAIL vs baseline ({baseline_path.name}): {regressions}",
              file=sys.stderr, flush=True)

    # Version bump check
    current_version = _read_version()
    baseline_version = (baseline or {}).get("version")
    version_bumped = (baseline_version is None) or (current_version != baseline_version)
    if not version_bumped:
        print(f"[predeploy] version label did NOT bump: baseline={baseline_version} current={current_version}",
              file=sys.stderr, flush=True)
    else:
        print(f"[predeploy] version: baseline={baseline_version or '<none>'} -> current={current_version}",
              flush=True)

    if args.report:
        write_report(Path(args.report), args.base_url, args.engine, results,
                     baseline_version, current_version, regressions)

    # --- baseline update ---
    # Record the REAL warm verdicts. We deliberately do NOT require a 12/12
    # sweep here: gating the write on a clean 12/12 (the old behaviour) meant
    # the baseline was only ever written on a mythical perfect cold run, so in
    # practice the file never got written, `detect_regressions(None, ...)`
    # always returned [], and the gate was inert (S-D1: "5 FAIL пропустились").
    # The only thing we refuse is recording a run that REGRESSED vs the prior
    # baseline (that would bake the regression in and silence it forever) or a
    # run whose version label did not bump (can't tell builds apart).
    recordable = (not regressions) and version_bumped
    if args.update_baseline:
        if recordable:
            write_baseline(baseline_path, current_version, results)
            print(f"[predeploy] baseline updated ({n_pass}/{n} PASS @ {current_version}): "
                  f"{baseline_path}", flush=True)
        elif regressions:
            print(f"[predeploy] refusing to update baseline — run regressed {regressions} "
                  f"vs prior baseline (recording would silence the regression)",
                  file=sys.stderr, flush=True)
        else:
            print(f"[predeploy] refusing to update baseline — version label did not bump "
                  f"(current={current_version}); bump ANALYTICS_VERSION first",
                  file=sys.stderr, flush=True)

    # --- exit code (priority order: transport/health > regressions > version > all-pass) ---
    if regressions:
        return 2
    if args.require_version_bump and not version_bumped:
        return 3
    if n_pass != n:
        # No regression vs baseline (e.g. first run or those probes were FAIL before too),
        # but still not all PASS. We do not block deploy in that case — the regression
        # check is what gates rollout per W-18. Surface non-zero only via stderr.
        # Convention: exit 0 so that a probe that has always been FAIL doesn't permanently
        # block deploys; W-18 explicitly says blocking is "при любом переходе PASS->FAIL".
        return 0
    return 0


def write_report(path: Path, base_url: str, engine: str, results: list[dict],
                 baseline_version: str | None, current_version: str,
                 regressions: list[str]) -> None:
    n_pass = sum(1 for r in results if r["passed"])
    md = [
        f"# Pre-deploy probe suite — {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"- Target: `{base_url}` (engine={engine})",
        f"- Version: `{baseline_version or '<none>'}` -> `{current_version}`",
        f"- Summary: **{n_pass}/{len(results)} PASS**",
        f"- Regressions vs baseline: {regressions if regressions else '—'}",
        "",
        "| Probe | Class | Verdict | Elapsed | Intent | Reasons |",
        "|---|---|---|---:|---|---|",
    ]
    for r in results:
        reasons = "; ".join(r["reasons"]) if r["reasons"] else ""
        md.append(f"| {r['id']} | {r['error_class']} | {'PASS' if r['passed'] else 'FAIL'} | "
                  f"{r['elapsed']:.1f}s | `{r['intent']}` | {reasons} |")
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"[predeploy] report: {path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
