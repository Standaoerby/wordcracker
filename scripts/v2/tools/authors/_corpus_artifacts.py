"""Corpus markup-artifact filter for affinity outputs.

Stan 2026-05-19: «xvth» showed up in Christie's top affinity list
(author_count=8, corpus_count=605, affinity=138). It's NOT vocabulary —
it's `XV-th` (XV century) from broken / OCR-d text markup. v3.1.1
surname filter didn't catch it (not a name), corpus-diff heuristic
passed (diff = 597 ≥ threshold 10), spaCy PROPN unreliable on such
tokens.

This module filters known corpus markup classes:

1. **Roman-numeral ordinals** — `iith / iiird / vth / xth / xith /
   xvth / xviith / xviiith / xixth / xxth ...` and bare Roman
   numerals (`ii / iii / iv / vi / vii / viii / ix / xi / xii / ...`).
   Pattern: `^[ivxlcdm]+(th|st|nd|rd)?$` after lowercase.

2. **Pure-consonant clusters of length ≥3** — like `pqr` / `wxyz` /
   `lll` / `iii` (well, ii / iii are already roman-numeral matched).
   These are almost always OCR garbage. Skipped if the token has any
   vowel (vowels = aeiou; y is ambiguous).

3. **Single-letter «words»** — `a / b / c / ...`. These slip past
   affinity v1 min_corpus_count when corpus has them as section
   markers. Drop length-1 alpha tokens.

Use parity with surname filter — apply in affinity_by_author,
affinity_by_book, and learning_words wrappers.
"""
from __future__ import annotations

import re
from typing import Tuple


# Roman-numeral characters: i v x l c d m (lowercase after normalize)
# Ordinal suffix: th / st / nd / rd
_ROMAN_NUMERAL_RE = re.compile(
    r"^[ivxlcdm]+(?:th|st|nd|rd)?$"
)

# Consonant-only tokens of length ≥3 (no vowels). OCR / markup garbage.
_VOWELS = set("aeiouAEIOU")


def is_corpus_artifact(word: str) -> bool:
    """True iff `word` looks like a corpus markup artifact and should
    NOT appear in vocabulary outputs.

    Conservative — false positives here mean dropping a real word,
    so prefer letting questionable tokens through. The patterns
    target known artifact shapes only.
    """
    if not word or not isinstance(word, str):
        return False
    w = word.strip().lower()
    if not w:
        return False
    # Single-character "words" (a / b / c). Note: pronoun "a" / "i"
    # are legitimate in vocab streams sometimes, but they'd already
    # be filtered by author_count thresholds — at this layer we're
    # dropping page-marker artifacts.
    if len(w) == 1 and w.isalpha():
        return True
    # Roman-numeral ordinals: ii / iii / iv / vth / xith / xvth ...
    # Be careful: «vi» / «vii» etc are Roman numerals BUT could also
    # be real words in some contexts. We're aggressive here because
    # in a vocab-by-affinity context these are virtually always markup.
    if _ROMAN_NUMERAL_RE.fullmatch(w):
        # Exempt very short pronouns that happen to be Roman-numeral-
        # shaped: «i» (already caught by len==1), «v» (very edge case).
        # The 2+ char Roman patterns are safe to drop.
        if len(w) >= 2:
            return True
    # Consonant-only ≥3: «pqr» / «wxyz» / «lll» / «xyz».
    # Only apply to fully-alpha tokens — digit-containing tokens like
    # «w11» / «t9» are placeholder strings the caller already cares
    # about (and they're not corpus markup in the OCR sense).
    if len(w) >= 3 and w.isalpha() and not any(ch in _VOWELS for ch in w):
        return True
    return False


def filter_corpus_artifacts(rows: list[dict], *,
                              word_key: str = "word",
                              ) -> Tuple[list[dict], int]:
    """Drop rows whose `word_key` value is a corpus markup artifact.

    Returns (filtered_rows, dropped_count). Mirrors the API of
    `filter_surnames` so the affinity wrappers can chain both filters
    with the same call shape.
    """
    if not rows:
        return rows, 0
    kept = [r for r in rows
            if not is_corpus_artifact(r.get(word_key) or "")]
    return kept, len(rows) - len(kept)


__all__ = ["is_corpus_artifact", "filter_corpus_artifacts"]
