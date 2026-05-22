"""Phase 3 lint — forbid `.split()[0][:N]` string-truncation anti-pattern.

Root cause of E20/E21/E22 (lang filter «english» → «eng» instead of «en»;
ajar surfaced because of «en» vs «eng» mismatch on stringified-list lang
metadata). The shape is universal:

    value.split("-")[0][:3]    ← three-char prefix of first segment

It's seductive — looks like a cheap normalization — but the «length-3»
literal is wrong for ISO language codes (which are length 2 or 3, never
exactly 3). Every observed prod use in this repo was incorrect.

Rule: do NOT chain `.split(...)[0][:N]` style truncation in scripts/v2.
Use a registered regex or an explicit lookup. Comments are allowed (so
the historical fix notes stay readable).

Gate: this AST-walking test must report zero occurrences across
scripts/v2 production code.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# Paths exempted from the lint:
#   * The patterns package itself (we read source for the lint, no
#     truncation in product code).
#   * Tests — they may contain regression cases that intentionally embed
#     the literal string. (Note: tests live outside scripts/v2 anyway.)
_EXEMPT_DIRS = ()


class NoSplitSliceAntipattern(unittest.TestCase):
    """Walk every .py in scripts/v2; flag `x.split(...)[0][:N]` chains.

    Detection is AST-based — we look for a Subscript whose value is
    another Subscript[0] whose value is a `.split(...)` Call. That
    matches the prod shape exactly without flagging benign list/dict
    slicing like `cands[:5]` or `items[0]`.
    """

    def test_no_split_then_zero_then_int_slice(self):
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[2] / "scripts" / "v2"
        for py in root.rglob("*.py"):
            rel = py.relative_to(root.parent.parent).as_posix()
            if any(rel.startswith(d) for d in _EXEMPT_DIRS):
                continue
            try:
                src = py.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                tree = ast.parse(src, filename=rel)
            except SyntaxError as e:
                # Surface as a separate failure — Phase 0 R10 ensures
                # the suite collects; a SyntaxError here would have
                # blocked import too.
                self.fail(f"{rel}: SyntaxError {e}")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                # Outer: x[:N]  — slice with at most an integer upper
                outer_slice = node.slice
                if not _is_int_upper_slice(outer_slice):
                    continue
                # Inner: x[0]  — subscript with constant int 0
                inner = node.value
                if not isinstance(inner, ast.Subscript):
                    continue
                if not _is_constant_zero(inner.slice):
                    continue
                # Innermost: x.split(...)  — call to .split
                call = inner.value
                if not isinstance(call, ast.Call):
                    continue
                if not (isinstance(call.func, ast.Attribute)
                        and call.func.attr == "split"):
                    continue
                offenders.append(
                    f"{rel}:{node.lineno} — `.split(...)[0][:{_int_upper(outer_slice)}]` "
                    f"truncation forbidden (root of E20/E21/E22)",
                )

        self.assertEqual(
            offenders, [],
            "Found .split()[0][:N] truncation anti-pattern:\n  "
            + "\n  ".join(offenders),
        )


def _is_constant_zero(s: ast.AST) -> bool:
    return isinstance(s, ast.Constant) and s.value == 0


def _is_int_upper_slice(s: ast.AST) -> bool:
    """True if `s` is `[:N]` where N is an int constant.

    AST shape: `ast.Slice(lower=None, upper=Constant(int), step=None)`.
    """
    if not isinstance(s, ast.Slice):
        return False
    if s.lower is not None:
        return False
    if s.step is not None:
        return False
    if not isinstance(s.upper, ast.Constant):
        return False
    return isinstance(s.upper.value, int)


def _int_upper(s: ast.AST) -> int:
    assert isinstance(s, ast.Slice)
    assert isinstance(s.upper, ast.Constant)
    return int(s.upper.value)


if __name__ == "__main__":
    unittest.main()
