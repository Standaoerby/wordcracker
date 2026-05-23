"""v5 fuzzy helpers — kept for the legacy test surface.

Moved from `scripts.v2.entity_resolver` (T1 / D-P1-6). The v6 pipeline
(mentions → candidates → scoring → decide) does NOT use these — v6 reads
the prominence index directly in `candidates.generate_candidates`. The
helpers here remain in-tree because `test_entity_resolver_v5.py` still
exercises them as a contract (per-canonical prominence in fuzzy
matching, surname-specialization, multi-token disambig follow-up).

Names with a leading underscore are kept as-is to preserve the test
import paths (`er._candidates_from_alias`, `er._specialize_…`).
"""
from __future__ import annotations

import re

from scripts.v2.entity_resolver_v6.prominence import (
    get_prominence_index,
    prominence_for,
    prominence_for_canonical,
)
from scripts.v2.entity_resolver_v6.types import Candidate


def _try_rapidfuzz():
    """Compat shim — see scripts.v2.patterns.helpers.try_rapidfuzz.

    Kept as a thin alias so existing internal callers continue to work
    without churn. The single source of truth lives in the patterns
    package (Phase 3).
    """
    from scripts.v2.patterns import try_rapidfuzz
    return try_rapidfuzz()


def _simple_token_score(q: str, a: str) -> int:
    """Cheap fuzzy substring score used when rapidfuzz is not installed.

    Author entries in SPGC metadata have the shape "Surname, Forenames",
    so token-aware scoring matters. A query of "hugo" should match
    "Hugo, Victor" with a high score (token match on surname), not 33%
    completeness ratio. Levels:

      100  exact match
       95  surname (first comma-separated token) match
       85  any other token match
       80  query is a prefix of the whole string
       70  query is anywhere inside the string
       75  full author string is inside the query (rare)
        0  otherwise
    """
    if not q or not a:
        return 0
    ql, al = q.strip().lower(), a.strip().lower()
    if not ql or not al:
        return 0
    if ql == al:
        return 100
    tokens = re.split(r"[\s,.]+", al)
    tokens = [t for t in tokens if t]
    if tokens and tokens[0] == ql:
        return 95
    if ql in tokens:
        return 85
    if al.startswith(ql):
        return 80
    if ql in al:
        return 70
    if al in ql:
        return 75
    return 0


def _regex_to_display(regex: str) -> str:
    return regex.lstrip("^").rstrip(",").strip()


def _match_canonical_by_tokens(
    q_lc: str, regex: str,
) -> tuple[str, str, dict] | None:
    """Disambiguation-followup helper.

    When user typed «Basil Wells» (multi-token, alias matched `^Wells,`
    via surname-fallback), find the canonical author whose name
    contains BOTH the surname AND the extra first-name tokens.
    Returns `(tighter_regex, canonical_name, prominence)` or None.

    Used when alias path can't auto-resolve to a single author but
    the user supplied enough tokens to pick one canonical from
    the surname's candidate set.
    """
    m = re.fullmatch(r"\^([A-Za-zЀ-ӿ' -]+),", regex)
    if not m:
        return None
    surname = m.group(1)
    surname_lc = surname.lower()
    q_tokens = [t.strip(".,") for t in q_lc.split()]
    extra_tokens = [t for t in q_tokens if t and t.lower() != surname_lc]
    if not extra_tokens:
        return None
    idx = get_prominence_index()
    by_canonical = idx.get("by_canonical", {}) if isinstance(idx, dict) else {}
    if not by_canonical:
        return None
    surname_prefix = f"{surname},"
    best: tuple[int, int, str, dict] | None = None
    for name, ent in by_canonical.items():
        if not name.startswith(surname_prefix):
            continue
        name_lc = name.lower()
        score = sum(1 for t in extra_tokens if t and t in name_lc)
        if score < 1:
            continue
        dl = int(ent.get("downloads", 0))
        if best is None or score > best[0] or (
                score == best[0] and dl > best[1]):
            best = (score, dl, name, ent)
    if best is None:
        return None
    _, _, name, ent = best
    parts = name.split(",", 1)
    if len(parts) != 2:
        return None
    first_token = parts[1].strip().split(" ", 1)[0].rstrip(".")
    if not first_token:
        return None
    tighter = f"^{surname}, {first_token}"
    return tighter, name, ent


def _specialize_surname_to_dominant(
    regex: str, *, dominance_ratio: float = 5.0,
) -> tuple[str | None, str | None, dict]:
    """Pick the most prominent canonical author whose name matches `regex`.

    Returns `(tightened_regex, display_name, prominence_dict)` if a
    dominant winner exists, else `(None, None, {})`. Dominance means
    the top canonical has ≥ `dominance_ratio` × runner-up downloads
    (or the runner-up has 0).
    """
    m = re.fullmatch(r"\^([A-Za-zЀ-ӿ' -]+),", regex)
    if not m:
        return None, None, {}
    surname = m.group(1)
    idx = get_prominence_index()
    by_canonical = idx.get("by_canonical", {}) if isinstance(idx, dict) else {}
    if not by_canonical:
        return None, None, {}
    surname_prefix = f"{surname},"
    matches = [(name, ent) for name, ent in by_canonical.items()
               if name.startswith(surname_prefix)]
    if len(matches) < 2:
        return None, None, {}
    matches.sort(key=lambda x: x[1].get("downloads", 0), reverse=True)
    top_name, top_ent = matches[0]
    runner_dl = matches[1][1].get("downloads", 0)
    top_dl = top_ent.get("downloads", 0)
    if top_dl == 0:
        return None, None, {}
    if runner_dl > 0 and (top_dl / runner_dl) < dominance_ratio:
        return None, None, {}
    parts = top_name.split(",", 1)
    if len(parts) != 2:
        return None, None, {}
    first_token = parts[1].strip().split(" ", 1)[0].rstrip(".")
    if not first_token:
        return None, None, {}
    tighter = f"^{surname}, {first_token}"
    return tighter, top_name, top_ent


def _candidates_from_alias(q_lc: str) -> list[Candidate]:
    """Curated alias hit — at most one candidate, score 100.

    B-R17-1 stage3.2: when the curated alias is a bare surname matching
    multiple canonicals, pick the most prominent specific author and
    tighten the regex to match only that one (if dominance ≥ 5×).
    """
    try:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
    except ImportError:
        return []
    regex = AUTHOR_ALIASES.get(q_lc)
    alias_hit_via_surname_fallback = False
    if not regex:
        parts = q_lc.split()
        if len(parts) >= 2:
            for k in (parts[-1], f"{parts[0]} {parts[-1]}"):
                regex = AUTHOR_ALIASES.get(k)
                if regex:
                    alias_hit_via_surname_fallback = (k == parts[-1])
                    break
    if not regex:
        return []
    prom = prominence_for(regex)
    if alias_hit_via_surname_fallback:
        first_match = _match_canonical_by_tokens(q_lc, regex)
        if first_match is not None:
            specific_regex, specific_display, specific_prom = first_match
            return [Candidate(
                key=specific_regex,
                display=specific_display,
                score=100,
                source="alias_curated",
                prominence=specific_prom.get("downloads", 0),
                books_in_corpus=specific_prom.get("books", 0),
            )]
        return []
    specialized_regex, specialized_display, specialized_prom = (
        _specialize_surname_to_dominant(regex)
    )
    if specialized_regex is not None:
        return [Candidate(
            key=specialized_regex,
            display=specialized_display,
            score=100,
            source="alias_curated",
            prominence=specialized_prom.get("downloads", 0),
            books_in_corpus=specialized_prom.get("books", 0),
        )]
    return [Candidate(
        key=regex,
        display=prom.get("canonical_first") or _regex_to_display(regex),
        score=100,
        source="alias_curated",
        prominence=prom.get("downloads", 0),
        books_in_corpus=prom.get("books", 0),
    )]


def _candidates_from_corpus_fuzzy(
    q_lc: str, *, limit: int = 8, min_score: int = 60,
) -> list[Candidate]:
    """Fuzzy match against the corpus author list.

    Uses rapidfuzz WRatio if installed; falls back to substring scan.
    Each candidate gets per-canonical prominence (B-R17-1 stage3.2).
    """
    try:
        from scripts.rag_tools import _metadata_df
        df = _metadata_df()
    except Exception:
        return []
    if df is None or "author" not in df.columns:
        return []
    authors_series = df["author"].dropna().astype(str)
    if authors_series.empty:
        return []
    authors = authors_series.unique().tolist()

    rf = _try_rapidfuzz()
    raw: list[tuple[str, int]] = []
    if rf is not None:
        process, fuzz = rf
        try:
            matches = process.extract(q_lc, authors, scorer=fuzz.WRatio,
                                       limit=limit)
            raw = [(choice, int(score)) for choice, score, _ in matches
                   if int(score) >= min_score]
        except Exception:
            raw = []
    else:
        for a in authors:
            score = _simple_token_score(q_lc, a)
            if score >= min_score:
                raw.append((a, score))
        raw.sort(key=lambda x: -x[1])
        raw = raw[:limit]

    out: list[Candidate] = []
    for choice, score in raw:
        surname = choice.split(",", 1)[0].strip()
        if not surname:
            continue
        regex = f"^{surname},"
        prom = prominence_for_canonical(choice)
        out.append(Candidate(
            key=regex,
            display=choice,
            score=score,
            source="fuzzy",
            prominence=prom.get("downloads", 0),
            books_in_corpus=prom.get("books", 0),
        ))
    return out


__all__ = [
    "_try_rapidfuzz",
    "_simple_token_score",
    "_regex_to_display",
    "_match_canonical_by_tokens",
    "_specialize_surname_to_dominant",
    "_candidates_from_alias",
    "_candidates_from_corpus_fuzzy",
]
