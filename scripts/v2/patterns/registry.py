"""PATTERNS / TOKEN_SETS — central registry of regexes & token-set predicates.

Two kinds of entries:

  * `CompiledPattern`  — a `re.Pattern` plus declared positive / negative
    test cases. Iterated by `tests/v2/test_patterns.py` — every positive
    MUST match, every negative MUST NOT. A pattern without ≥2 of each
    is a build error.

  * `TokenSet` — a `frozenset[str]` predicate (membership-check), with
    positive members and negative non-members. Used for the
    `NON_FIRST_NAME_TOKENS` style stop-list that previously diverged
    between modules.

The cases live in `cases.py` to keep this file readable. Adding a new
pattern is one entry here + cases there + nothing else.

Phase 3 / REFACTOR_BRIEF.md, "Регекс-харнесс".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.v2.patterns.cases import (
    CANONICAL_FORMAT_NEGATIVES,
    CANONICAL_FORMAT_POSITIVES,
    NON_FIRST_NAME_NEGATIVES,
    NON_FIRST_NAME_POSITIVES,
)


@dataclass(frozen=True)
class CompiledPattern:
    """A regex bound to its positive / negative test cases.

    `positives` — strings that the pattern MUST match (finditer ≥ 1 hit).
    `negatives` — strings that the pattern MUST NOT match (finditer == 0).

    The brief: «Паттерн без negatives[] = ошибка сборки.» That is
    enforced by `test_patterns.py`, not at runtime — we don't want
    import-time test infrastructure.
    """
    name: str
    pattern: re.Pattern
    positives: tuple[str, ...]
    negatives: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class TokenSet:
    """A frozenset[str] membership predicate, bound to its cases.

    `positives` — strings that MUST be in the set.
    `negatives` — strings that MUST NOT be in the set.
    """
    name: str
    tokens: frozenset[str]
    positives: tuple[str, ...]
    negatives: tuple[str, ...]
    description: str = ""


# ============================================================
# Regex definitions
# ============================================================

# CANONICAL_FORMAT — explicit «Surname, First [Middle...]» pattern.
#
# Surname: 2+ letters NO SPACES (single token), followed by comma.
# First name(s): letters + dots/spaces (e.g. "H. G." with two initials).
# Lookahead doesn't break on dot — allows "h. g." capture; breaks on
# other punctuation / end-of-string.
#
# CRITICAL: This regex is permissive. The CALLER must additionally
# require that the captured surname is a known author alias — without
# that gate, ordinary RU text like «слова, которые» matches and pollutes
# author extraction (R-22 root cause). The negative cases below verify
# the regex shape, not the alias check.
CANONICAL_FORMAT_RE = re.compile(
    r"\b([a-zа-яёA-ZА-ЯЁ']{2,}),\s+([a-zа-яёA-ZА-ЯЁ][a-zа-яёA-ZА-ЯЁ. ]+?)"
    r"(?=[\s,;:!?]|$|\s*$)",
)


# ============================================================
# Token-set definitions
# ============================================================

# NON_FIRST_NAME_TOKENS — stop-words that may appear before a surname
# but are NOT first names (prepositions, particles, verb forms).
#
# Previously duplicated in:
#   - scripts/v2/entity_resolver_v6/mentions.py (without punctuation)
#   - scripts/v2/entity_resolver_v6/main.py    (with punctuation)
# Now: ONE definition, with the union of both (punctuation included —
# main.py needed it to handle author lists separated by «—», «:», etc.).
NON_FIRST_NAME_TOKENS: frozenset[str] = frozenset({
    # Russian prepositions / conjunctions
    "и", "а", "но", "у", "в", "на", "от", "из", "к", "с", "о",
    # Russian wh-words
    "что", "как", "почему", "зачем", "когда", "где", "куда",
    # Russian imperatives / common verbs
    "сравни", "напиши", "покажи", "дай", "найди", "слова",
    "слов", "книги", "книг", "автор", "авторы", "автора",
    "написал", "написала", "написали",
    # English prepositions / function words
    "by", "of", "and", "or", "the", "a", "an", "from", "to",
    "books", "author", "authors", "compare", "show", "give",
    "find", "wrote", "works",
    # Punctuation / separators that may appear between authors
    # (formerly only in main.py — merging the two stop-lists picks this up)
    "—", "-", ":", ";",
})


# ============================================================
# Registries
# ============================================================

PATTERNS: dict[str, CompiledPattern] = {
    "canonical_format": CompiledPattern(
        name="canonical_format",
        pattern=CANONICAL_FORMAT_RE,
        positives=CANONICAL_FORMAT_POSITIVES,
        negatives=CANONICAL_FORMAT_NEGATIVES,
        description=(
            "«Surname, FirstName» canonical form. Permissive — caller "
            "must additionally check that surname is a known alias."
        ),
    ),
}


TOKEN_SETS: dict[str, TokenSet] = {
    "non_first_name_tokens": TokenSet(
        name="non_first_name_tokens",
        tokens=NON_FIRST_NAME_TOKENS,
        positives=NON_FIRST_NAME_POSITIVES,
        negatives=NON_FIRST_NAME_NEGATIVES,
        description=(
            "Stop-list of tokens that look like first-name candidates "
            "but are actually prepositions / particles / verb forms / "
            "punctuation separators."
        ),
    ),
}


__all__ = [
    "CANONICAL_FORMAT_RE",
    "NON_FIRST_NAME_TOKENS",
    "PATTERNS",
    "TOKEN_SETS",
    "CompiledPattern",
    "TokenSet",
]
