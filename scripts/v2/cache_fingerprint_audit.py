#!/usr/bin/env python3
"""S-F1 / ADR-F1 / D67 — cache-fingerprint audit gate.

Verifies that the AST fingerprint for each registered tool MOVES when
the tool's wrapper source — or any of its declared depth=1 callees —
changes between `HEAD` and the ref passed as `--since`. Catches the
failure class where a refactor edits a helper but the depth=1 walk
silently doesn't pick it up (gap in `_depth1_callees`), which would
re-open the E18 stale-cache class without a visible signal.

Run as a CI step in
[predeploy.yml](.github/workflows/predeploy.yml), and locally via:

    python -m scripts.v2.cache_fingerprint_audit --since=HEAD~1

Exit codes:
    0 — no mismatches (or no scripts/ files changed in the range)
    1 — at least one tool has a stale fingerprint
    2 — bad invocation / git error / import error
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile

log = logging.getLogger("wordcracker.v2.cache_fingerprint_audit")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# Snippet executed in a worktree-at-ref. Has to be self-contained
# (no shared module-level state with the auditor) and stable across
# versions of the registry — older versions (pre-S-F1) lack the
# `_depth1_callees` walk, which is fine: we just want whatever
# fingerprint the registry at that ref produces. The fp's stay-vs-move
# comparison happens here, not in the worktree.
_AUDIT_SNIPPET = r"""
import json, sys, os
sys.path.insert(0, ".")
out = {}
try:
    # tools/__init__.py registers every @tool decorator on import.
    import scripts.v2.tools  # noqa: F401
    from scripts.v2.tool_registry import REGISTRY
    from scripts.v2.contracts.registry import wrapper_fingerprint_for_tool
    for tool in sorted(REGISTRY):
        try:
            out[tool] = wrapper_fingerprint_for_tool(tool) or ""
        except Exception:
            out[tool] = ""
except Exception as e:
    sys.stderr.write(f"audit-snippet import error: {e!r}\n")
    sys.exit(2)
sys.stdout.write(json.dumps(out))
"""


def _git(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper for git invocations, always rooted at REPO_ROOT."""
    return subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=kwargs.pop("cwd", REPO_ROOT),
        **kwargs,
    )


def _changed_files(since: str) -> list[pathlib.Path]:
    """Files changed between `since` and HEAD (absolute paths)."""
    r = _git(["diff", "--name-only", since, "HEAD"])
    if r.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {r.stderr.strip()}")
    paths: list[pathlib.Path] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = (REPO_ROOT / line).resolve()
        paths.append(p)
    return paths


def _fingerprints_at_head() -> dict[str, str]:
    """Collect (tool → fp) in-process at HEAD.

    Same logic as the snippet, just avoids a redundant subprocess for
    the local-state side of the comparison.
    """
    import scripts.v2.tools  # noqa: F401 — triggers @tool registrations
    from scripts.v2.tool_registry import REGISTRY
    from scripts.v2.contracts.registry import wrapper_fingerprint_for_tool

    out: dict[str, str] = {}
    for tool in sorted(REGISTRY):
        try:
            out[tool] = wrapper_fingerprint_for_tool(tool) or ""
        except Exception:
            out[tool] = ""
    return out


def _fingerprints_at_ref(ref: str) -> dict[str, str]:
    """Collect (tool → fp) in a `git worktree add` of `ref`.

    The worktree is removed after the subprocess completes (or on
    error). Honors the deployed Python env — caller is responsible for
    ensuring `python` resolves to the same interpreter the prod image
    uses (CI does this via ADR-B7's full-lock install).
    """
    with tempfile.TemporaryDirectory(prefix="wc-fp-audit-") as tmp:
        worktree = pathlib.Path(tmp) / "wt"
        r = _git(["worktree", "add", "--detach", str(worktree), ref])
        if r.returncode != 0:
            raise RuntimeError(
                f"git worktree add {ref} failed: {r.stderr.strip()}",
            )
        try:
            env = {**os.environ, "PYTHONPATH": str(worktree)}
            # The audit subprocess MUST observe the kill-switch as on
            # (default). If the caller's env has WC_CACHE_AST_INVALIDATION=off,
            # the snippet's fp lookup still runs (it's not behind the
            # kill-switch — only `cache.cache_key` is), so we leave env
            # alone. Pin-mode also has no effect on the snippet because
            # it only matters for `cache_key` path.
            r = subprocess.run(
                [sys.executable, "-c", _AUDIT_SNIPPET],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"audit subprocess at {ref} failed: {r.stderr.strip()}",
                )
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"audit subprocess produced invalid JSON: {e}",
                )
        finally:
            cleanup = _git(["worktree", "remove", "--force", str(worktree)])
            if cleanup.returncode != 0:
                log.warning(
                    "worktree cleanup at %s reported: %s",
                    worktree, cleanup.stderr.strip(),
                )


def _tool_source_files() -> dict[str, set[pathlib.Path]]:
    """Map each registered tool to the resolved source files that
    contribute to its fingerprint (wrapper file + v1 file + each
    depth-1 callee's file). Used to decide whether a tool's source
    actually changed in the diff range.
    """
    import scripts.v2.tools  # noqa: F401
    from scripts.v2.tool_registry import REGISTRY
    from scripts.v2.contracts.registry import (
        V1_CONTRACTS,
        _depth1_callees,
    )

    def _file_of(fn) -> pathlib.Path | None:
        try:
            sf = inspect.getsourcefile(fn)
        except (TypeError, OSError):
            return None
        if not sf:
            return None
        return pathlib.Path(sf).resolve()

    out: dict[str, set[pathlib.Path]] = {}
    for tool, spec in REGISTRY.items():
        files: set[pathlib.Path] = set()
        target = spec.fn
        target_inner = getattr(target, "__wrapped__", target)

        wrapper_inner = target_inner
        v1_fn = None
        for binding in V1_CONTRACTS.values():
            candidate = binding.wrapper_fn
            candidate_inner = getattr(candidate, "__wrapped__", candidate)
            if candidate is target or candidate_inner is target_inner:
                wrapper_inner = candidate_inner
                try:
                    v1_fn = binding.resolved_v1_fn()
                except Exception:
                    v1_fn = None
                break

        for fn in (wrapper_inner, v1_fn):
            if fn is None:
                continue
            f = _file_of(fn)
            if f is not None:
                files.add(f)
            for cal in _depth1_callees(fn):
                cf = _file_of(cal)
                if cf is not None:
                    files.add(cf)

        out[tool] = files
    return out


def audit(since: str) -> tuple[int, str]:
    """Returns (exit_code, message).

    Exit 0 with a skip message if no scripts/ files changed in the
    range. Exit 1 with a stale-fingerprint report. Exit 2 on any
    git/subprocess error.
    """
    try:
        changed = _changed_files(since)
    except RuntimeError as e:
        return 2, f"audit: {e}"

    scripts_changed = {p for p in changed
                       if str(p).startswith(str((REPO_ROOT / "scripts").resolve()))}
    if not scripts_changed:
        return 0, f"audit: no scripts/ files changed since {since}; skipping fingerprint diff"

    try:
        fp_head = _fingerprints_at_head()
    except Exception as e:
        return 2, f"audit: in-process fingerprint collection failed: {e!r}"

    try:
        fp_since = _fingerprints_at_ref(since)
    except RuntimeError as e:
        return 2, f"audit: {e}"

    src_map = _tool_source_files()

    mismatches: list[tuple[str, str, list[pathlib.Path]]] = []
    for tool, head_fp in fp_head.items():
        if head_fp != fp_since.get(tool, ""):
            continue  # fp moved — the tool is fine
        tool_files = src_map.get(tool, set())
        tool_changed = sorted(f for f in tool_files if f in scripts_changed)
        if tool_changed:
            mismatches.append((tool, head_fp, tool_changed))

    if mismatches:
        lines = [
            f"audit: {len(mismatches)} tool(s) have stale fingerprints "
            f"between {since} and HEAD — source files changed but the "
            f"fingerprint did not move (walk gap):"
        ]
        for tool, fp, files in mismatches:
            rel = [str(f.relative_to(REPO_ROOT)) for f in files]
            lines.append(f"  {tool} (fp={fp or '<empty>'}): {rel}")
        lines.append(
            "Fix: either extend `_depth1_callees` to discover the "
            "edited symbol, or bump the affected wrapper_version "
            "manually as a stop-gap. See docs/v2/decisions.md → "
            "2026-05-25 S-F1."
        )
        return 1, "\n".join(lines)

    return (
        0,
        f"audit: all {len(fp_head)} tool fingerprints reflect source "
        f"changes since {since}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", default="HEAD~1",
        help="Git ref to compare HEAD against (default: HEAD~1)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    code, msg = audit(args.since)
    stream = sys.stdout if code == 0 else sys.stderr
    print(msg, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())
