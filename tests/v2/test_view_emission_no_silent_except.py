"""Phase 2.5 / RECOVERY_BRIEF Cluster D gate — no silent swallowing
around view-emission.

Rule (R3-derived): every `try` block in `scripts/v2/tools/**/*.py`
that surrounds a `vb.attach_view(...)` / view-builder call MUST narrow
its `except` clause to the realistic builder-error tuple, NOT bare
`except Exception` / `except`.

Why: a broad `except Exception` masks builder bugs and tests can't see
why `result.view is None`. The brief calls this out explicitly:
  > Эта ловушка нужна, чтобы враппер не падал из-за бага рендера. Но
  > она же скрывает баги от тестов.
  > Recovery шаг 1: снять exception silently. Заменить except Exception
  > на узкий except (ValueError, TypeError) И залогировать с traceback.

This test is AST-driven — it walks each tool source, finds `try` blocks
that contain a call to `attach_view` / `build_*_view`, and checks the
attached `except` handlers. A bare `except` or `except Exception` next
to an attach_view-context fails the gate.

Allowed exception types in the narrow tuple:
  * Builder data-shape errors: ValueError, TypeError, KeyError,
    AttributeError, IndexError.
  * Anything else (CancelledError, KeyboardInterrupt, OSError) MUST
    propagate so view-construction bugs surface, not get logged-away.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


_ALLOWED_NARROW = frozenset({
    # Realistic shapes view builders raise on bad input
    "ValueError",
    "TypeError",
    "KeyError",
    "AttributeError",
    "IndexError",
    # Sometimes a view triggers a slow data load that times out
    "TimeoutError",
})


_VIEW_EMIT_NAMES = frozenset({
    "attach_view",
    # Some wrappers call into helpers that wrap attach_view themselves;
    # match the common surface here. Each builder name we touch in this
    # repo lives in scripts/v2/view_builders as `build_*`.
})


def _try_contains_view_emit(node: ast.Try) -> bool:
    """True if any call inside this try-body looks like view emission."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in _VIEW_EMIT_NAMES:
                return True
            # Wrappers also commonly call `result.view = build_*_view(...)`
            # or `view = build_*_view(...)` directly with attach below.
            # We treat any `build_*` call as a view emission surrogate.
            if isinstance(f, ast.Attribute) and f.attr.startswith("build_"):
                return True
            if isinstance(f, ast.Name) and f.id.startswith("build_"):
                return True
    return False


def _handler_is_broad(h: ast.ExceptHandler) -> bool:
    """True iff this handler is `except:` or `except Exception:`."""
    if h.type is None:
        return True
    if isinstance(h.type, ast.Name) and h.type.id == "Exception":
        return True
    if isinstance(h.type, ast.Name) and h.type.id == "BaseException":
        return True
    # `except (Exception, ...):` is also broad.
    if isinstance(h.type, ast.Tuple):
        for el in h.type.elts:
            if isinstance(el, ast.Name) and el.id in {"Exception", "BaseException"}:
                return True
    return False


def _handler_is_narrow_and_known(h: ast.ExceptHandler) -> bool:
    """True iff the handler uses ONLY allowed-narrow exception names."""
    if h.type is None:
        return False
    if isinstance(h.type, ast.Name):
        return h.type.id in _ALLOWED_NARROW
    if isinstance(h.type, ast.Tuple):
        return all(
            isinstance(el, ast.Name) and el.id in _ALLOWED_NARROW
            for el in h.type.elts
        )
    return False


class NoSilentExceptAroundViewEmission(unittest.TestCase):
    """Walk every tool file; flag try/except Exception around attach_view."""

    def test_no_broad_except_around_attach_view(self):
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[2] / "scripts" / "v2" / "tools"
        for py in root.rglob("*.py"):
            rel = py.relative_to(root.parent.parent.parent).as_posix()
            try:
                src = py.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                tree = ast.parse(src, filename=rel)
            except SyntaxError as e:
                self.fail(f"{rel}: SyntaxError {e}")

            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                if not _try_contains_view_emit(node):
                    continue
                for handler in node.handlers:
                    if _handler_is_broad(handler):
                        offenders.append(
                            f"{rel}:{handler.lineno} — broad except around "
                            f"attach_view / build_*_view. Narrow to "
                            f"({', '.join(sorted(_ALLOWED_NARROW))}) and "
                            f"use logging.exception(...) so the traceback "
                            f"surfaces.",
                        )

        self.assertEqual(
            offenders, [],
            "RECOVERY_BRIEF Cluster D violated — view-emission silently "
            "swallowed:\n  " + "\n  ".join(offenders),
        )

    def test_view_emission_handlers_use_known_narrow_set(self):
        """Stricter half: handlers should use ONLY the allowed names.

        A wrapper that catches `OSError` or `RuntimeError` around
        attach_view is also suspect — those aren't expected from a
        well-typed builder, and if they happen the wrapper should let
        them propagate (or write a separate try/except outside the
        attach_view block).
        """
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[2] / "scripts" / "v2" / "tools"
        for py in root.rglob("*.py"):
            rel = py.relative_to(root.parent.parent.parent).as_posix()
            try:
                src = py.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                tree = ast.parse(src, filename=rel)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                if not _try_contains_view_emit(node):
                    continue
                for handler in node.handlers:
                    if _handler_is_broad(handler):
                        continue  # already caught by the first test
                    if not _handler_is_narrow_and_known(handler):
                        # Compose a readable rep of the handler type
                        rep = ast.unparse(handler.type) if handler.type else "<bare>"
                        offenders.append(
                            f"{rel}:{handler.lineno} — view-emission except "
                            f"uses unknown exception type {rep!r}. Allowed: "
                            f"{sorted(_ALLOWED_NARROW)}.",
                        )

        self.assertEqual(
            offenders, [],
            "view-emission handlers must use the canonical narrow tuple:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
