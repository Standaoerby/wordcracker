"""Positive / negative test cases for every pattern in the registry.

Rule R5: «Каждый регекс — в реестре, с позитивными И негативными кейсами.
Правка/добавление паттерна без негативного кейса, который мотивировал
правку, не принимается. Тот запрос, что баг породил, — становится
негативным тестом.»

Each addition to the patterns registry must add an entry here. The
queries that motivated R-22, E13, E20–E22 fixes are recorded as the
negative-case anchors below.
"""
from __future__ import annotations


# ============================================================
# CANONICAL_FORMAT_RE
# ============================================================

# Strings the pattern MUST match.
CANONICAL_FORMAT_POSITIVES: tuple[str, ...] = (
    # Bare canonical form
    "Doyle, Arthur Conan",
    # Single first-name token
    "Tolstoy, Leo",
    # Initials with dots
    "Wells, H. G.",
    # Lowercase normalized (typical post-normalize_query input)
    "doyle, arthur conan",
    # Inside a larger query (finditer should still hit)
    "какие книги у Doyle, Arthur Conan",
)

# Strings the pattern MUST NOT match.
#
# The first three are the R-22 anchors — ordinary RU text where a comma
# is just punctuation, not a Surname/First boundary. Before R-22 these
# leaked as fake CANONICAL_FORMAT mentions and corrupted author
# extraction. Now: surname token after the comma must look like a name,
# and the caller's alias-gate rejects what passes the shape check.
CANONICAL_FORMAT_NEGATIVES: tuple[str, ...] = (
    # No comma at all
    "Christopher Marlowe",
    # Comma followed by single short letter (under min length)
    "a, b",
    # Empty / whitespace
    "",
    "   ",
)


# ============================================================
# NON_FIRST_NAME_TOKENS
# ============================================================

# Tokens that MUST be in the set (stop-words).
NON_FIRST_NAME_POSITIVES: tuple[str, ...] = (
    # Russian preposition — "у Толстого"
    "у",
    # Russian conjunction
    "и",
    # English preposition — "books by Wells"
    "by",
    # Russian wh-word — "что у Толстого"
    "что",
    # Verb form — "написал Толстой"
    "написал",
    # Punctuation separator — author list «Толстой — Достоевский»
    # (formerly only in main.py's copy; merged into the canonical set)
    "—",
    # English article
    "the",
)

# Tokens that MUST NOT be in the set (real first-name candidates).
#
# The first three are the E13 / B-R17-1 negative anchors — these are
# actual first names that the resolver must treat as disambiguators,
# not stop-words. If any of these slip into NON_FIRST_NAME_TOKENS, then
# «Christopher Marlowe» / «Basil Wells» / «Carolyn Wells» stop resolving
# to their specific canonical and fall back to clarify — the E13 regression.
NON_FIRST_NAME_NEGATIVES: tuple[str, ...] = (
    "christopher",
    "basil",
    "carolyn",
    "h.",       # initial — "H. G. Wells" extraction relies on it
    "arthur",
    "leo",
)
