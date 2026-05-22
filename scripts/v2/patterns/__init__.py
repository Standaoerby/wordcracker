"""Pattern registry — Phase 3 of REFACTOR_BRIEF.

Single source of truth for regexes and token-set predicates that were
previously inlined and diverged across modules (root cause of E4 topics,
E13 over-eager disambiguation, E20/E21/E22 lang truncation,
R-22 canonical-format false positives).

Rules:
  * Every pattern is defined ONCE, in `registry.py`, with positives and
    negatives in `cases.py`.
  * The pattern test (`tests/v2/test_patterns.py`) iterates the registry
    and asserts every positive matches AND every negative does NOT match.
  * A pattern without ≥2 positives + ≥2 negatives is a build error.

Public surface:

    from scripts.v2.patterns import (
        CANONICAL_FORMAT_RE,            # compiled regex
        NON_FIRST_NAME_TOKENS,          # frozenset[str]
        try_rapidfuzz,                  # callable
        PATTERNS, TOKEN_SETS,           # registries
    )

Consumers (mentions.py, main.py, entity_resolver.py) import from here
instead of defining their own copies. Drift becomes impossible.
"""
from __future__ import annotations

from scripts.v2.patterns.helpers import try_rapidfuzz
from scripts.v2.patterns.registry import (
    CANONICAL_FORMAT_RE,
    NON_FIRST_NAME_TOKENS,
    PATTERNS,
    TOKEN_SETS,
    CompiledPattern,
    TokenSet,
)

__all__ = [
    "CANONICAL_FORMAT_RE",
    "NON_FIRST_NAME_TOKENS",
    "try_rapidfuzz",
    "PATTERNS",
    "TOKEN_SETS",
    "CompiledPattern",
    "TokenSet",
]
