#!/usr/bin/env python3
"""Autonomy control surface — kill-switch + audit-log (AUTONOMY_RUNBOOK_R-30 §7).
WP-0 deliverable #5.

This is the ONE place the two halves of Stan's "видимость и контроль" live, so
BOTH autonomous action paths share a single implementation instead of copying it
(the runbook §6 "fan-out скопирован по builder'ам" anti-pattern, R6):

  * the deploy runner   (scripts/autonomy/deploy_runner.py, §4) — auto-deploy;
  * the merge-step      (scripts/autonomy/merge_gate.py,   §3) — auto-merge.

Both import this module. Neither re-implements the kill-switch read or the
audit-line format, so a change to either is made once and is true for both —
there is no way for the deploy path and the merge path to drift.

KILL-SWITCH (runbook §7 — Stan's control, the freeze):
  `read_killswitch(repo_root)` is engaged when EITHER the env var
  `WC_AUTONOMY_KILLSWITCH` is set truthy OR the flag file `AUTONOMY_KILLSWITCH`
  exists at the repo root. Engaged → the agent falls back to "PR-only, no
  merge / no deploy" (the old NIGHT_RUN posture). Both signals are host-local
  (a process env var; an untracked on-host file) — neither is reachable from an
  agent PR, so an agent can engage the freeze (fail-safe) but can never lift it.
  One command (`export WC_AUTONOMY_KILLSWITCH=1` or `touch AUTONOMY_KILLSWITCH`)
  freezes every autonomous action.

AUDIT-LOG (runbook §7 — Stan's visibility, the trail):
  `format_audit_line` builds one machine- AND human-readable line; `append_audit`
  appends it to `AUTONOMY_LOG.md` (the file Stan reads). Append-only; written at
  runtime on the prod host, never hand-edited in a PR — `AUTONOMY_LOG.md` is a
  scope-fence DENY path. Each autonomous action writes exactly one line per
  terminal outcome. The deploy runner writes the DEPLOY side (target/previous
  SHA, gate, deploy/rollback exit codes = "результат деплоя/отката"); the
  merge-step writes the MERGE side (PR#, SHA, flipped pins, scope-fence verdict,
  eval-delta). The two lines share a SHA so Stan can correlate a merge with its
  deploy — together they are the full §7 record.

Line shape (fields are joined by exactly TWO spaces so a value never collides
with the separator; `detail` is free-form after the `::`):

    <ts>  <EVENT>  k1=v1  k2=v2  ...  runner=<id>  :: <detail>

Stdlib-only and self-contained, like its siblings deploy_runner / smoke /
eval_tripwire: it must import instantly under a bare prod-host interpreter and
carries no project dependency. Every value is passed in (repo_root, environ,
clock) so the whole surface is unit-tested with pure fakes, no env / no files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

# The audit trail Stan reads. Lives at repo root (NOT under the fenced autonomy
# package) and is itself a scope-fence DENY path — append-only at runtime.
AUDIT_LOG_NAME = "AUTONOMY_LOG.md"

# Kill-switch signals (runbook §7). Both host-local — unreachable from a PR.
KILLSWITCH_ENV = "WC_AUTONOMY_KILLSWITCH"
KILLSWITCH_FLAG_NAME = "AUTONOMY_KILLSWITCH"

# Env values that read as "not engaged" — an unset / explicitly-off var must NOT
# freeze the agents. Anything else truthy (1, true, yes, on, "freeze", …) engages.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


# ───────────────────────────────────────────────────────────────── kill-switch ──

@dataclass(frozen=True)
class KillSwitch:
    """The freeze verdict. `engaged=True` → agents drop to PR-only (no merge,
    no deploy). `source` says which signal fired ("env" | "file" | "clear") so
    the loud log + the audit line name the exact trigger."""
    engaged: bool
    reason: str
    source: str


def read_killswitch(
    repo_root: Path | str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    env_var: str = KILLSWITCH_ENV,
    flag_name: str = KILLSWITCH_FLAG_NAME,
) -> KillSwitch:
    """The single shared kill-switch read (runbook §7). Engaged when the env var
    is set truthy OR the flag file exists at `repo_root`. The env var is checked
    first so a host-wide freeze does not depend on any filesystem state. Pure:
    `environ` defaults to the live process env but is injectable for tests."""
    env_map = environ if environ is not None else os.environ
    raw = (env_map.get(env_var, "") or "").strip().lower()
    if raw not in _FALSEY:
        return KillSwitch(True, f"env {env_var} is set", "env")
    flag = Path(repo_root) / flag_name
    if flag.exists():
        return KillSwitch(True, f"flag file present: {flag_name}", "file")
    return KillSwitch(False, "clear", "clear")


# ──────────────────────────────────────────────────────────────────── audit-log ──

def now_utc() -> str:
    """UTC timestamp for an audit line. `%Y-%m-%dT%H:%M:%SZ` — second-precision,
    sortable, no separator-colliding double-space. The action paths inject a
    fixed clock in tests; this is the production default."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_path_for(repo_root: Path | str) -> Path:
    """The audit-log path for a checkout — `<repo_root>/AUTONOMY_LOG.md`."""
    return Path(repo_root) / AUDIT_LOG_NAME


def format_audit_line(
    timestamp: str,
    event: str,
    fields: Sequence[tuple[str, object]] = (),
    detail: str = "",
) -> str:
    """Render one audit line. Fields are emitted in the given order as `k=v`
    and joined to the `<ts>  <EVENT>` head by TWO spaces; a non-empty `detail`
    is appended after a `  :: ` sentinel. Callers render empty values as `-`
    (an absent field is unambiguous, never a bare `k=`). This is the ONLY
    formatter — the deploy line and the merge line are the same shape, so the
    format is stable across both paths by construction."""
    parts = [timestamp, event]
    parts.extend(f"{k}={v}" for k, v in fields)
    line = "  ".join(parts)
    if detail:
        line += f"  :: {detail}"
    return line


def append_audit(path: Path | str, line: str) -> None:
    """Append one line to the audit-log, creating the parent dir on demand.
    Append-only (`"a"`) — the trail is never rewritten, only grown. UTF-8 so
    Cyrillic detail text round-trips. The sole external effect in this module;
    the action paths override it in tests to capture lines in memory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
