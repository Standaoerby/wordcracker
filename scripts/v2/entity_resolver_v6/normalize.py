"""v6 entity resolver — query normalization + RU lemmatization.

Moved here from `scripts.v2.entity_resolver` (T1 / D-P1-6) so v6 submodules
have a sibling-package home for these primitives and the old module collapses
to a re-export shim.

Pipeline:
  NFKC → strip quotes → unify dashes → fold Latin→Cyrillic homoglyphs in
  mixed-alphabet tokens → collapse whitespace → lowercase.

Plus per-token RU genitive→nominative suffix folding for surnames.

Behaviour is exactly what `entity_resolver.normalize_query` did before T1 —
this is a pure move, not a rewrite. Tests in test_entity_resolver_v5.py /
test_entities.py / test_entity_resolver_v6.py continue to exercise it via
the re-export shim.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


# Latin → Cyrillic homoglyph table. Covers the cases R14 surfaced
# (Latin 'c' inside «Доcтоевский»). Only Latin→Cyrillic direction;
# we never want Cyrillic→Latin since the corpus side is mostly Latin
# but RU author names are always Cyrillic in user input.
_HOMOGLYPH_LAT_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "h": "н", "k": "к", "m": "м", "t": "т", "b": "в",
    "A": "А", "C": "С", "E": "Е", "O": "О", "P": "Р", "X": "Х",
    "Y": "У", "H": "Н", "K": "К", "M": "М", "T": "Т", "B": "В",
}

_DASH_RE = re.compile(r"[‐‑‒–—―−]")
_QUOTES = "\"'«»“”„‟‹›"


def _has_cyrillic(s: str) -> bool:
    return any("А" <= ch <= "я" or ch in "ёЁ" for ch in s)


def _has_latin(s: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in s)


def _fold_homoglyphs_in_cyrillic_run(s: str) -> str:
    """Per-token Latin→Cyrillic fold inside mixed-alphabet tokens.

    Pure-Latin queries («Doyle») are left alone; per-token scope so a
    Latin author name in the middle of a Cyrillic sentence is preserved.
    """
    if not _has_cyrillic(s):
        return s
    out = []
    for tok in re.split(r"(\s+)", s):
        if tok.isspace() or not tok:
            out.append(tok)
            continue
        if _has_cyrillic(tok) and _has_latin(tok):
            folded = "".join(_HOMOGLYPH_LAT_TO_CYR.get(ch, ch) for ch in tok)
            out.append(folded)
        else:
            out.append(tok)
    return "".join(out)


@dataclass
class NormalizationResult:
    output: str
    steps: list[str] = field(default_factory=list)


def normalize_query(q: str) -> NormalizationResult:
    """Apply the normalization pipeline. Always idempotent.

    Steps are recorded in `steps` for trace logging (P9 RequestTrace).
    """
    raw = q or ""
    steps: list[str] = []

    nfkc = unicodedata.normalize("NFKC", raw)
    if nfkc != raw:
        steps.append(f"NFKC ({len(raw)} → {len(nfkc)} chars)")

    stripped = nfkc.strip().strip(_QUOTES).strip()
    if stripped != nfkc.strip():
        steps.append("stripped quotes")

    de_dashed = _DASH_RE.sub("-", stripped)
    if de_dashed != stripped:
        steps.append("dashes → ASCII")

    folded = _fold_homoglyphs_in_cyrillic_run(de_dashed)
    if folded != de_dashed:
        steps.append("homoglyph fold (Latin → Cyrillic)")

    spaced = re.sub(r"\s+", " ", folded).strip()
    if spaced != folded:
        steps.append("whitespace collapsed")

    lc = spaced.lower()
    if lc != spaced:
        steps.append("lowercased")

    return NormalizationResult(output=lc, steps=steps)


# Russian genitive endings for masculine surnames. Conservative —
# we want to catch «Толстого» → «Толстой», not over-fold genuine
# nominative forms.
#
# CRITICAL — rules are matched in order, so MORE SPECIFIC rules (longer
# suffix) MUST come first, else «достоевского» matches «ого» before
# «ского» and folds to «достоевской» (wrong).
_RU_AUTHOR_SUFFIX_RULES: list[tuple[str, str]] = [
    # masculine in -ский / -цкий — longer suffixes first
    ("ского", "ский"),    # Достоевского → Достоевский
    ("скому", "ский"),
    ("ским",  "ский"),
    ("ском",  "ский"),
    ("цкого", "цкий"),
    ("цкому", "цкий"),
    # feminine endings — longer first
    ("овой",  "ов"),
    ("евой",  "ев"),
    ("ова",   "ов"),
    ("ову",   "ов"),
    ("ева",   "ев"),
    ("еву",   "ев"),
    # masculine surnames in -ой
    ("ого",   "ой"),    # Толстого → Толстой
    ("ому",   "ой"),    # Толстому → Толстой
    ("ым",    "ой"),    # Толстым → Толстой (instrumental)
    # short -а ending (Свифта → Свифт). Very generic — only apply
    # when earlier rules didn't match.
    ("ом",    ""),
    ("а",     ""),
    ("у",     ""),
    ("е",     ""),
]
_RU_AUTHOR_SUFFIX_RULES.sort(key=lambda r: -len(r[0]))


def _ru_lemmatize_author_token(tok: str) -> tuple[str, str | None]:
    """Try to fold a single Cyrillic author token to its nominative.

    Returns (lemma, rule_applied | None). If no rule applies (or token
    isn't Cyrillic), returns (tok, None).
    """
    if not tok or not _has_cyrillic(tok) or len(tok) < 4:
        return tok, None
    low = tok.lower()
    for suf, repl in _RU_AUTHOR_SUFFIX_RULES:
        if low.endswith(suf) and len(low) > len(suf) + 1:
            new = low[: -len(suf)] + repl
            return new, f"{suf} → {repl}"
    return tok, None


def ru_lemmatize_author_query(q: str) -> tuple[str, list[str]]:
    """Per-token RU lemmatization with rule trace for observability."""
    if not _has_cyrillic(q):
        return q, []
    parts = q.split()
    out = []
    trace: list[str] = []
    for p in parts:
        lemma, rule = _ru_lemmatize_author_token(p)
        if rule:
            trace.append(f"RU author lemma «{p}» → «{lemma}» ({rule})")
        out.append(lemma)
    return " ".join(out), trace


__all__ = [
    "NormalizationResult",
    "normalize_query",
    "ru_lemmatize_author_query",
]
