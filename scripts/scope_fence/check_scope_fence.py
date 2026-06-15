#!/usr/bin/env python3
"""Scope-fence — the cornerstone autonomy gate (AUTONOMY_RUNBOOK_R-30 §5).

A PR diff that reaches the 🔴 (irreversible) zone must NOT become
merge-eligible. This script is the mechanical enforcement of the runbook's
"max autonomy over everything REVERSIBLE": it keeps every agent change
inside the reversible zone by hard-failing CI whenever the diff touches a
path — or introduces an operation — for which no rollback exists.

Two layers, both driven entirely by `denylist.txt` (Stan tunes the data,
not the engine):

  1. PATH denylist — any changed path (added / modified / deleted /
     renamed) matching a DENY glob, and not rescued by an ALLOW glob, is a
     violation. Covers deploy-config (compose, Dockerfile, infra/ nginx +
     cloudflared, systemd), CI (.github/**), secrets (.env*), the
     deploy/ops scripts, destructive/DB/index scripts, the data/index
     trees, and the autonomy machinery itself (self-protection).

  2. CONTENT denylist — any ADDED diff line matching a CONTENT regex is a
     violation. Catches a destructive op smuggled into an otherwise-allowed
     file: force-push, history rewrite, data/index/secret deletion, DB
     drops, chroma resets, branch-protection writes. A file that is itself
     path-denied (or SCANEXEMPT) is skipped here — its path violation is
     already reported and its body legitimately contains such strings.

The denylist lives next to this engine (`scripts/scope_fence/denylist.txt`)
and is itself a DENY path: agents cannot widen their own fence (runbook §5).

Stdlib-only, no project import — runs on a bare CI box with no PYTHONPATH
(same property as scripts/check_version_bump.py).

Exit codes (a deploy/merge wrapper can `set -e` over this):
    0 — clean: diff touches no protected path / destructive marker
    1 — script-level error (git unavailable, bad ref, missing/!parsable
        denylist)
    2 — VIOLATION: diff reached a 🔴 path or destructive marker → the PR is
        not merge-eligible. The change belongs to Stan's 🔴-keys (runbook §1).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

# scripts/scope_fence/check_scope_fence.py → parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DENYLIST = Path(__file__).resolve().parent / "denylist.txt"

# Exit code for "the fence tripped". Distinct from check_version_bump's 3
# (version gate) and from 1 (script error) so a wrapper can tell them apart.
EXIT_VIOLATION = 2

Violation = namedtuple("Violation", "kind subject detail")


# ---------------------------------------------------------------------------
# Glob → regex (security-critical: explicit and tested, not clever)
# ---------------------------------------------------------------------------

def glob_to_regex(glob: str) -> "re.Pattern[str]":
    """Translate a denylist glob into a full-path-anchored regex.

    Semantics on POSIX (`/`) repo-relative paths:
      `**` — any number of path segments, including the `/` separators
      `*`  — any run of chars WITHIN a single segment (never `/`)
      `?`  — exactly one char (never `/`)
    Everything else is matched literally (`re.escape`).
    """
    g = glob.strip().replace("\\", "/")
    out = ["^"]
    i, n = 0, len(g)
    while i < n:
        c = g[i]
        if c == "*":
            if i + 1 < n and g[i + 1] == "*":   # `**`
                out.append(".*")
                i += 2
            else:                                # `*`
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


# ---------------------------------------------------------------------------
# Denylist model + parser
# ---------------------------------------------------------------------------

class Denylist:
    def __init__(self) -> None:
        self.deny: list[tuple[str, "re.Pattern[str]"]] = []
        self.allow: list[tuple[str, "re.Pattern[str]"]] = []
        self.scan_exempt: list[tuple[str, "re.Pattern[str]"]] = []
        self.content: list[tuple[str, "re.Pattern[str]"]] = []
        self.source: str = "<denylist>"


def parse_denylist(text: str, source: str = "<denylist>") -> tuple[Denylist, list[str]]:
    """Parse the directive grammar. Returns (denylist, errors). A non-empty
    errors list means the caller must treat the denylist as unusable (a
    silently-misparsed fence is worse than no fence)."""
    dl = Denylist()
    dl.source = source
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"{source}:{lineno}: directive with no value: {raw!r}")
            continue
        directive, value = parts[0].upper(), parts[1].strip()
        if directive == "DENY":
            dl.deny.append((value, glob_to_regex(value)))
        elif directive == "ALLOW":
            dl.allow.append((value, glob_to_regex(value)))
        elif directive == "SCANEXEMPT":
            dl.scan_exempt.append((value, glob_to_regex(value)))
        elif directive == "CONTENT":
            try:
                dl.content.append((value, re.compile(value)))
            except re.error as e:
                errors.append(f"{source}:{lineno}: bad CONTENT regex {value!r}: {e}")
        else:
            errors.append(f"{source}:{lineno}: unknown directive {directive!r}")
    return dl, errors


def load_denylist(path: Path) -> Denylist:
    """Read + parse a denylist file, exiting 1 on any error (a fence that
    can't read its own rules must fail loud, never open)."""
    if not path.exists():
        die(1, f"denylist not found: {path}")
    dl, errors = parse_denylist(path.read_text(encoding="utf-8"), source=str(path))
    if errors:
        for e in errors:
            print(f"[scope-fence] denylist error: {e}", file=sys.stderr)
        die(1, f"{len(errors)} denylist parse error(s) in {path}")
    if not dl.deny and not dl.content:
        die(1, f"denylist {path} has no DENY/CONTENT rules — refusing to run an empty fence")
    return dl


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    # Strip a leading "./" PREFIX only — NOT via lstrip("./"), which would
    # treat "./" as a char set and eat the leading dot of dotfiles
    # (".github" → "github"), silently disabling the most important DENY
    # entries (CI, .env). git diff already emits repo-relative POSIX paths.
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _first_match(path: str, rules) -> str | None:
    p = _norm(path)
    for glob, rx in rules:
        if rx.match(p):
            return glob
    return None


def path_denied(path: str, dl: Denylist) -> str | None:
    """Return the matching DENY glob if `path` is denied and not rescued by
    an ALLOW, else None."""
    deny = _first_match(path, dl.deny)
    if deny is None:
        return None
    if _first_match(path, dl.allow) is not None:
        return None
    return deny


def _content_exempt(path: str, dl: Denylist) -> bool:
    """A file is content-scan-exempt if it is explicitly SCANEXEMPT, or if it
    is already path-denied (the path violation is reported separately, and
    its body — e.g. deploy.sh, this denylist — legitimately holds the very
    strings the CONTENT markers look for)."""
    if _first_match(path, dl.scan_exempt) is not None:
        return True
    if path_denied(path, dl) is not None:
        return True
    return False


def scan_paths(changed_paths, dl: Denylist) -> list[Violation]:
    out = []
    for p in changed_paths:
        if not p.strip():
            continue
        g = path_denied(p, dl)
        if g:
            out.append(Violation("path", _norm(p), f"matches DENY {g}"))
    return out


def added_lines_by_file(diff_text: str) -> dict[str, list[str]]:
    """Parse a unified diff into {target_path: [added line bodies]}.

    Tracks the current target file from `+++ b/<path>` headers. Deletions
    (`+++ /dev/null`) contribute no added lines. Added lines start with a
    single `+` (the `+++` header is excluded)."""
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            current = None
        elif line.startswith("+++ "):
            tgt = line[4:].strip()
            if tgt == "/dev/null":
                current = None
            else:
                current = tgt[2:] if tgt.startswith(("b/", "a/")) else tgt
                files.setdefault(current, [])
        elif line.startswith("+") and not line.startswith("+++"):
            if current is not None:
                files[current].append(line[1:])
    return files


def scan_content(diff_text: str, dl: Denylist) -> list[Violation]:
    out = []
    for path, lines in added_lines_by_file(diff_text).items():
        if _content_exempt(path, dl):
            continue
        for body in lines:
            for raw, rx in dl.content:
                if rx.search(body):
                    snippet = body.strip()
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    out.append(Violation("content", _norm(path),
                                         f"added line matches CONTENT /{raw}/  ›  {snippet}"))
                    break  # one violation per added line is enough
    return out


def check(changed_paths, diff_text: str, dl: Denylist) -> list[Violation]:
    return scan_paths(changed_paths, dl) + scan_content(diff_text, dl)


# ---------------------------------------------------------------------------
# git plumbing (only used by the CLI; unit tests drive the pure fns directly)
# ---------------------------------------------------------------------------

def die(code: int, msg: str) -> None:
    print(f"[scope-fence] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def _git(args: list[str], cwd: str) -> str:
    try:
        out = subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE)
    except FileNotFoundError:
        die(1, "git not on PATH")
    except subprocess.CalledProcessError as e:
        die(1, f"git {' '.join(args)} failed: {e.stderr.decode('utf-8', 'ignore').strip()}")
    return out.decode("utf-8", "ignore")


def resolve_merge_base(base_ref: str, cwd: str) -> str:
    """`git merge-base HEAD <base_ref>` — the STABLE fork point, immune to
    the base advancing (the R-25 lesson, same as check_version_bump). Empty
    result (unrelated histories) is a hard error, never a silent tip compare."""
    base = _git(["merge-base", "HEAD", base_ref], cwd).strip()
    if not base:
        die(1, f"empty merge-base for HEAD..{base_ref} — no common ancestor "
               f"(is {base_ref!r} fetched?)")
    return base


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scope-fence — 🔴 reversibility gate (runbook §5)")
    ap.add_argument("--base-ref", default="origin/main",
                    help="branch whose merge-base with HEAD bounds the PR diff "
                         "(default origin/main; pull_request CI).")
    ap.add_argument("--diff-range", default=None,
                    help="explicit 'A B' or 'A..B' range; overrides merge-base(base-ref). "
                         "Used for push events / debugging.")
    ap.add_argument("--denylist", default=str(DEFAULT_DENYLIST),
                    help=f"denylist file (default: {DEFAULT_DENYLIST})")
    ap.add_argument("--repo", default=str(REPO_ROOT),
                    help="repo dir for git ops (default: this checkout)")
    ap.add_argument("--changed-path", action="append", default=None,
                    help="explicit changed path; repeatable. Bypasses git (testing).")
    ap.add_argument("--diff-file", default=None,
                    help="read a unified diff from this file instead of git (testing).")
    args = ap.parse_args(argv)

    dl = load_denylist(Path(args.denylist))

    # Source of the changed-paths + diff: explicit (tests) or git (CI).
    if args.changed_path is not None or args.diff_file is not None:
        changed_paths = list(args.changed_path or [])
        diff_text = Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else ""
    else:
        cwd = args.repo
        if args.diff_range:
            rng = args.diff_range.replace("..", " ").split()
        else:
            rng = [resolve_merge_base(args.base_ref, cwd), "HEAD"]
        changed_paths = [p for p in _git(["diff", "--name-only", *rng], cwd).splitlines() if p.strip()]
        diff_text = _git(["diff", "--unified=0", *rng], cwd)

    violations = check(changed_paths, diff_text, dl)

    if violations:
        print("[scope-fence] VIOLATION — this PR reaches the 🔴 (irreversible) zone:\n",
              file=sys.stderr)
        for v in violations:
            print(f"  {v.kind.upper():7} {v.subject}\n          {v.detail}", file=sys.stderr)
        print("\n  These paths/operations have no rollback — they are Stan's 🔴-keys\n"
              "  (AUTONOMY_RUNBOOK §1). The scope-fence (§5) keeps autonomy inside the\n"
              "  reversible zone. Drop the 🔴 changes from this PR; a genuinely required\n"
              "  🔴 change goes through Stan by hand (WP-0 bootstrap exception aside).",
              file=sys.stderr)
        return EXIT_VIOLATION

    n = len([p for p in changed_paths if p.strip()])
    print(f"[scope-fence] OK — {n} changed path(s); 0 protected paths, 0 destructive markers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
