"""Helpers that don't fit the pattern/token-set shape.

Currently: `try_rapidfuzz` — the single source of truth for the optional
`rapidfuzz` import. Previously inlined as `_try_rapidfuzz` in
`entity_resolver.py`; the audit flagged a second copy with a divergent
contract elsewhere (Phase 1 collapse removed it). This consolidates the
remaining definition into the patterns package so any future copy is
visibly an anti-pattern.
"""
from __future__ import annotations

from typing import Any


def try_rapidfuzz() -> tuple[Any, Any] | None:
    """Return `(process, fuzz)` from rapidfuzz if installed, else None.

    Real prod has rapidfuzz installed; this guard covers dev boxes and
    CI without the optional dep. Consumers (`entity_resolver.rank_author_candidates`)
    fall back to `_simple_token_score` when None.

    Contract: returns either a 2-tuple `(process, fuzz)` OR `None`. Never
    raises. Never returns a partial.
    """
    try:
        from rapidfuzz import fuzz, process
        return process, fuzz
    except ImportError:
        return None


__all__ = ["try_rapidfuzz"]
