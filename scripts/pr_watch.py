#!/usr/bin/env python3
"""§3 merge-eligibility evaluator + post-merge deploy trigger — the in-repo,
unit-tested core of the SOW ``wordcracker-pr-watch`` daemon (AUTONOMY_RUNBOOK_R-30
§3 + §4 + §7; R-30 Phase B B2, the merge half of autonomy).

WHAT THIS IS (and is NOT):
  The ``wordcracker-pr-watch`` daemon already runs hourly on the prod host (SOW).
  Phase B B2 replaces its old "merge via Chrome-with-OK" step with a real,
  unattended, machine-checkable decision. That decision logic lives HERE — a
  proper module the daemon imports / invokes (``python3 -m scripts.pr_watch``)
  once per poll — instead of in the daemon's ad-hoc loop, so it is version-
  controlled and unit-tested. This is NOT a second daemon (locked decision #4):
  the hourly trigger stays on SOW; this is the per-poll body it calls.

WHY OUTSIDE THE FENCE:
  The merge MACHINERY that performs an irreversible action — `merge_gate.py`'s
  `GhMergeStep` / the deploy runner — is fenced (``scripts/autonomy/**``). This
  module is the *policy* layer that only DECIDES eligibility and then calls that
  fenced machinery; the actual merge still passes through `GhMergeStep.run`
  (kill-switch + audit) and GitHub's required-status-checks (`--auto`). So the
  agent may iterate on the policy autonomously while every real merge stays
  gated. Hence `scripts/pr_watch.py`, deliberately not under the fence.

ONE POLL PASS (per open PR authored by the agent):
  A. gather facts  — `gh pr checks` / `gh pr view` (read-only).
  B. evaluate()    — the PURE §3 decision (no I/O): MERGE | WAIT | SKIP_FENCED |
                     SKIP_ALREADY. The locked §3 gate (Phase B B1):
                       • required checks (NOT the fence) all SUCCESS, and
                       • the Scope-fence job clean (SUCCESS) — evaluator-enforced,
                         not a required GitHub check (decision #1), and
                       • the `self-reviewed` label present (decision #2).
  C. merge         — on MERGE: build a `MergeContext` and run `GhMergeStep`
                     (`gh pr merge --squash --auto`). `run()` reads the
                     kill-switch and writes exactly one §7 audit line.
  D. deploy        — SEPARATE from the merge return (`--auto` is async): watch
                     the real main-HEAD advance and fire the (already-live,
                     Phase A) deploy runner, with idempotency + a P7-flaky
                     backoff so a flaky deploy gate does not redeploy the same
                     main every poll.

Stdlib-only and self-contained like the autonomy siblings — imports instantly
under a bare prod-host interpreter. Every external effect (every `gh`/`git`
subprocess, the deploy-runner invocation, the on-host state files) is a small
overridable method, so the decision logic is unit-tested with pure fakes — no
`gh`, no git, no network, no files (tests/v2/test_merge_gate.py).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# scripts/pr_watch.py → parents[1] == repo root. Same sys.path bootstrap the
# fenced siblings use, so the autonomy imports resolve under BOTH
# `python -m scripts.pr_watch` AND a bare `python scripts/pr_watch.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.autonomy import control  # noqa: E402  (after the sys.path bootstrap)
from scripts.autonomy.merge_gate import GhMergeStep, MergeContext  # noqa: E402
from scripts.autonomy import deploy_runner  # noqa: E402  (exit-code bands, no copy)

# ── The §3 gate inputs (locked, Phase B B1) ────────────────────────────────
# EXACT job names as GitHub / `gh pr checks --json name` reports them — the
# `name:` fields of the predeploy + scope-fence workflows. Drift here = a check
# silently ignored, so they are pinned, not pattern-matched.
REQUIRED_CHECKS = (
    "tests/v2 (R10 collect + full directory run)",
    "Mandatory version-bump",
    "12-probe config sanity",
)
FENCE_JOB = "Scope-fence (🔴 reversibility gate)"   # evaluator-enforced, NOT required (decision #1)
SELF_REVIEW_LABEL = "self-reviewed"                  # decision #2

# ── merge-eligibility decisions (STEP B) ───────────────────────────────────
MERGE = "MERGE"                  # all green + fence clean + label → enable auto-merge
WAIT = "WAIT"                    # not yet eligible — re-evaluate next poll
SKIP_FENCED = "SKIP_FENCED"      # fence red → withhold; bootstrap hand-merge for Stan
SKIP_ALREADY = "SKIP_ALREADY"    # auto-merge already enabled — idempotent no-op

# ── deploy-trigger decisions (STEP D) ──────────────────────────────────────
DEPLOY = "DEPLOY"                        # new main HEAD → run the deploy runner
DEPLOY_SKIP_CURRENT = "DEPLOY_SKIP_CURRENT"   # this main already deployed (idempotent)
DEPLOY_SKIP_BACKOFF = "DEPLOY_SKIP_BACKOFF"   # this main failed before → backoff (P7-flaky)
DEPLOY_SKIP_NO_SHA = "DEPLOY_SKIP_NO_SHA"     # could not resolve remote main HEAD

# Deploy-runner exit codes that mean "this main failed to deploy / was rolled
# back" → blacklist the SHA so a flaky gate is not redeployed every poll. Taken
# from the runner itself (no copy — R6): 20 DEPLOY_FAILED · 21 ROLLED_BACK ·
# 22 ROLLBACK_FAILED · 23 NO_ROLLBACK_TARGET. 0 OK, 24 kill-switch and 25
# precondition are deliberately NOT here — those are not flaky-gate failures.
DEPLOY_BACKOFF_CODES = frozenset({
    deploy_runner.EXIT_DEPLOY_FAILED,
    deploy_runner.EXIT_ROLLED_BACK,
    deploy_runner.EXIT_ROLLBACK_FAILED,
    deploy_runner.EXIT_NO_ROLLBACK_TARGET,
})

# On-host (SOW) state files — host-local, untracked, never committed (same
# posture as AUTONOMY_KILLSWITCH / AUTONOMY_LOG.md). Operator clears the failed
# file by hand to re-enable a backed-off SHA.
LAST_DEPLOYED_SHA_FILE = "AUTONOMY_LAST_DEPLOYED_SHA"
FAILED_SHA_FILE = "AUTONOMY_FAILED_SHA"

# Idempotency marker for the one-time scope-fenced PR comment (decision #1 / §3.2).
FENCE_COMMENT_MARKER = "<!-- autonomy:scope-fenced -->"


@dataclass
class PrFacts:
    """The read-only facts about ONE open PR that the §3 decision is a pure
    function of (STEP A output → STEP B input). `checks` maps a check-name to
    its (upper-cased) state; `fence_state` is the Scope-fence job's state held
    out separately because the fence is evaluator-enforced, not a required
    check. Constructed directly in tests — no `gh` needed to exercise B."""
    pr: int | str
    checks: dict                                     # check-name -> state (SUCCESS/FAILURE/PENDING/SKIPPED/…)
    fence_state: str = ""                            # Scope-fence job state ("" if not reported yet)
    labels: List[str] = field(default_factory=list)  # label names on the PR
    auto_merge_enabled: bool = False                 # is autoMergeRequest already set?
    head_sha: str = ""                               # PR head SHA (best-effort; squash makes a new main SHA)
    pins_flipped: List[str] = field(default_factory=list)  # characterization pins flipped (audit; [] at start)


@dataclass
class Decision:
    """A §3 merge-eligibility verdict: one of the MERGE/WAIT/SKIP_* constants
    plus a human reason for the log + the (one-time) fenced comment."""
    action: str
    reason: str = ""


@dataclass
class DeployDecision:
    """A STEP-D deploy-trigger verdict: DEPLOY or one of the DEPLOY_SKIP_*
    constants, plus a reason."""
    action: str
    reason: str = ""


# ───────────────────────────────────────────────────────────────── STEP B ──
def evaluate(facts: PrFacts) -> Decision:
    """The PURE §3 merge-eligibility decision — no I/O, fully unit-tested.

    Order matters and is the locked Phase B B2 contract:
      1. auto-merge already enabled        → SKIP_ALREADY (idempotent; never re-arm).
      2. Scope-fence == FAILURE            → SKIP_FENCED  (withhold — the PR touches
                                             an irreversible path; Stan hand-merges).
      3. every required check SUCCESS AND  → MERGE.
         Scope-fence SUCCESS (clean) AND
         the `self-reviewed` label present
      4. otherwise                         → WAIT (re-evaluated next poll).

    Note step 3 requires the fence to be explicitly clean (SUCCESS), so a fence
    that is merely still PENDING falls through to WAIT — we NEVER enable
    auto-merge before the reversibility gate has confirmed clean."""
    if facts.auto_merge_enabled:
        return Decision(SKIP_ALREADY, "auto-merge already enabled")

    fence = (facts.fence_state or "").upper()
    if fence == "FAILURE":
        return Decision(SKIP_FENCED, "scope-fence red — irreversible path; "
                                     "bootstrap hand-merge for Stan")

    missing = [c for c in REQUIRED_CHECKS
               if (facts.checks.get(c) or "").upper() != "SUCCESS"]
    has_label = SELF_REVIEW_LABEL in (facts.labels or [])
    fence_clean = fence == "SUCCESS"

    if missing or not has_label or not fence_clean:
        reasons = []
        if missing:
            reasons.append("required checks not green: " + ", ".join(missing))
        if not fence_clean:
            reasons.append(f"scope-fence not clean (state={facts.fence_state or '?'})")
        if not has_label:
            reasons.append(f"missing label '{SELF_REVIEW_LABEL}'")
        return Decision(WAIT, "; ".join(reasons))

    return Decision(MERGE, "required checks green, scope-fence clean, self-reviewed")


# ───────────────────────────────────────────────────────────────── STEP D ──
def decide_deploy(remote_sha: str, last_deployed_sha: str,
                  failed_sha: str) -> DeployDecision:
    """The PURE post-merge deploy-trigger decision — no I/O, unit-tested.

      • no remote SHA resolved            → DEPLOY_SKIP_NO_SHA.
      • remote == last successfully-       → DEPLOY_SKIP_CURRENT (already shipped;
        deployed SHA                         do not redeploy the same main).
      • remote == a SHA that previously    → DEPLOY_SKIP_BACKOFF (P7-flaky interim:
        failed to deploy                     a flaky deploy gate must not redeploy
                                             the same main every poll).
      • a NEW main SHA                     → DEPLOY.

    Re-enabling a backed-off SHA is by construction: a new merge produces a new
    SHA (≠ failed) → DEPLOY; or the operator clears the failed-SHA file on SOW
    (failed → "") → DEPLOY. No timer, no auto-retry of the same flaky SHA."""
    if not remote_sha:
        return DeployDecision(DEPLOY_SKIP_NO_SHA, "could not resolve remote main HEAD")
    if remote_sha == last_deployed_sha:
        return DeployDecision(DEPLOY_SKIP_CURRENT, f"main {remote_sha} already deployed")
    if remote_sha == failed_sha:
        return DeployDecision(DEPLOY_SKIP_BACKOFF,
                              f"main {remote_sha} previously failed to deploy — backoff "
                              "until a new merge or a manual clear (P7-flaky interim)")
    return DeployDecision(DEPLOY, f"new main {remote_sha}")


class PrWatcher:
    """One poll pass of ``wordcracker-pr-watch``: evaluate each open PR (STEP
    A→B→C) and trigger a deploy on a real main advance (STEP D).

    Every external effect (each `gh`/`git` subprocess, the deploy-runner
    invocation, the on-host state files, the merge step) is a thin overridable
    method, so `poll_open_prs()` / `maybe_deploy()` are exercised in tests with
    pure fakes. Production uses the real subprocess implementations unchanged."""

    def __init__(
        self,
        *,
        repo_root: Path | str = REPO_ROOT,
        runner_id: str = "pr-watch",
        author: str = "@me",
        audit_path: Path | str | None = None,
        clock: Optional[Callable[[], str]] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.runner_id = runner_id
        # Scope merges to the agent's OWN open PRs (runbook §3: "автор = агент").
        # A human PR without the `self-reviewed` label can never reach MERGE
        # anyway, but the author filter keeps the poller from even touching it.
        self.author = author
        self.audit_path = (Path(audit_path) if audit_path
                           else control.audit_path_for(self.repo_root))
        self._clock = clock
        self.log = log if log is not None else print

    # ============================================================= one pass ==
    def run_once(self, *, deploy: bool = True) -> None:
        """A full poll pass: evaluate every eligible PR, then (optionally) the
        post-merge deploy trigger. The SOW daemon calls this hourly."""
        self.poll_open_prs()
        if deploy:
            self.maybe_deploy()

    # ----------------------------------------------------------- STEP A+B+C --
    def poll_open_prs(self) -> List[Decision]:
        """Evaluate every open PR authored by the agent. Errors on one PR are
        logged and skipped — one bad PR never stalls the rest of the pass."""
        prs = self.list_open_prs()
        self.log(f"[pr-watch] {len(prs)} open PR(s) by {self.author}: {prs}")
        decisions: List[Decision] = []
        for pr in prs:
            try:
                decisions.append(self.process_pr(pr))
            except Exception as e:  # noqa: BLE001 — one PR's failure isolates here
                self.log(f"[pr-watch] PR {pr}: error {type(e).__name__}: {e}")
        return decisions

    def process_pr(self, pr) -> Decision:
        """STEP A→B→C for one PR: gather facts, decide, and act on the decision."""
        facts = self.gather_facts(pr)
        decision = evaluate(facts)
        self.log(f"[pr-watch] PR {pr}: {decision.action} — {decision.reason}")

        if decision.action == SKIP_FENCED:
            self.announce_fenced(pr)          # one-time comment, then idempotent skip
            return decision
        if decision.action != MERGE:
            return decision                   # SKIP_ALREADY / WAIT → nothing to do

        # STEP C — enable auto-merge through the fenced, kill-switch-gated step.
        # scope_fence is "clean" by construction (we only reach MERGE when the
        # fence is SUCCESS); eval_delta is "deploy-time" because the eval-tripwire
        # is a deploy gate, not a merge gate (decision #6) — its real delta is
        # recorded by the deploy runner post-merge.
        ctx = MergeContext(pr=facts.pr, sha=facts.head_sha,
                           pins_flipped=facts.pins_flipped,
                           scope_fence="clean", eval_delta="deploy-time")
        rc = self.make_merge_step().run(ctx)
        self.log(f"[pr-watch] PR {pr}: merge-step rc={rc} "
                 f"(0=auto-merge enabled · 24=kill-switch · 30=gh failed)")
        return decision

    # ----------------------------------------------------------------- STEP D --
    def maybe_deploy(self) -> DeployDecision:
        """Trigger the (Phase-A-live) deploy runner on a real main advance —
        decoupled from the merge return because `--auto` is async. Idempotent
        via the last-deployed-SHA file; P7-flaky-safe via the failed-SHA backoff
        (a failed/rolled-back deploy blacklists that SHA until a new merge or a
        manual clear, so a flaky gate is not redeployed every poll)."""
        remote = self.resolve_remote_main_sha()
        decision = decide_deploy(remote, self.read_last_deployed_sha(),
                                 self.read_failed_sha())
        if decision.action != DEPLOY:
            self.log(f"[pr-watch] deploy: {decision.action} — {decision.reason}")
            return decision

        self.log(f"[pr-watch] deploy: triggering deploy runner for main {remote}")
        rc = self.run_deploy_runner()
        if rc == deploy_runner.EXIT_OK:
            self.write_last_deployed_sha(remote)
            self.clear_failed_sha()
            self.log(f"[pr-watch] deploy: DEPLOY_OK — last-deployed-sha now {remote}")
        elif rc in DEPLOY_BACKOFF_CODES:
            # Deploy failed / rolled back (e.g. the flaky P7 gate). Blacklist this
            # SHA + flag Stan ONCE; do NOT auto-retry the same main (no churn).
            self.write_failed_sha(remote)
            self.flag_stan("DEPLOY_BACKOFF", sha=remote, rc=rc,
                           detail="deploy runner failed/rolled-back; no auto-retry of "
                                  "this main (P7-flaky interim) — re-enabled by the next "
                                  f"merge or by clearing {FAILED_SHA_FILE} on SOW")
            self.log(f"[pr-watch] deploy: rc={rc} → backoff on {remote}; flagged Stan")
        else:
            # 24 kill-switch / 25 precondition / other transient — NOT a flaky-gate
            # failure, so do not blacklist; it retries naturally next poll.
            self.log(f"[pr-watch] deploy: rc={rc} (kill-switch/precondition/transient) — "
                     f"not blacklisting {remote}; will retry next poll")
        return decision

    # =====================================================================
    # External-effect methods — overridden wholesale in tests.
    # Production implementations below; all subprocess / files, no project state.
    # =====================================================================

    def _run(self, argv: List[str]) -> subprocess.CompletedProcess:
        """Run a command in the repo root, capturing output. The single seam
        the subprocess-level tests patch."""
        return subprocess.run(argv, cwd=str(self.repo_root), text=True,
                              capture_output=True)

    def _gh_json(self, argv: List[str], *, accept_nonzero: bool = False):
        """Run a `gh ... --json ...` command and parse its stdout. Returns the
        parsed value, or None on a real failure (logged). A read failure
        degrades to None → the PR is simply re-evaluated next poll, never merged
        on missing facts.

        `accept_nonzero` is for `gh pr checks`, whose exit code ENCODES the
        aggregate check state (0 all-pass, 8 pending, non-zero on failure) while
        STILL emitting the per-check JSON — so a non-zero exit there is not a
        tool error and its body must be read (else a red fence / pending checks
        would look like "no facts"). For `gh pr view` / `gh pr list`, a non-zero
        exit IS a real error (default)."""
        proc = self._run(argv)
        if proc.returncode != 0 and not accept_nonzero:
            self.log(f"[pr-watch] {' '.join(argv[:3])} failed rc={proc.returncode}: "
                     f"{(proc.stderr or '').strip()}")
            return None
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as e:
            self.log(f"[pr-watch] could not parse JSON from {' '.join(argv[:3])} "
                     f"(rc={proc.returncode}): {e}")
            return None

    def list_open_prs(self) -> List[int]:
        """Open PR numbers authored by the agent (`gh pr list`)."""
        data = self._gh_json(["gh", "pr", "list", "--state", "open",
                              "--author", self.author, "--json", "number"]) or []
        return [d["number"] for d in data if isinstance(d, dict) and "number" in d]

    def gather_facts(self, pr) -> PrFacts:
        """STEP A — collect the §3 facts for one PR via read-only `gh`:
          • `gh pr checks --json name,state` → per-check state (a name counts as
            SUCCESS only if EVERY row for it is SUCCESS), with the Scope-fence
            job's state held out separately;
          • `gh pr view --json labels,autoMergeRequest,headRefOid` → labels,
            whether auto-merge is already enabled, and the head SHA."""
        checks_raw = self._gh_json(["gh", "pr", "checks", str(pr),
                                    "--json", "name,state"], accept_nonzero=True) or []
        agg: dict = defaultdict(list)
        for c in checks_raw:
            if isinstance(c, dict):
                agg[c.get("name", "")].append((c.get("state") or "").upper())
        checks: dict = {}
        for name, states in agg.items():
            if states and all(s == "SUCCESS" for s in states):
                checks[name] = "SUCCESS"
            else:
                non = [s for s in states if s != "SUCCESS"]
                checks[name] = "FAILURE" if "FAILURE" in non else (non[0] if non else "")

        view = self._gh_json(["gh", "pr", "view", str(pr), "--json",
                              "labels,autoMergeRequest,headRefOid"]) or {}
        labels = [l.get("name", "") for l in (view.get("labels") or [])
                  if isinstance(l, dict)]
        return PrFacts(
            pr=pr,
            checks=checks,
            fence_state=checks.get(FENCE_JOB, ""),
            labels=labels,
            auto_merge_enabled=bool(view.get("autoMergeRequest")),
            head_sha=view.get("headRefOid") or "",
        )

    def make_merge_step(self) -> GhMergeStep:
        """The production merge step (`gh pr merge --squash --auto`), sharing
        this watcher's audit sink + clock. Overridden in tests with a spy."""
        return GhMergeStep(repo_root=self.repo_root, runner_id="pr-watch-merge",
                           audit_path=self.audit_path, clock=self._clock, log=self.log)

    # -- scope-fenced one-time notice (decision #1 / §3.2) -------------------
    def announce_fenced(self, pr) -> None:
        """Post the 'fenced — hand-merge for Stan' comment ONCE per PR, keyed by
        an HTML-comment marker so re-polls stay quiet (no spam)."""
        if self.already_announced_fenced(pr):
            return
        body = (f"{FENCE_COMMENT_MARKER}\n"
                "🔴 **Scope-fence red.** This PR touches an irreversible path "
                "(`scripts/autonomy/**`, CI workflows, deploy config, …). "
                "Auto-merge is withheld — this is a **bootstrap hand-merge for "
                "Stan** (AUTONOMY_RUNBOOK_R-30 §3.2 / §5). Required tests still "
                "gate it; merge with the normal button once reviewed.")
        self.post_comment(pr, body)
        self.log(f"[pr-watch] PR {pr}: posted scope-fenced notice")

    def already_announced_fenced(self, pr) -> bool:
        data = self._gh_json(["gh", "pr", "view", str(pr), "--json", "comments"]) or {}
        return any(FENCE_COMMENT_MARKER in (c.get("body") or "")
                   for c in (data.get("comments") or []) if isinstance(c, dict))

    def post_comment(self, pr, body: str) -> None:
        self._run(["gh", "pr", "comment", str(pr), "--body", body])

    # -- STEP D effects -----------------------------------------------------
    def resolve_remote_main_sha(self) -> str:
        """The real main HEAD on origin (`git ls-remote origin main`) — the
        authority for "a PR actually merged" (a squash makes a NEW main SHA, so
        the merge-step's PR-head SHA is best-effort and not used here)."""
        proc = self._run(["git", "ls-remote", "origin", "main"])
        if proc.returncode != 0:
            self.log(f"[pr-watch] git ls-remote failed rc={proc.returncode}: "
                     f"{(proc.stderr or '').strip()}")
            return ""
        out = (proc.stdout or "").strip()
        return out.split()[0] if out else ""

    def run_deploy_runner(self) -> int:
        """Invoke the (fenced, Phase-A-live) deploy runner as a fresh subprocess
        — `python scripts/autonomy/deploy_runner.py --runner-id auto-deploy` —
        so it gets a clean interpreter + does its own `git checkout main && pull`
        in the repo root. Returns its exit code (0 OK · 20-23 failed/rolled-back
        · 24 kill-switch · 25 precondition)."""
        proc = self._run([sys.executable, "scripts/autonomy/deploy_runner.py",
                          "--runner-id", "auto-deploy"])
        out = (proc.stdout or "").rstrip()
        err = (proc.stderr or "").rstrip()
        if out:
            self.log(out)
        if err:
            self.log(err)
        return proc.returncode

    def _state_path(self, name: str) -> Path:
        return self.repo_root / name

    def read_last_deployed_sha(self) -> str:
        p = self._state_path(LAST_DEPLOYED_SHA_FILE)
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def write_last_deployed_sha(self, sha: str) -> None:
        self._state_path(LAST_DEPLOYED_SHA_FILE).write_text(sha + "\n", encoding="utf-8")

    def read_failed_sha(self) -> str:
        p = self._state_path(FAILED_SHA_FILE)
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def write_failed_sha(self, sha: str) -> None:
        self._state_path(FAILED_SHA_FILE).write_text(sha + "\n", encoding="utf-8")

    def clear_failed_sha(self) -> None:
        p = self._state_path(FAILED_SHA_FILE)
        if p.exists():
            p.unlink()

    def flag_stan(self, event: str, *, sha: str = "", rc: Optional[int] = None,
                  detail: str = "") -> str:
        """Surface a one-off operator flag into the SAME `AUTONOMY_LOG.md` trail
        Stan reads (runbook §7), using the shared `control` formatter so the
        line shape matches the merge / deploy lines. Written once at the moment
        of the backoff, not on every quiet re-poll."""
        ts = self._clock() if self._clock is not None else control.now_utc()
        fields: list = [("sha", sha or "-")]
        if rc is not None:
            fields.append(("rc", rc))
        fields.append(("runner", self.runner_id))
        line = control.format_audit_line(ts, event, fields, detail)
        control.append_audit(self.audit_path, line)
        self.log(f"[pr-watch] FLAG → {line}")
        return line


# ---------------------------------------------------------------------------
# CLI — one poll pass; the SOW `wordcracker-pr-watch` daemon invokes this hourly
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="§3 merge-eligibility evaluator + post-merge deploy trigger — "
                    "one poll pass (AUTONOMY_RUNBOOK_R-30 §3/§4; R-30 Phase B B2)")
    ap.add_argument("--repo", default=str(REPO_ROOT),
                    help="repo dir to operate on (default: this checkout)")
    ap.add_argument("--author", default="@me",
                    help="only consider open PRs by this author (default: the token's user)")
    ap.add_argument("--runner-id", default="pr-watch",
                    help="label written into any audit-log line this poller emits")
    ap.add_argument("--no-deploy", action="store_true",
                    help="evaluate/merge only; skip the STEP D deploy trigger")
    args = ap.parse_args(argv)
    watcher = PrWatcher(repo_root=Path(args.repo), runner_id=args.runner_id,
                        author=args.author)
    watcher.run_once(deploy=not args.no_deploy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
