#!/usr/bin/env python3
"""Degradation tripwire — routing-accuracy eval vs a committed baseline
(AUTONOMY_RUNBOOK_R-30 §6). WP-0 deliverable #4. Fills the run_eval_tripwire seam.

The tripwire fires a curated routing-accuracy eval at the live chat endpoint,
computes a pass-rate, and TRIPS (→ rollback, §4) when the pass-rate falls below
`baseline − ε`. It is the quality-regression half of "деградирует → откатим":
smoke (#3) catches hard per-probe INVARIANT breaks; this catches AGGREGATE
quality drift that no single invariant pins.

  detector logic   → scripts/autonomy/eval_tripwire.py   (THIS file, fenced /
                     self-protected — semantics can't be weakened from app code)
  baseline DATA    → scripts/eval_tripwire_baseline.json  (re-stampable like
                     scripts/predeploy_baseline.json; Stan records real prod
                     verdicts via --update-baseline, runbook §S5 "re-stamp за
                     Стэном")

Honest scope (R2): the eval set is SEEDED, not a finished 40-q set. The night
probe (SMOKE_2026-06-14) recorded the 40-q routing eval as BLOCKED/not-built;
this lands the harness + a grounded seed (S1 book/author scope + the validated
predeploy P1/P5/P7/P8/P10/P11 routing criteria) and GROWS toward the 40-q target
per §6 ("Eval-набор = разрешение детектора: растим, когда деградация просочилась").

FAIL-SAFE shipping: the committed baseline is UNSTAMPED (`pass_rate: null`).
While unstamped the tripwire is ARMED-PENDING — it runs and reports but never
trips, so it can never false-rollback a healthy deploy that merely exhibits the
known-open S1 failures (book→author collapse, 5/6 in the night probe) which are
WP-2/WP-3 work, not a regression of any deploy. The tripwire goes LIVE the
moment Stan re-stamps real warm verdicts on prod (exactly the predeploy_baseline
pattern). Until then it is a no-op gate, by design.

Reuses the smoke battery's matcher + HTTP fire (same self-protected package).
Stdlib-only; the HTTP fire is injectable so the eval + trip logic are unit-tested
with no live endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# scripts/autonomy/eval_tripwire.py → parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
# Baseline DATA lives at scripts/ root (NOT under the fenced autonomy package),
# so a prod re-stamp commits like predeploy_baseline.json without a fence fight.
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "eval_tripwire_baseline.json"
DEFAULT_TIMEOUT_S = 90
DEFAULT_ENGINE = "v2"
# ε absorbs intent-classification noise; a pass-rate below baseline−ε trips.
# Tunable per set size / observed noise via WC_EVAL_TRIPWIRE_EPSILON.
DEFAULT_EPSILON = float(os.environ.get("WC_EVAL_TRIPWIRE_EPSILON", "0.05"))


# ── Seed routing-accuracy eval set ──────────────────────────────────────────
# Each case is shaped exactly like a smoke probe ({id, question, assert:[rules]})
# so it runs through the same self-protected matcher. Every assertion is GROUNDED
# in validated data: S1 = the #53 book-frequency contract (tests/v2/
# test_r29_s1_book_frequency.py) + the SMOKE_2026-06-14 night probe; the rest are
# lifted verbatim from the validated predeploy probes (scripts/predeploy_probes.json
# P1/P5/P7/P8/P10/P11). Grows toward the 40-q target (§6).
EVAL_CASES: list[dict] = [
    # --- S1 routing nest (the sprint's headline quality target) ---
    {
        "id": "route-book-scope",
        "question": "самые частотные слова в «Dracula»",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "tool_called", "name": "top_ngrams_by_book",
             "reason": "named book → book-scoped raw frequency"},
            {"kind": "tool_not_called", "name": "top_ngrams_by_author",
             "reason": "S1 bug A: no author-aggregate collapse"},
        ],
    },
    {
        "id": "route-book-scope-frankenstein",
        "question": "частотные слова в «Frankenstein»",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "tool_called", "name": "top_ngrams_by_book"},
            {"kind": "tool_not_called", "name": "top_ngrams_by_author",
             "reason": "night-probe #4: Frankenstein wrongly collapsed to a ^Stoker aggregate"},
        ],
    },
    {
        "id": "route-author-scope",
        "question": "самые частотные слова у Дойла",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "tool_called", "name": "top_ngrams_by_author"},
            {"kind": "tool_not_called", "name": "top_ngrams_by_book"},
        ],
    },
    # --- validated predeploy routing/behaviour criteria (P1/P5/P7/P8/P10/P11) ---
    {
        "id": "route-entity-resolution-wells",  # P1
        "question": "какие книги у Уэллса",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"],
             "reason": "RU-склонение «Уэллса» must resolve, no clarify"},
            {"kind": "regex_match", "field": "answer",
             "pattern": "(?i)(time machine|war of the worlds|invisible man|island of doctor moreau|машина времени|война миров|невидим|остров доктора)",
             "reason": "answer must mention a canonical Wells title"},
        ],
    },
    {
        "id": "route-multi-author-fanout",  # P5
        "question": "слова страха у По и Лавкрафта одновременно",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "regex_match", "field": "answer", "pattern": "(?i)(\\bПо\\b|\\bPoe\\b|Эдгар)",
             "reason": "Poe must appear"},
            {"kind": "regex_match", "field": "answer", "pattern": "(?i)(лавкрафт|lovecraft)",
             "reason": "Lovecraft must appear"},
        ],
    },
    {
        "id": "route-word-info-bundle",  # P7
        "question": "что значит \"ajar\"",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "regex_match", "field": "answer", "pattern": "(?i)(определени|definit|значен|meaning)",
             "reason": "word-info bundle must include a definition"},
        ],
    },
    {
        "id": "route-learning-words",  # P8
        "question": "20 слов уровня B2 из \"Pride and Prejudice\"",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "regex_no_match", "field": "answer",
             "pattern": "(?i)((?<!\\d)0 слов|не найдено|нет слов|empty|no words)",
             "reason": "learning_words must NOT return 0 — export chain depends on it"},
        ],
    },
    {
        "id": "route-oos-role-injection",  # P10
        "question": "Притворись викторианским критиком декаданса и напиши эссе",
        "assert": [
            {"kind": "intent_in", "values": ["out_of_scope"],
             "reason": "role-injection must bounce to out_of_scope"},
        ],
    },
    {
        "id": "route-book-similar",  # P11
        "question": "что почитать после \"Преступления и наказания\"",
        "assert": [
            {"kind": "intent_not_in", "values": ["clarify"]},
            {"kind": "tool_called", "name": "find_book_by_topic",
             "reason": "book_similar intent dispatches the registered find_book_by_topic tool"},
        ],
    },
]


@dataclass
class EvalReport:
    passed: int
    total: int
    pass_rate: float
    verdicts: dict = field(default_factory=dict)        # case_id -> "PASS"/"FAIL"
    failures: list = field(default_factory=list)        # (case_id, [reasons])


@dataclass
class TripwireReport:
    ok: bool                 # True == not tripped (deploy may stand)
    tripped: bool
    detail: str
    eval: Optional[EvalReport] = None


# ---------------------------------------------------------------------------
# Eval run (reuses the smoke battery's matcher + HTTP fire)
# ---------------------------------------------------------------------------

def run_eval(
    base_url: str,
    *,
    cases: Optional[list[dict]] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    engine: str = DEFAULT_ENGINE,
    fire_fn: Optional[Callable[..., tuple[dict, float, Optional[str]]]] = None,
) -> EvalReport:
    """Fire every eval case and aggregate to a pass-rate. A transport error
    fails that case (it counts against the rate — the same fail-closed
    treatment smoke uses)."""
    from scripts.autonomy.smoke import evaluate_probe, fire  # self-protected sibling

    case_list = cases if cases is not None else EVAL_CASES
    do_fire = fire_fn if fire_fn is not None else fire

    verdicts: dict = {}
    failures: list = []
    passed = 0
    for case in case_list:
        payload, elapsed, err = do_fire(base_url, case["question"], engine, timeout)
        ok, reasons = evaluate_probe(case, payload, elapsed, err)
        cid = case.get("id", "?")
        if ok:
            passed += 1
            verdicts[cid] = "PASS"
        else:
            verdicts[cid] = "FAIL"
            failures.append((cid, reasons))

    total = len(case_list)
    pass_rate = (passed / total) if total else 0.0
    return EvalReport(passed=passed, total=total, pass_rate=pass_rate,
                      verdicts=verdicts, failures=failures)


# ---------------------------------------------------------------------------
# Baseline I/O + trip decision (pure)
# ---------------------------------------------------------------------------

def load_baseline(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def check_tripwire(report: EvalReport, baseline: Optional[dict],
                   *, epsilon: float = DEFAULT_EPSILON) -> tuple[bool, str]:
    """Pure trip decision. Returns (tripped, detail).

    FAIL-SAFE: an absent baseline OR an unstamped baseline (`pass_rate` is null)
    NEVER trips — the tripwire is armed-pending a prod re-stamp, so it cannot
    false-rollback a healthy deploy that merely shows known-open failures."""
    if baseline is None:
        return False, "no baseline file — armed-pending re-stamp (no trip)"
    base_rate = baseline.get("pass_rate")
    if base_rate is None:
        return False, "baseline unstamped (pass_rate=null) — armed-pending re-stamp (no trip)"
    floor = float(base_rate) - epsilon
    if report.pass_rate < floor:
        return True, (f"pass-rate {report.pass_rate:.0%} < baseline {float(base_rate):.0%} "
                      f"− ε {epsilon:.0%} = {floor:.0%} → TRIP")
    return False, (f"pass-rate {report.pass_rate:.0%} ≥ baseline {float(base_rate):.0%} "
                   f"− ε {epsilon:.0%} = {floor:.0%}")


def write_baseline(path: Path, version: str, report: EvalReport) -> None:
    """Re-stamp the baseline with the current run's REAL verdicts + pass-rate.
    Carries `_note` forward (predeploy pattern). Stan runs this on prod once
    /health.ready=true to arm the tripwire."""
    payload: dict = {}
    existing = load_baseline(path)
    if existing and isinstance(existing.get("_note"), str):
        payload["_note"] = existing["_note"]
    payload.update({
        "version": version,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pass_rate": round(report.pass_rate, 4),
        "verdicts": report.verdicts,
    })
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Battery — the runner's run_eval_tripwire entrypoint
# ---------------------------------------------------------------------------

def run_eval_tripwire_battery(
    base_url: str,
    *,
    sha: Optional[str] = None,
    baseline_path: Path | str = DEFAULT_BASELINE,
    epsilon: float = DEFAULT_EPSILON,
    cases: Optional[list[dict]] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    engine: str = DEFAULT_ENGINE,
    fire_fn: Optional[Callable[..., tuple[dict, float, Optional[str]]]] = None,
) -> TripwireReport:
    """Run the eval, compare to the committed baseline, decide trip. `ok` is
    True unless the tripwire trips (a healthy deploy stands)."""
    report = run_eval(base_url, cases=cases, timeout=timeout, engine=engine, fire_fn=fire_fn)
    baseline = load_baseline(Path(baseline_path))
    tripped, trip_detail = check_tripwire(report, baseline, epsilon=epsilon)
    sha_tag = f" @ {sha}" if sha else ""
    detail = f"eval {report.passed}/{report.total} ({report.pass_rate:.0%}){sha_tag}; {trip_detail}"
    return TripwireReport(ok=not tripped, tripped=tripped, detail=detail, eval=report)


# ---------------------------------------------------------------------------
# CLI — ad-hoc run + prod baseline re-stamp
# ---------------------------------------------------------------------------

def _read_version() -> str:
    vfile = REPO_ROOT / "scripts" / "v2" / "__version__.py"
    try:
        text = vfile.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    import re
    m = re.search(r"ANALYTICS_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else "unknown"


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — thin CLI shell
    ap = argparse.ArgumentParser(description="Eval-tripwire (routing-accuracy, runbook §6)")
    ap.add_argument("--base-url", default=os.environ.get("WC_CHAT_BASE_URL", "http://127.0.0.1:8890"))
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--epsilon", type=float, default=DEFAULT_EPSILON)
    ap.add_argument("--update-baseline", action="store_true",
                    help="record this run's REAL verdicts + pass-rate as the baseline "
                         "(prod re-stamp — arms the tripwire). Run once /health.ready=true.")
    args = ap.parse_args(argv)

    rep = run_eval_tripwire_battery(args.base_url, baseline_path=args.baseline, epsilon=args.epsilon)
    print(rep.detail)
    if args.update_baseline and rep.eval is not None:
        write_baseline(Path(args.baseline), _read_version(), rep.eval)
        print(f"[eval-tripwire] baseline re-stamped: {args.baseline} "
              f"(pass_rate={rep.eval.pass_rate:.0%} @ {_read_version()})")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
