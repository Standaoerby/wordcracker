"""W-18 — mandatory version-bump gate (CI / pre-deploy).

This is the FIRST gate of the W-18 deploy pipeline. It fires BEFORE the
12-probe runner so that "deploy succeeded but the version label didn't
move" (the failure mode observed 2026-05-24) is caught at CI time —
without needing a reachable prod endpoint.

It answers one question: did `ANALYTICS_VERSION` in
`scripts/v2/__version__.py` change vs the prior reference?

Three reference modes:

- `--against git` (default) — compare HEAD against the parent commit
  (`HEAD~1`). For PR builds where CI provides BASE_SHA, prefer
  `--git-ref <BASE_SHA>`. Use this in CI.
- `--against baseline` — compare against the version recorded in
  `scripts/predeploy_baseline.json` (whatever the last clean prod run
  pinned). Use this in pre-deploy on the deploy host.
- `--against file <path>` — compare against an arbitrary
  `__version__.py`-shaped file (escape hatch for local debugging).

Exit codes — same contract as `predeploy_probe_suite.py` so the deploy
wrapper can `set -e` over both:

    0 — version was bumped (deploy may proceed)
    1 — script-level error (missing file, git unavailable, bad ref)
    3 — version did NOT bump (deploy BLOCKED) — same code as the probe
        runner's version-bump exit, so a single wrapper can map 3 →
        "bump the version label" without case analysis on which tool
        produced it.

The script is stdlib-only and does not import the project — it can run
on a CI box that has no PYTHONPATH set up yet.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE_REL = "scripts/v2/__version__.py"
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "predeploy_baseline.json"
VERSION_RE = re.compile(r"ANALYTICS_VERSION\s*=\s*['\"]([^'\"]+)['\"]")


def parse_version_text(text: str) -> str | None:
    """Pull ANALYTICS_VERSION out of a __version__.py-shaped string."""
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def read_current_version() -> str:
    p = REPO_ROOT / VERSION_FILE_REL
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        die(1, f"cannot read {p}: {e}")
    v = parse_version_text(text)
    if not v:
        die(1, f"ANALYTICS_VERSION not found in {p}")
    return v


def read_version_from_git(ref: str) -> str | None:
    """Run `git show <ref>:scripts/v2/__version__.py` and parse. Returns
    None if the file did not exist at that ref (new repo, brand-new
    file) so callers can treat that as "any non-empty current version
    is a bump"."""
    try:
        out = subprocess.check_output(
            ["git", "show", f"{ref}:{VERSION_FILE_REL}"],
            cwd=REPO_ROOT,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        die(1, "git not on PATH — required for --against git")
    except subprocess.CalledProcessError as e:
        # `git show` exits non-zero if the file does not exist at the
        # ref. Detect that vs. a real git error.
        msg = e.stderr.decode("utf-8", "ignore").lower()
        if "exists on disk, but not in" in msg or "does not exist" in msg or "bad revision" in msg:
            # If the ref itself is bad, fail loudly.
            if "bad revision" in msg or "unknown revision" in msg:
                die(1, f"git ref {ref!r} not resolvable: {e.stderr.decode('utf-8', 'ignore').strip()}")
            return None
        die(1, f"git show {ref}:{VERSION_FILE_REL} failed: {e.stderr.decode('utf-8', 'ignore').strip()}")
    return parse_version_text(out.decode("utf-8", "ignore"))


def read_version_from_baseline(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(1, f"baseline at {path} is not valid JSON: {e}")
    v = data.get("version")
    if not v or v == "unknown":
        return None
    return v


def read_version_from_file(path: Path) -> str | None:
    if not path.exists():
        die(1, f"--against file: {path} does not exist")
    return parse_version_text(path.read_text(encoding="utf-8"))


def die(code: int, msg: str) -> None:
    print(f"[version-bump] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="W-18: version-bump gate")
    ap.add_argument("--against", choices=("git", "baseline", "file"), default="git",
                    help="reference to compare against (default: git HEAD~1)")
    ap.add_argument("--git-ref", default=None,
                    help="git ref to compare against when --against git "
                         "(default: HEAD~1; in CI set this to the PR base SHA)")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help=f"baseline path for --against baseline (default: {DEFAULT_BASELINE})")
    ap.add_argument("--file", default=None,
                    help="path to an alternative __version__.py for --against file")
    ap.add_argument("--require-strict-increase", action="store_true",
                    help="also fail if the new version compares <= old by tuple order. "
                         "Off by default — the W-18 contract is 'must differ', not 'must be greater', "
                         "because rollbacks legitimately decrease the label.")
    args = ap.parse_args(argv)

    current = read_current_version()

    if args.against == "git":
        ref = args.git_ref or os.environ.get("BASE_SHA") or "HEAD~1"
        prior = read_version_from_git(ref)
        ref_label = f"git {ref}"
    elif args.against == "baseline":
        baseline_path = Path(args.baseline)
        prior = read_version_from_baseline(baseline_path)
        ref_label = f"baseline {baseline_path.name}"
    elif args.against == "file":
        if not args.file:
            die(1, "--against file requires --file <path>")
        prior = read_version_from_file(Path(args.file))
        ref_label = f"file {args.file}"
    else:  # pragma: no cover — argparse guards
        die(1, f"unknown --against: {args.against!r}")

    if prior is None:
        # No prior reference (new repo / new file / no baseline yet).
        # Treat as bumped — first deploy of a fresh setup is allowed.
        print(f"[version-bump] no prior version at {ref_label!r} — treating "
              f"current {current!r} as bumped (first run / new setup)", flush=True)
        return 0

    if prior == current:
        die(3, f"ANALYTICS_VERSION did NOT bump: {ref_label} == current == {current!r}. "
               f"Edit {VERSION_FILE_REL} before deploying.")

    if args.require_strict_increase and not _tuple_gt(current, prior):
        die(3, f"ANALYTICS_VERSION did not strictly increase: {ref_label}={prior!r} >= current={current!r} "
               f"(--require-strict-increase set; remove this flag to allow rollbacks).")

    print(f"[version-bump] OK: {ref_label}={prior!r} -> current={current!r}", flush=True)
    return 0


def _tuple_gt(a: str, b: str) -> bool:
    """Best-effort dotted-numeric compare: '2.6.13' > '2.6.2' == True.

    Non-numeric segments (`-alpha1`) sort lexicographically AFTER numbers
    of the same prefix. Good enough for W-18 — the W-18 contract is
    'must differ', this is only a soft guard behind --require-strict-increase.
    """
    def _key(v: str) -> tuple:
        parts = re.split(r"[.\-+]", v.strip())
        out = []
        for p in parts:
            try:
                out.append((0, int(p)))
            except ValueError:
                out.append((1, p))
        return tuple(out)
    return _key(a) > _key(b)


if __name__ == "__main__":
    sys.exit(main())
