#!/usr/bin/env python3
"""WP-1 routing eval runner (RAG_TASK_WP1, D-R30-8·9·10).

Runs the committed ground-truth routing set (`scripts/eval/routing_eval_set.json`,
≥40 cases) through the LIVE in-process v2 engine (`rag_v2.ask`) and scores every
case with the SAME self-protected matcher the smoke battery + tripwire use
(`scripts.autonomy.smoke.evaluate_probe`) — so a PASS here means exactly what a
PASS means there. Reuse, not a second matcher (R3/R4).

What it measures
================
Tool-routing accuracy against ground truth. It is a MEASUREMENT, not a gate:
the incumbent (`wordcracker:v2` + `format=json`) is NOT expected to score 100% —
the set is deliberately seeded with known-open routing gaps (the quoted-title
book→affinity mislabel, the book-vs-author scope nest) so there is headroom to
measure improvement. A baseline < 100% is the POINT; the run always exits 0 on a
completed pass. Do NOT wire this into a deploy threshold (the tripwire keeps its
own curated 9-seed baseline) — its FAILs are by-design.

Honours WC_ORCHESTRATOR_FORMAT implicitly
=========================================
It just calls `ask()`, which reads the toggle (PR-1, scripts/v2/planner/
orch_format.py). So the SAME runner scores whichever arm is active:

    python3 -m scripts.eval.run_routing_eval                     # BASELINE (json)
    WC_ORCHESTRATOR_FORMAT=schema python3 -m scripts.eval.run_routing_eval  # SCHEMA

Diff the two fail lists to see which cases schema FIXED vs BROKE.

SOW-only
========
The default path imports the heavy engine (torch / ollama / chromadb), so this
is run on SOW (the 3090), NOT a CI required-check — the #66 static validator
(`tests/v2/test_wp1_routing_eval_set.py`) stays the hermetic CI guard. The
scoring/aggregation/report logic here is engine-free and unit-tested via an
injected `ask_fn` seam (`tests/v2/test_run_routing_eval.py`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# scripts/eval/run_routing_eval.py → parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_SET = REPO_ROOT / "scripts" / "eval" / "routing_eval_set.json"
DEFAULT_REPORT_DIR = REPO_ROOT / "scripts" / "eval" / "reports"

# An ask_fn takes (question, history|None) and returns the rag_v2.ask payload
# dict ({answer, intent, tool_calls:[{name,args}], …}) — the exact shape
# evaluate_probe consumes. Injectable so the runner is tested with no engine.
AskFn = Callable[[str, Optional[list]], dict]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    id: str
    passed: bool
    elapsed_s: float
    intent: str = ""
    tools: list = field(default_factory=list)
    reasons: list = field(default_factory=list)   # failure reasons (empty on PASS)
    category: str = ""
    visa: str = ""


@dataclass
class EvalReport:
    passed: int
    total: int
    pass_rate: float
    results: list = field(default_factory=list)   # list[CaseResult]

    @property
    def failures(self) -> list:
        return [r for r in self.results if not r.passed]


# ---------------------------------------------------------------------------
# Engine seam
# ---------------------------------------------------------------------------

def _default_ask(question: str, history: Optional[list] = None) -> dict:
    """Live in-process engine call. Imported lazily so merely importing this
    module (for the hermetic unit test) never pulls torch/ollama/chromadb."""
    from scripts.v2 import rag_v2
    return rag_v2.ask(question, history=history)


def load_cases(path: Path | str = DEFAULT_EVAL_SET) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("cases") or []


# ---------------------------------------------------------------------------
# Run + score
# ---------------------------------------------------------------------------

def run_eval(cases: list[dict], *, ask_fn: Optional[AskFn] = None) -> EvalReport:
    """Fire every case through `ask_fn` (default: the live engine), score it via
    the shared matcher, and aggregate to a pass-rate. FAIL-CLOSED: an exception
    from the engine fails that case (transport reason), exactly as the tripwire
    treats an unreachable endpoint."""
    from scripts.autonomy.smoke import evaluate_probe  # self-protected matcher

    ask = ask_fn or _default_ask
    results: list[CaseResult] = []
    passed = 0

    for case in cases:
        t0 = time.perf_counter()
        try:
            payload = ask(case["question"], case.get("history"))
            err: Optional[str] = None
        except Exception as e:  # noqa: BLE001 — any engine failure is fail-closed
            payload, err = {}, f"{type(e).__name__}: {e}"
        elapsed = time.perf_counter() - t0

        ok, reasons = evaluate_probe(case, payload, elapsed, err)
        if ok:
            passed += 1
        results.append(CaseResult(
            id=case.get("id", "?"),
            passed=ok,
            elapsed_s=round(elapsed, 2),
            intent=str((payload or {}).get("intent") or ""),
            tools=[tc.get("name") for tc in ((payload or {}).get("tool_calls") or [])],
            reasons=reasons,
            category=str(case.get("_category") or ""),
            visa=str(case.get("_visa") or ""),
        ))

    total = len(cases)
    pass_rate = (passed / total) if total else 0.0
    return EvalReport(passed=passed, total=total, pass_rate=pass_rate, results=results)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _version() -> str:
    try:
        from scripts.v2.__version__ import ANALYTICS_VERSION
        return ANALYTICS_VERSION
    except Exception:
        return "unknown"


def _format_mode() -> str:
    """The orchestrator arm this run scored — labels BASELINE vs SCHEMA reports."""
    val = (os.environ.get("WC_ORCHESTRATOR_FORMAT") or "json").strip().lower()
    return "schema" if val == "schema" else "json"


def format_report(report: EvalReport) -> str:
    """Human-readable: header (pass-rate + arm + version), a per-case PASS/FAIL
    table, then the explicit FAILS list with reasons (the headroom)."""
    mode = _format_mode()
    lines: list[str] = []
    lines.append(
        f"ROUTING EVAL  {report.passed}/{report.total} "
        f"({report.pass_rate:.0%})  |  format={mode}  |  v{_version()}"
    )
    lines.append("-" * 72)
    for r in report.results:
        tools = ",".join(t for t in r.tools if t) or "-"
        lines.append(
            f"{'PASS' if r.passed else 'FAIL'}  {r.id:<34}  "
            f"intent={r.intent or '-':<22}  {r.elapsed_s:>5.1f}s  tools={tools}"
        )

    fails = report.failures
    if fails:
        lines.append("")
        lines.append(f"FAILS ({len(fails)}) — reasons (headroom; visaed fails are by-design):")
        for r in fails:
            tag = f"  [visa:{r.visa}]" if r.visa else ""
            lines.append(f"  ✗ {r.id}{tag}")
            for reason in r.reasons:
                lines.append(f"      - {reason}")
    else:
        lines.append("")
        lines.append("no fails — 100% (no headroom left in this set)")
    return "\n".join(lines)


def write_report(report: EvalReport, *, report_dir: Path | str = DEFAULT_REPORT_DIR,
                 now: Optional[datetime] = None) -> Path:
    """Dump a timestamped JSON report. Returns the written path."""
    ts = (now or datetime.now(timezone.utc))
    mode = _format_mode()
    out = {
        "version": _version(),
        "recorded_at": ts.isoformat(timespec="seconds"),
        "orchestrator_format": mode,
        "passed": report.passed,
        "total": report.total,
        "pass_rate": round(report.pass_rate, 4),
        "results": [
            {
                "id": r.id,
                "passed": r.passed,
                "intent": r.intent,
                "tools": r.tools,
                "elapsed_s": r.elapsed_s,
                "category": r.category,
                "visa": r.visa,
                "reasons": r.reasons,
            }
            for r in report.results
        ],
    }
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"routing_eval_{mode}_{stamp}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — thin shell
    ap = argparse.ArgumentParser(
        description="WP-1 routing eval — score the ground-truth set on the live "
                    "engine (honours WC_ORCHESTRATOR_FORMAT). SOW-only.")
    ap.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET),
                    help="path to the routing eval set JSON")
    ap.add_argument("--report", action="store_true",
                    help=f"also write a timestamped JSON report under {DEFAULT_REPORT_DIR}")
    ap.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    args = ap.parse_args(argv)

    cases = load_cases(args.eval_set)
    if not cases:
        print(f"FAIL: no cases in {args.eval_set}", file=sys.stderr)
        return 1

    report = run_eval(cases)
    print(format_report(report))
    if args.report:
        path = write_report(report, report_dir=args.report_dir)
        print(f"\nreport → {path}")
    # Always 0 on a completed run: this is a measurement, not a gate (the
    # seeded headroom FAILs are by-design; the tripwire is the real gate).
    return 0


if __name__ == "__main__":
    sys.exit(main())
