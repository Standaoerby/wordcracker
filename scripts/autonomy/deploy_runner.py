#!/usr/bin/env python3
"""Deploy + verify + smoke runner — the post-merge autonomy entrypoint
(AUTONOMY_RUNBOOK_R-30 §4). WP-0 deliverable #2.

The single command an agent (or the auto-merge machinery, #5) runs AFTER a PR
lands on main, to ship that main to prod with a rollback safety net:

    git checkout main && git pull              (R-CLEAN-START, runbook §2/§4)
      → bash scripts/deploy.sh                 (build + compose up; OWNS its
                                                 own verify+probe internal
                                                 rollback — exit 10/11/12)
      → bash scripts/verify_deployed_image.sh <main-HEAD-sha>
                                                 (assert the LIVE image SHA ==
                                                  merged main HEAD, runbook §4)
      → smoke-as-code asserts                  (#3 seam — S1-style probes)
      → eval-tripwire                          (#4 seam — 40-q eval vs baseline,
                                                 runbook §6)

Auto-rollback fires on ANY runner-level gate failure (verify / smoke /
eval-tripwire): the runner re-runs `deploy.sh --rollback <previous-sha>` to
redeploy the image that was live BEFORE this deploy, writes ONE line to the
audit-log, and STOPS. It never retries blindly (runbook §4).

Division of rollback labour — important:
  * deploy.sh OWNS the rollback for ITS OWN gates (the internal docker-tag /
    /health verify + the 12-probe predeploy gate). A non-zero deploy.sh exit
    means it already rolled back (10), could not (12 — no target), or its
    rollback also failed (11). The runner does NOT re-roll-back on a deploy.sh
    failure — that would be a blind retry. It records the deploy.sh exit code
    and stops.
  * The runner OWNS the rollback for the gates that run AFTER deploy.sh returns
    green: the runbook-§4 SHA re-assert, smoke-as-code, and the eval-tripwire.
    deploy.sh has already swapped the live image by then, so the runner
    captures the previous-live SHA ITSELF (before calling deploy.sh) to have a
    rollback target by construction — the same `docker inspect` mechanism
    deploy.sh uses internally (deploy.sh §"capture PREVIOUS_SHA").

Clean seams for the rest of WP-0 (override the method, keep the contract —
`StepResult(ok=False, ...)` from a gate triggers the rollback path):
  * `run_smoke()`        — WP-0 #3 wires the real smoke-as-code battery.
  * `run_eval_tripwire()`— WP-0 #4 wires the 40-q eval + committed baseline.
  * `write_audit()` / `check_killswitch()` — WP-0 #5 hardens the audit-log
    (`AUTONOMY_LOG.md`, runbook §7) and the kill-switch.

Stdlib-only, no project import — runs on the prod host with a bare interpreter
and imports instantly (same property as scripts/scope_fence/check_scope_fence.py
and scripts/check_version_bump.py). All external effects live in small,
individually-overridable methods so the orchestration is unit-testable with no
docker / git / live endpoint (tests/v2/test_deploy_runner.py).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# scripts/autonomy/deploy_runner.py → parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_NAME = "wordcracker-textlab"
AUDIT_LOG_NAME = "AUTONOMY_LOG.md"

# --- exit codes ------------------------------------------------------------
# Distinct integers so the caller (and Stan, reading the audit-log) can tell
# the outcomes apart without parsing prose. Offset from deploy.sh's own codes
# (0/2/3/6/10/11/12) so a runner code is never mistaken for a deploy.sh code.
EXIT_OK = 0                    # deploy live + every gate green
EXIT_PRECONDITION = 25         # checkout/pull or HEAD-resolve failed; nothing deployed
EXIT_KILLSWITCH = 24           # kill-switch engaged → refused to deploy (nothing touched)
EXIT_DEPLOY_FAILED = 20        # deploy.sh returned non-zero (it owns its own rollback)
EXIT_ROLLED_BACK = 21          # a runner gate tripped → rolled back to the previous SHA
EXIT_NO_ROLLBACK_TARGET = 23   # a runner gate tripped but no previous SHA → manual recovery
EXIT_ROLLBACK_FAILED = 22      # a runner gate tripped AND the rollback redeploy also failed


@dataclass
class StepResult:
    """Outcome of one runner step. `ok` is the gate verdict: True == proceed,
    False == this gate failed (→ rollback for a post-deploy gate; → refuse for
    the kill-switch). `detail` doubles as the payload for the steps that return
    a value (current_head_sha / capture_previous_sha put the SHA in `detail`)."""
    name: str
    ok: bool
    exit_code: Optional[int] = None
    detail: str = ""


class DeployRunner:
    """Orchestrates the runbook-§4 deploy with auto-rollback.

    Every external effect (git, docker, the deploy/verify shell scripts, the
    audit-log write, the kill-switch read) is a thin overridable method, so
    `run()`'s decision logic is exercised in tests with pure fakes. Production
    uses the real subprocess implementations below unchanged.
    """

    def __init__(
        self,
        *,
        repo_root: Path | str = REPO_ROOT,
        runner_id: str = "deploy_runner",
        audit_path: Path | str | None = None,
        chat_base_url: Optional[str] = None,
        clock: Optional[Callable[[], str]] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.runner_id = runner_id
        self.audit_path = Path(audit_path) if audit_path else (self.repo_root / AUDIT_LOG_NAME)
        # Live chat endpoint the smoke battery (#3) fires at. Mirrors deploy.sh's
        # CHAT_BASE_URL default (the local prod chat service on :8890).
        self.chat_base_url = (chat_base_url or os.environ.get("WC_CHAT_BASE_URL")
                              or os.environ.get("CHAT_BASE_URL") or "http://127.0.0.1:8890")
        self._clock = clock
        self.log = log if log is not None else print

    # ----------------------------------------------------------------- run --
    def run(self) -> int:
        """Execute the deploy pipeline once. Returns an EXIT_* code. Writes
        exactly one audit-log line per terminal outcome. Never retries."""
        # 0. Kill-switch (runbook §7) — checked before anything is touched so an
        #    engaged switch is a clean no-op, not a half-deploy.
        ks = self.check_killswitch()
        if not ks.ok:
            self._audit("KILLSWITCH_REFUSE", gate="killswitch", detail=ks.detail)
            self.log(f"[runner] kill-switch engaged — refusing to deploy: {ks.detail}")
            return EXIT_KILLSWITCH

        # 1. R-CLEAN-START: land on a clean, current main.
        sync = self.sync_main()
        if not sync.ok:
            self._audit("PRECONDITION_FAIL", gate="sync", detail=sync.detail)
            self.log(f"[runner] precondition failed (sync): {sync.detail}")
            return EXIT_PRECONDITION

        # The deploy target is exactly merged-main HEAD. verify asserts the
        # LIVE image equals this — the runbook-§4 "SHA == merged main" gate.
        head = self.current_head_sha()
        if not head.ok or not head.detail:
            self._audit("PRECONDITION_FAIL", gate="head_sha", detail=head.detail or "empty HEAD sha")
            self.log(f"[runner] precondition failed (HEAD sha): {head.detail}")
            return EXIT_PRECONDITION
        target_sha = head.detail

        # 2. Capture the currently-live SHA BEFORE deploy replaces it — this is
        #    the rollback target for the runner-owned post-deploy gates. Empty
        #    `detail` == cold start / non-family image: no target (not an error).
        previous_sha = (self.capture_previous_sha().detail or "").strip()

        # 3. Deploy. deploy.sh owns its internal verify+probe rollback; a
        #    non-zero exit is already-handled — the runner records and stops,
        #    it does NOT re-roll-back (no blind retry).
        deploy = self.run_deploy()
        if not deploy.ok:
            self._audit("DEPLOY_FAILED", target=target_sha, previous=previous_sha,
                        gate="deploy.sh", deploy_rc=deploy.exit_code, detail=deploy.detail)
            self.log(f"[runner] deploy.sh failed (rc={deploy.exit_code}) — deploy.sh owns its "
                     f"own rollback; not retrying. STOP.")
            return EXIT_DEPLOY_FAILED

        # 4-6. Runner-owned post-deploy gates, in order. The FIRST failure
        #      rolls back and stops — later gates do not run (the image is
        #      already being reverted).
        gates = (
            lambda: self.run_verify(target_sha),       # runbook §4: SHA == merged main
            lambda: self.run_smoke(target_sha),        # #3 seam
            lambda: self.run_eval_tripwire(target_sha),  # #4 seam, runbook §6
        )
        for gate in gates:
            result = gate()
            if not result.ok:
                return self._rollback_and_stop(result, target_sha, previous_sha,
                                               deploy_rc=deploy.exit_code)

        # 7. Every gate green.
        self._audit("DEPLOY_OK", target=target_sha, previous=previous_sha,
                    deploy_rc=deploy.exit_code, detail="all gates green")
        self.log(f"[runner] OK — {IMAGE_NAME}:{target_sha} is live and verified")
        return EXIT_OK

    def _rollback_and_stop(self, failed: StepResult, target_sha: str,
                           previous_sha: str, *, deploy_rc: Optional[int]) -> int:
        """A runner-owned gate failed post-deploy. Roll back to the pre-deploy
        SHA (if we captured one), record the incident, and STOP — never retry."""
        self.log(f"[runner] GATE FAILED: {failed.name} — {failed.detail}")

        if not previous_sha:
            # No rollback target (cold start). Refuse to invoke deploy.sh
            # --rollback with an empty SHA; surface an unambiguous dead-end.
            self._audit("ROLLBACK_NO_TARGET", target=target_sha, previous="",
                        gate=failed.name, deploy_rc=deploy_rc, detail=failed.detail)
            self.log("[runner] no previous SHA captured — cannot roll back; manual "
                     "recovery required. STOP.")
            return EXIT_NO_ROLLBACK_TARGET

        rollback = self.run_rollback(previous_sha)
        if rollback.ok:
            self._audit("ROLLBACK_OK", target=target_sha, previous=previous_sha,
                        gate=failed.name, deploy_rc=deploy_rc, rollback_rc=rollback.exit_code,
                        detail=f"gate {failed.name} tripped; rolled back to {previous_sha}")
            self.log(f"[runner] rolled back to {previous_sha}; target {target_sha} did not "
                     f"stick. STOP (no blind retry) — flag Stan.")
            return EXIT_ROLLED_BACK

        # Rollback itself failed — loudest possible state.
        self._audit("ROLLBACK_FAILED", target=target_sha, previous=previous_sha,
                    gate=failed.name, deploy_rc=deploy_rc, rollback_rc=rollback.exit_code,
                    detail=f"gate {failed.name} tripped AND rollback to {previous_sha} failed")
        self.log(f"[runner] ROLLBACK ALSO FAILED (rc={rollback.exit_code}) — host is in a bad "
                 f"state; manual recovery required. STOP.")
        return EXIT_ROLLBACK_FAILED

    # =====================================================================
    # External-effect methods — overridden wholesale in tests.
    # Production implementations below; all subprocess, no project import.
    # =====================================================================

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        """Run a command in the repo root, capturing output. The single seam
        the subprocess-level tests patch."""
        return subprocess.run(argv, cwd=str(self.repo_root), text=True,
                              capture_output=True)

    def _echo(self, proc: subprocess.CompletedProcess) -> None:
        """Surface a child script's output through the runner log so the
        operator sees deploy.sh / verify diagnostics inline."""
        out = (proc.stdout or "").rstrip()
        err = (proc.stderr or "").rstrip()
        if out:
            self.log(out)
        if err:
            self.log(err)

    def check_killswitch(self) -> StepResult:
        """Kill-switch read (runbook §7). `ok=True` == clear to proceed.

        WP-0 #5 OWNS the full semantics (where the flag lives, audit on
        refuse, the merge-step half). This minimal real check — env
        `WC_AUTONOMY_KILLSWITCH` truthy, or an `AUTONOMY_KILLSWITCH` flag file
        at repo root — makes the runner honour a freeze from day one so the
        seam is proven wired, not stubbed."""
        env = os.environ.get("WC_AUTONOMY_KILLSWITCH", "").strip().lower()
        if env not in ("", "0", "false", "no", "off"):
            return StepResult("killswitch", ok=False,
                              detail="env WC_AUTONOMY_KILLSWITCH is set")
        flag = self.repo_root / "AUTONOMY_KILLSWITCH"
        if flag.exists():
            return StepResult("killswitch", ok=False,
                              detail=f"flag file present: {flag.name}")
        return StepResult("killswitch", ok=True, detail="clear")

    def sync_main(self) -> StepResult:
        """R-CLEAN-START (runbook §2/§4): `git checkout main && git pull`.
        `--ff-only` so a non-fast-forwardable main is a loud precondition
        failure, never a silent merge commit on the prod host."""
        checkout = self._run(["git", "checkout", "main"])
        if checkout.returncode != 0:
            return StepResult("sync", ok=False, exit_code=checkout.returncode,
                              detail=f"git checkout main failed: {checkout.stderr.strip()}")
        pull = self._run(["git", "pull", "--ff-only"])
        if pull.returncode != 0:
            return StepResult("sync", ok=False, exit_code=pull.returncode,
                              detail=f"git pull --ff-only failed: {pull.stderr.strip()}")
        return StepResult("sync", ok=True, detail="on main, fast-forwarded")

    def current_head_sha(self) -> StepResult:
        """The merged-main HEAD as a short SHA — matches the tag deploy.sh
        builds (`git rev-parse --short`) so verify compares like-for-like."""
        rev = self._run(["git", "rev-parse", "--short", "HEAD"])
        if rev.returncode != 0:
            return StepResult("head_sha", ok=False, exit_code=rev.returncode,
                              detail=rev.stderr.strip())
        return StepResult("head_sha", ok=True, detail=rev.stdout.strip())

    def capture_previous_sha(self) -> StepResult:
        """The image tag of the currently-running gutenberg-lab container,
        read BEFORE deploy replaces it — the rollback target by construction
        (mirrors deploy.sh's internal PREVIOUS_SHA capture). Empty `detail`
        means cold start / not the wordcracker-textlab family: no target, and
        that is a clean state, not an error (`ok=True`)."""
        ps = self._run(["docker", "ps", "--filter", "name=^gutenberg-lab$",
                        "--format", "{{.ID}}"])
        ids = [x for x in (ps.stdout or "").splitlines() if x.strip()] if ps.returncode == 0 else []
        if not ids:
            return StepResult("previous_sha", ok=True, detail="")  # cold start
        inspect = self._run(["docker", "inspect", "--format", "{{.Config.Image}}", ids[0].strip()])
        image = (inspect.stdout or "").strip() if inspect.returncode == 0 else ""
        prefix = f"{IMAGE_NAME}:"
        if image.startswith(prefix):
            return StepResult("previous_sha", ok=True, detail=image[len(prefix):])
        return StepResult("previous_sha", ok=True, detail="")  # not our family → no target

    def run_deploy(self) -> StepResult:
        """`bash scripts/deploy.sh` — deploys HEAD (== main after sync).
        deploy.sh handles its own internal verify+probe rollback; the runner
        only reads its exit code. Path is exactly `scripts/deploy.sh` (the
        deploy-saga lesson)."""
        proc = self._run(["bash", "scripts/deploy.sh"])
        self._echo(proc)
        ok = proc.returncode == 0
        return StepResult("deploy", ok=ok, exit_code=proc.returncode,
                          detail="deploy.sh OK" if ok else f"deploy.sh exit {proc.returncode}")

    def run_verify(self, sha: str) -> StepResult:
        """`bash scripts/verify_deployed_image.sh <sha>` — runbook §4 assert
        that the live image SHA equals merged main HEAD."""
        proc = self._run(["bash", "scripts/verify_deployed_image.sh", sha])
        self._echo(proc)
        ok = proc.returncode == 0
        return StepResult("verify", ok=ok, exit_code=proc.returncode,
                          detail="verify OK" if ok else f"verify exit {proc.returncode}")

    def run_smoke(self, sha: str) -> StepResult:
        """WP-0 #3: smoke-as-code battery (runbook §6). Fire the S1-style probe
        battery at the live chat endpoint. FAIL-CLOSED — a transport error, an
        empty battery, or any broken invariant returns ok=False and triggers
        the same rollback path as verify/eval. verify (which gated
        /health.git_sha == sha) has already run, so the runtime identity is
        proven before we get here; the battery only asserts behaviour."""
        # Put the runner's own repo root on sys.path so `scripts.autonomy.smoke`
        # imports under BOTH `python -m scripts.autonomy.deploy_runner` and a
        # bare `python scripts/autonomy/deploy_runner.py` invocation.
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        try:
            from scripts.autonomy.smoke import run_smoke_battery
            report = run_smoke_battery(self.chat_base_url, expected_sha=sha)
        except Exception as e:  # noqa: BLE001 — import OR run failure is fail-closed
            return StepResult("smoke", ok=False,
                              detail=f"smoke battery error: {type(e).__name__}: {e}")
        self.log(f"[runner] {report.detail}")
        return StepResult("smoke", ok=report.ok, detail=report.detail)

    def run_eval_tripwire(self, sha: str) -> StepResult:
        """WP-0 #4: degradation tripwire (runbook §6). Fire the routing-accuracy
        eval at the live endpoint and trip on pass-rate < baseline−ε. FAIL-SAFE
        on the baseline side — an unstamped baseline never trips, so it can't
        false-rollback a healthy deploy — and FAIL-CLOSED on errors (any
        tripwire import/run error → ok=False → rollback)."""
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        try:
            from scripts.autonomy.eval_tripwire import run_eval_tripwire_battery
            report = run_eval_tripwire_battery(self.chat_base_url, sha=sha)
        except Exception as e:  # noqa: BLE001 — import OR run failure is fail-closed
            return StepResult("eval_tripwire", ok=False,
                              detail=f"eval-tripwire error: {type(e).__name__}: {e}")
        self.log(f"[runner] {report.detail}")
        return StepResult("eval_tripwire", ok=report.ok, detail=report.detail)

    def run_rollback(self, previous_sha: str) -> StepResult:
        """`bash scripts/deploy.sh --rollback <sha>` — redeploy the previous
        tagged image (deploy.sh re-writes .env, recreates compose services,
        re-verifies). Only ever called with a non-empty previous_sha."""
        proc = self._run(["bash", "scripts/deploy.sh", "--rollback", previous_sha])
        self._echo(proc)
        ok = proc.returncode == 0
        return StepResult("rollback", ok=ok, exit_code=proc.returncode,
                          detail=(f"rolled back to {previous_sha}" if ok
                                  else f"rollback to {previous_sha} exit {proc.returncode}"))

    # ------------------------------------------------------------- audit ----
    def _now(self) -> str:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _audit(self, event: str, *, target: Optional[str] = None,
               previous: Optional[str] = None, gate: Optional[str] = None,
               deploy_rc: Optional[int] = None, rollback_rc: Optional[int] = None,
               detail: str = "") -> str:
        """Build one human-readable audit line and hand it to `write_audit`.
        Field set covers the deploy side of runbook §7 (timestamp, event,
        target/previous SHA, which gate, deploy/rollback exit codes); the
        merge side (PR#, flipped pins, scope-fence, eval-delta) is written by
        the auto-merge machinery (#5)."""
        parts = [self._now(), event]
        if target is not None:
            parts.append(f"target={target or '-'}")
        if previous is not None:
            parts.append(f"previous={previous or '-'}")
        if gate:
            parts.append(f"gate={gate}")
        if deploy_rc is not None:
            parts.append(f"deploy_rc={deploy_rc}")
        if rollback_rc is not None:
            parts.append(f"rollback_rc={rollback_rc}")
        parts.append(f"runner={self.runner_id}")
        line = "  ".join(parts)
        if detail:
            line += f"  :: {detail}"
        self.write_audit(line)
        return line

    def write_audit(self, line: str) -> None:
        """=== WP-0 #5 SEAM: audit-log sink (runbook §7).

        Base impl appends one line to `AUTONOMY_LOG.md` (the file Stan reads).
        Append-only; written at runtime on the prod host, never hand-edited in
        a PR (the file is a DENY path in the scope-fence). #5 hardens this
        (structured fields, the merge-side record, kill-switch integration)."""
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deploy + verify + smoke runner with auto-rollback "
                    "(AUTONOMY_RUNBOOK_R-30 §4)")
    ap.add_argument("--repo", default=str(REPO_ROOT),
                    help="repo dir to deploy from (default: this checkout)")
    ap.add_argument("--runner-id", default="deploy_runner",
                    help="label written into each audit-log line")
    args = ap.parse_args(argv)
    runner = DeployRunner(repo_root=Path(args.repo), runner_id=args.runner_id)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
