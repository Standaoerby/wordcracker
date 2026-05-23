"""v6 Stage 2 — Candidate Generation.

For each Mention, return all canonical KB entries that could match.
Strategy varies by mention type:

  CANONICAL_FORMAT → direct by_canonical lookup; fuzzy fallback if miss
  ALIAS_HIT        → use alias regex, filter by_canonical for matching
                     surname (alias targets surname regex)
  FULL_NAME        → use surname regex from alias, filter by_canonical;
                     Stage 3 scoring will use extra_tokens to disambiguate
  RU_STEM          → surname regex from RU alias, filter by_canonical
  SURNAME_ONLY     → surname regex, filter by_canonical

Returns up to `limit` candidates per mention (default 20). Stage 3 ranks
them, Stage 4 thresholds.
"""
from __future__ import annotations

import re

from scripts.v2.entity_resolver_v6.prominence import get_prominence_index
from scripts.v2.entity_resolver_v6.types import Candidate, Mention, MentionType


def generate_candidates(mention: Mention, *, limit: int = 20) -> list[Candidate]:
    """Return KB candidates matching this mention.

    Defensive: returns empty list if prominence index unavailable
    (CI / dev environments).
    """
    try:
        idx = get_prominence_index()
    except Exception:
        return []
    if not isinstance(idx, dict):
        return []
    by_canonical = idx.get("by_canonical", {}) if "by_canonical" in idx else {}

    surname = _extract_surname(mention.regex)
    if not surname:
        return []

    # Defensive fallback — when metadata index unavailable (CI/dev),
    # synthesize a single candidate from the alias regex itself so
    # downstream still gets «something». Prominence/books = 0.
    if not by_canonical:
        return [Candidate(
            key=mention.regex,
            display=surname,
            score=80,
            source="v6_alias_fallback",
            prominence=0,
            books_in_corpus=0,
        )]

    # All canonicals matching the regex prefix.
    # If regex is specific (e.g. `^Hardy, Thomas`), filter by full prefix
    # — not just surname. Otherwise (bare `^Surname,`) all surnames match.
    regex_prefix = mention.regex.lstrip("^")  # "Hardy, Thomas" or "Wells,"
    regex_prefix_lower = regex_prefix.lower()
    matches: list[tuple[str, dict]] = []
    for name, ent in by_canonical.items():
        if not isinstance(name, str):
            continue
        if name.lower().startswith(regex_prefix_lower):
            matches.append((name, ent))
    if not matches:
        # Same fallback — known alias but no canonical in index. Synthesize.
        return [Candidate(
            key=mention.regex,
            display=surname,
            score=80,
            source="v6_alias_fallback",
            prominence=0,
            books_in_corpus=0,
        )]

    # Sort by downloads desc as initial ordering — Stage 3 will rescore
    matches.sort(key=lambda x: x[1].get("downloads", 0), reverse=True)

    out: list[Candidate] = []
    for name, ent in matches[:limit]:
        out.append(Candidate(
            key=f"^{name}",                # tighter regex (full canonical)
            display=name,
            score=100,                      # placeholder; Stage 3 rescores
            source="v6_kb_lookup",
            prominence=int(ent.get("downloads", 0) or 0),
            books_in_corpus=int(ent.get("books", 0) or 0),
        ))
    return out


def _extract_surname(regex: str) -> str | None:
    """Extract surname from `^Surname,` regex pattern."""
    m = re.fullmatch(r"\^([A-Za-zЀ-ӿ' -]+),.*", regex)
    if not m:
        return None
    return m.group(1).strip()
