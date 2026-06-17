#!/usr/bin/env python3
"""Auto-merge step — the merge-side control surface (AUTONOMY_RUNBOOK_R-30 §3 +
§7). WP-0 deliverable #5 (merge half).

Runbook §3 lets an agent auto-merge its own PR once the merge-eligibility gate is
green. This module is the step that performs that merge — and, per §7, it is the
SECOND consumer (with the deploy runner) of the shared kill-switch + audit-log:

  * BEFORE merging it reads the SAME kill-switch the deploy runner reads
    (`control.read_killswitch`). Engaged → it refuses and falls back to PR-only
    (no merge), writing a loud line + a MERGE_KILLSWITCH_REFUSE audit record.
    One freeze stops BOTH paths.
  * On every terminal outcome it writes ONE audit line to `AUTONOMY_LOG.md` via
    the SAME formatter + sink the deploy runner uses (`control.format_audit_line`
    / `control.append_audit`). The merge line carries the §7 MERGE fields — PR#,
    SHA, flipped characterization pins, scope-fence verdict, eval-delta — and
    shares its SHA with the deploy runner's line for that same main, so Stan can
    read the merge and its deploy as one story.

What this deliberately does NOT do (flagged R-30 WP-0 follow-up, NOT this PR):
the actual `gh pr merge` call needs GitHub *write* rights on the prod host (SOW)
— there is no GitHub write connector yet. So `do_merge()` is an overridable SEAM
(it raises until wired), exactly as `deploy_runner` shipped `run_smoke` /
`run_eval_tripwire` as seams that #3 / #4 later filled. The §3 eligibility
verdict itself (CI green, scope-fence clean, pins, eval, self-review) is computed
by that future caller and handed in as a `MergeContext` to be recorded — this
module owns the kill-switch gate + the audit record, the parts #5 is responsible
for. Wiring `do_merge` flips the whole step live without touching the control
surface.

Stdlib-only and self-contained like its siblings; every external effect
(kill-switch read, the merge call, the audit write) is overridable so the step
is unit-tested with pure fakes — no `gh`, no network, no files.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

# scripts/autonomy/merge_gate.py → parents[2] == repo root. Same sys.path
# bootstrap the runner uses, so `control` imports under `-m` AND a bare run.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.autonomy import control  # noqa: E402  (after the sys.path bootstrap)

# --- exit codes ------------------------------------------------------------
# Distinct integers so Stan can read an outcome without parsing prose. 24 is the
# SAME freeze code the deploy runner returns (one kill-switch, one code); 30+ is
# the merge band, offset from the runner's 20-25 deploy band so a merge code is
# never mistaken for a deploy code.
EXIT_OK = 0                # merged (or, in tests, the merge seam returned ok)
EXIT_KILLSWITCH = 24       # kill-switch engaged → refused to merge (PR-only)
EXIT_MERGE_FAILED = 30     # the merge call itself failed


@dataclass
class MergeContext:
    """The §3 inputs recorded for one PR's merge decision. The future auto-merge
    caller computes the eligibility verdict (CI / scope-fence / pins / eval) and
    passes the results here to be audited — this module records, it does not
    re-evaluate. Every field lands in the §7 MERGE audit line."""
    pr: Union[int, str]                                  # the PR number
    sha: str = ""                                        # merged-main SHA (ties to the deploy line)
    pins_flipped: list = field(default_factory=list)     # characterization pins flipped green in this PR
    scope_fence: str = "?"                               # §3.2 verdict: "clean" | "red" | …
    eval_delta: str = "?"                                # §3.4 eval-tripwire delta vs baseline, e.g. "+0%" / "-3%"


@dataclass
class MergeResult:
    """Outcome of the `do_merge` seam. `ok=True` == the PR is merged."""
    ok: bool
    detail: str = ""


class MergeStep:
    """Runbook-§3 auto-merge step. `run(ctx)` checks the kill-switch, performs
    the merge through the `do_merge` seam, and records exactly one audit line.

    Every external effect is an overridable method (kill-switch read, the merge
    call, the audit write), so `run()`'s decision logic is exercised with pure
    fakes. Production wires `do_merge` to `gh pr merge` (the follow-up)."""

    def __init__(
        self,
        *,
        repo_root: Path | str = REPO_ROOT,
        runner_id: str = "merge_step",
        audit_path: Path | str | None = None,
        clock: Optional[Callable[[], str]] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.runner_id = runner_id
        self.audit_path = (Path(audit_path) if audit_path
                           else control.audit_path_for(self.repo_root))
        self._clock = clock
        self.log = log if log is not None else print

    # ----------------------------------------------------------------- run --
    def run(self, ctx: MergeContext) -> int:
        """Execute the merge step once. Returns an EXIT_* code and writes
        exactly one audit line per terminal outcome. Never retries."""
        # 0. Kill-switch (runbook §7) — checked BEFORE the merge, the same gate
        #    the deploy runner applies before a deploy. Engaged → PR-only.
        ks = self.check_killswitch()
        if ks.engaged:
            self._audit("MERGE_KILLSWITCH_REFUSE", ctx, detail=ks.reason)
            self.log(f"[merge] kill-switch engaged ({ks.source}) — refusing to merge "
                     f"PR {ctx.pr}; PR-only mode: {ks.reason}")
            return EXIT_KILLSWITCH

        # 1. Merge via the seam. The follow-up wires `gh pr merge`; until then a
        #    live call raises (fail-loud), never a silent half-merge.
        result = self.do_merge(ctx)
        if result.ok:
            self._audit("MERGE_OK", ctx, detail=result.detail or "auto-merged")
            self.log(f"[merge] PR {ctx.pr} merged → main {ctx.sha or '-'}")
            return EXIT_OK

        self._audit("MERGE_FAILED", ctx, detail=result.detail or "merge call failed")
        self.log(f"[merge] PR {ctx.pr} merge FAILED — not retrying: {result.detail}")
        return EXIT_MERGE_FAILED

    # =====================================================================
    # External-effect methods — overridden wholesale in tests.
    # =====================================================================

    def check_killswitch(self) -> control.KillSwitch:
        """Kill-switch read (runbook §7) — the SHARED reader, identical to the
        deploy runner's. Engaged → the step refuses to merge."""
        return control.read_killswitch(self.repo_root)

    def do_merge(self, ctx: MergeContext) -> MergeResult:
        """=== SEAM: the actual merge. The R-30 WP-0 follow-up wires this to
        `gh pr merge <pr> --merge` (needs GitHub write rights on SOW). Unwired,
        it raises — a live merge step with no merge mechanism is a loud bug, not
        a silent no-op. Tests and the future caller override this."""
        raise NotImplementedError(
            "auto-merge mechanism not wired — R-30 WP-0 follow-up: `gh pr merge` "
            "needs GitHub write rights on SOW. Override do_merge() to enable.")

    # ------------------------------------------------------------- audit ----
    def _now(self) -> str:
        if self._clock is not None:
            return self._clock()
        return control.now_utc()

    def _audit(self, event: str, ctx: MergeContext, *, detail: str = "") -> str:
        """Build one MERGE-side audit line (runbook §7) via the shared
        `control.format_audit_line` and hand it to `write_audit`. Field set:
        PR#, SHA, flipped pins, scope-fence verdict, eval-delta — the merge half
        of §7. Empty values render as `-`; `pins` is a comma-joined name list
        (no spaces — the field separator is two spaces). Same `sha` as the
        deploy line for this main, so the two records correlate."""
        pins = ",".join(str(p) for p in (ctx.pins_flipped or [])) or "-"
        fields: list[tuple[str, object]] = [
            ("pr", ctx.pr if ctx.pr not in (None, "") else "-"),
            ("sha", ctx.sha or "-"),
            ("pins", pins),
            ("scope_fence", ctx.scope_fence or "-"),
            ("eval_delta", ctx.eval_delta or "-"),
            ("runner", self.runner_id),
        ]
        line = control.format_audit_line(self._now(), event, fields, detail)
        self.write_audit(line)
        return line

    def write_audit(self, line: str) -> None:
        """Audit-log sink (runbook §7) — appends to `AUTONOMY_LOG.md` via the
        shared `control.append_audit`. Tests override this to capture in memory."""
        control.append_audit(self.audit_path, line)


class GhMergeStep(MergeStep):
    """Production `MergeStep` — the auto-merge mechanism wired (R-30 Phase B B2).

    Fills the `do_merge` seam the base class left open with `gh pr merge <pr>
    --squash --auto`, the way #3 / #4 filled the deploy runner's smoke /
    eval-tripwire seams. Everything ELSE — the kill-switch gate, the single §7
    audit line, the never-retry contract — is inherited from `MergeStep.run()`
    unchanged, so one freeze still stops both the merge and the deploy path and
    the audit format stays identical across both.

    `--squash` is the locked merge method (one commit per PR, in step with
    version-bump-per-PR). `--auto` is the server-side backstop: GitHub performs
    the merge only once the required status checks go green, so the token can
    never bypass the tests even if it leaks (Phase B B1 risk radius).

    Because `--auto` is asynchronous, ``ok=True`` means **auto-merge was
    ENABLED**, not that the PR is already merged — GitHub merges later when the
    checks pass. The post-merge deploy is therefore triggered SEPARATELY off the
    real main-HEAD advance, never off this return value (Phase B B2 STEP D).

    Requires, on the prod host (SOW): `gh` authenticated with a token that has
    Pull-requests:write (B1) AND "Allow auto-merge" enabled on the repo (B1).
    Without either, `gh` exits non-zero → `MergeResult(ok=False)` → `run()`
    records MERGE_FAILED (loud, correct), no silent half-merge.
    """

    def do_merge(self, ctx: MergeContext) -> MergeResult:
        import subprocess
        proc = subprocess.run(
            ["gh", "pr", "merge", str(ctx.pr), "--squash", "--auto"],
            cwd=str(self.repo_root), text=True, capture_output=True,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            return MergeResult(ok=True, detail=out or f"auto-merge enabled #{ctx.pr}")
        return MergeResult(ok=False, detail=f"gh exit {proc.returncode}: {err or out}")
