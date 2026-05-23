"""v6 Stage 3 — Multi-Factor Scoring.

Score each candidate against the mention with 3 factors:
  string_sim       — Jaccard token overlap between mention.surface and
                     candidate.display (lowercased)
  token_overlap    — fraction of mention.extra_tokens present as
                     substrings of candidate.display.lower()
  prominence_prior — log-normalized downloads relative to max in group

Weights vary by mention type — the user's signal dictates emphasis:

  FULL_NAME         token_overlap=0.6 string_sim=0.2 prominence=0.2
    User gave first-name token — token_overlap dominates.

  CANONICAL_FORMAT  string_sim=0.7 token_overlap=0.2 prominence=0.1
    User gave exact form — string match dominates.

  RU_STEM           prominence=0.5 string_sim=0.4 token_overlap=0.1
    RU stem usually points to one author; prominence + match.

  ALIAS_HIT         prominence=0.5 string_sim=0.5 token_overlap=0.0
    Multi-word alias is already specific; trust it; rank by prominence.

  SURNAME_ONLY      prominence=0.6 string_sim=0.4 token_overlap=0.0
    No disambiguating info — prominence is best signal.

Combined score is the weighted sum, in [0, 1].
"""
from __future__ import annotations

import math

from scripts.v2.entity_resolver_v6.types import (
    Candidate,
    CandidateScore,
    Mention,
    MentionType,
)


# Weights table: (token_overlap_w, string_sim_w, prominence_w)
# RU_STEM and ALIAS_HIT bump prominence higher because cross-alphabet
# (Cyrillic surface vs Latin canonical) zeroes out string_sim — prominence
# becomes the dominant signal. Transliteration helps token_overlap in
# scoring but Jaccard string_sim remains low across alphabets.
_WEIGHTS: dict[MentionType, tuple[float, float, float]] = {
    MentionType.FULL_NAME:         (0.6, 0.2, 0.2),
    MentionType.CANONICAL_FORMAT:  (0.2, 0.7, 0.1),
    MentionType.RU_STEM:           (0.1, 0.1, 0.8),
    MentionType.ALIAS_HIT:         (0.3, 0.2, 0.5),
    MentionType.SURNAME_ONLY:      (0.0, 0.4, 0.6),
}


# First-name transliteration map for cross-alphabet token_overlap.
# Used when mention.extra_tokens contains Cyrillic and candidate.display
# is Latin (or vice versa). Compact curated map for most common cases —
# extend as needed.
_RU_TO_EN_FIRST_NAMES = {
    "лев": "leo",
    "льва": "leo",
    "льву": "leo",
    "львом": "leo",
    "льве": "leo",
    "конан": "conan",
    "конана": "conan",
    "артур": "arthur",
    "артура": "arthur",
    "александр": "alexander",
    "александра": "alexander",
    "виктор": "victor",
    "виктора": "victor",
    "уильям": "william",
    "уильяма": "william",
    "томас": "thomas",
    "томаса": "thomas",
    "натаниэль": "nathaniel",
    "фёдор": "fyodor",
    "федор": "fyodor",
    "фёдора": "fyodor",
    "иван": "ivan",
    "ивана": "ivan",
    "николай": "nikolai",
    "николая": "nikolai",
    "антон": "anton",
    "антона": "anton",
    "анна": "anna",
    "анны": "anna",
    "льюис": "lewis",
    "герберт": "herbert",
    "джордж": "george",
    "редьярд": "rudyard",
    "честертон": "chesterton",  # fallback if surname appears in extra
    "оскар": "oscar",
    "марк": "mark",
    "артура": "arthur",
    "оливер": "oliver",
    "генри": "henry",
    "джон": "john",
    "джона": "john",
}


def _transliterate_ru_token(t: str) -> str:
    """Return Latin transliteration if t is Cyrillic and known. Else t.

    Lookup in _RU_TO_EN_FIRST_NAMES. Falls back to input on miss.
    """
    return _RU_TO_EN_FIRST_NAMES.get(t.lower(), t)


def score_candidates(
    mention: Mention,
    candidates: list[Candidate],
) -> list[CandidateScore]:
    """Score each candidate; return sorted descending by combined.

    Empty list when no candidates.
    """
    if not candidates:
        return []

    w_token, w_string, w_prom = _WEIGHTS.get(
        mention.type, (0.0, 0.5, 0.5)
    )

    max_dl = max((c.prominence for c in candidates), default=1) or 1
    mention_tokens = _tokenize(mention.surface.lower())

    scored: list[CandidateScore] = []
    for c in candidates:
        cand_lc = c.display.lower()
        cand_tokens = _tokenize(cand_lc)

        # 1. String similarity — PRECISION (mention containment).
        # Use precision-based score: how much of the mention is found
        # in the candidate, NOT Jaccard. Jaccard penalizes longer
        # canonical names (e.g. "Wells, H. G. (Herbert George)" gets
        # lower Jaccard vs mention than "Wells, J. (Joseph)" purely
        # because the canonical is longer — false signal).
        if mention_tokens:
            overlap = len(cand_tokens & mention_tokens)
            string_sim = overlap / len(mention_tokens) if mention_tokens else 0.0
        else:
            string_sim = 0.0

        # 2. Token overlap — fraction of extra_tokens present
        # Normalize both sides. Critical: short tokens (single letter
        # initials like 'h', 'g') match ONLY against full canonical
        # tokens or first-letters-of-tokens — NOT substring (else 'h'
        # matches «joseph» which has 'h' inside).
        if mention.extra_tokens:
            cand_norm = _strip_punctuation(cand_lc)
            cand_token_set = set(t for t in cand_norm.split() if t)
            cand_first_letters = set(t[0] for t in cand_token_set if t)
            match_count = 0
            for t in mention.extra_tokens:
                t_norm = _strip_punctuation(t)
                if not t_norm:
                    continue
                # Cross-alphabet: transliterate Cyrillic → Latin
                t_translit = _transliterate_ru_token(t_norm)
                # Try in order of specificity:
                # A) Full token equality with any canonical token
                if t_norm in cand_token_set or t_translit in cand_token_set:
                    match_count += 1
                    continue
                # B) Substring match — ONLY for tokens ≥ 3 chars
                #    (prevents 'h' from matching 'joseph')
                if len(t_norm) >= 3:
                    if t_norm in cand_norm or t_translit in cand_norm:
                        match_count += 1
                        continue
                # C) Single-letter initial: match if it's any canonical
                #    token's first letter (e.g. 'h' matches «Herbert»)
                if len(t_norm) == 1 and t_norm in cand_first_letters:
                    match_count += 1
                    continue
            token_overlap = match_count / len(mention.extra_tokens)
        else:
            token_overlap = 0.0

        # 3. Prominence prior — log-normalized
        prominence_prior = (
            math.log(1 + c.prominence) / math.log(1 + max_dl)
            if max_dl > 0 else 0.0
        )

        combined = (
            w_token * token_overlap
            + w_string * string_sim
            + w_prom * prominence_prior
        )

        scored.append(CandidateScore(
            candidate=c,
            string_sim=round(string_sim, 4),
            token_overlap=round(token_overlap, 4),
            prominence_prior=round(prominence_prior, 4),
            combined=round(combined, 4),
        ))

    scored.sort(key=lambda x: x.combined, reverse=True)
    return scored


def _strip_punctuation(s: str) -> str:
    """Replace punctuation with spaces, collapse whitespace."""
    out = []
    prev_space = False
    for ch in s:
        if ch.isalpha() or ch.isdigit():
            out.append(ch)
            prev_space = False
        else:
            if not prev_space:
                out.append(" ")
                prev_space = True
    return "".join(out).strip()


def _tokenize(s: str) -> set[str]:
    """Tokenize a string into a set of meaningful tokens.

    - Lowercase
    - Strip punctuation
    - Drop tokens shorter than 2 chars (initials excluded as Jaccard noise)
    - Treat dots as token separators (h.g. → h, g)
    """
    out: set[str] = set()
    cur: list[str] = []
    for ch in s.lower():
        if ch.isalpha():
            cur.append(ch)
        else:
            if cur:
                tok = "".join(cur)
                if len(tok) >= 2:
                    out.add(tok)
            cur = []
    if cur:
        tok = "".join(cur)
        if len(tok) >= 2:
            out.add(tok)
    return out
