"""v6 main entry — `resolve_v6(query) → ResolverDecision`.

Plus `to_resolve_result(decision)` adapter so v5 callers get the same
`ResolveResult` shape they expect (backwards-compat).

Pipeline:
  query → normalize → ru_lemmatize → detect_mentions → for primary
  mention: generate_candidates → score_candidates → decide
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scripts.v2.entity_resolver_v6.candidates import generate_candidates
from scripts.v2.entity_resolver_v6.decision import decide
from scripts.v2.entity_resolver_v6.mentions import detect_mentions
from scripts.v2.entity_resolver_v6.scoring import score_candidates
from scripts.v2.entity_resolver_v6.types import (
    Decision,
    Mention,
    ResolverDecision,
)

if TYPE_CHECKING:
    from scripts.v2.entity_resolver import ResolveResult

log = logging.getLogger("wordcracker.v2.entity_resolver_v6")


def resolve_v6(query: str) -> ResolverDecision:
    """Top-level v6 resolver entry.

    Returns a ResolverDecision (typed). For v5-compatible output, wrap
    with `to_resolve_result(decision)`.
    """
    if not query or not query.strip():
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            reason="empty query",
        )

    # Reuse v5 normalization (NFKC + lowercase + RU lemmatize)
    try:
        from scripts.v2.entity_resolver import (
            normalize_query,
            ru_lemmatize_author_query,
        )
    except ImportError as e:
        log.warning("v6 cannot import v5 normalization: %s", e)
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            reason=f"v5 import failed: {e}",
        )

    norm = normalize_query(query)
    q_lc = norm.output
    q_lem, _ = ru_lemmatize_author_query(q_lc)
    if q_lem != q_lc:
        q_lc = q_lem

    # Stage 1 — Mention Detection
    mentions = detect_mentions(q_lc)
    if not mentions:
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            reason="no mentions detected",
        )

    # Primary mention = first by span (sorted in detect_mentions)
    primary = mentions[0]

    # Stage 2 — Candidate Generation
    candidates = generate_candidates(primary)

    # Stage 3 — Scoring
    scored = score_candidates(primary, candidates)

    # Stage 4 — Decision
    return decide(primary, scored)


def resolve_v6_for_alias(
    query: str,
    alias_key: str,
    regex: str,
) -> ResolverDecision:
    """Refinement entry — v5 has already extracted the primary author
    via `_find_authors`. v6 only decides whether to resolve to a
    specific canonical, clarify, or accept the alias as-is.

    This avoids v6 re-analysing the whole query (which would mis-pick
    secondary authors in multi-author queries like «Мелвилла, Конрад
    и Стивенсон»).

    Pipeline:
      1. Build a Mention from alias_key + regex, extracting extra_tokens
         from query context around the alias_key
      2. Stage 2 generate_candidates (by_canonical filter by surname)
      3. Stage 3 score_candidates (using extra_tokens for token_overlap)
      4. Stage 4 decide

    Returns ResolverDecision.
    """
    if not query or not alias_key or not regex:
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            reason="empty input",
        )

    try:
        from scripts.v2.entity_resolver import (
            normalize_query,
            ru_lemmatize_author_query,
        )
    except ImportError:
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            reason="v5 import failed",
        )

    norm = normalize_query(query)
    q_lc = norm.output
    q_lem, _ = ru_lemmatize_author_query(q_lc)
    if q_lem != q_lc:
        q_lc = q_lem

    # Determine mention type from alias_key + regex shape
    from scripts.v2.entity_resolver_v6.types import Mention, MentionType
    mention_type = _classify_mention(alias_key, regex, q_lc)

    # Compute extra_tokens: query tokens adjacent to alias_key,
    # excluding alias_key itself and OTHER known aliases (to avoid
    # picking up secondary authors as first-name tokens).
    extra_tokens = _extract_extra_tokens(q_lc, alias_key)

    mention = Mention(
        surface=alias_key,
        type=mention_type,
        span=(0, len(alias_key)),  # span not meaningful here
        alias_key=alias_key,
        regex=regex,
        extra_tokens=extra_tokens,
    )

    candidates = generate_candidates(mention)
    scored = score_candidates(mention, candidates)
    return decide(mention, scored)


def _classify_mention(
    alias_key: str, regex: str, q_lc: str,
) -> "MentionType":
    """Best-effort classify mention type from alias_key + regex shape.

    Used by resolve_v6_for_alias when v5 already extracted the alias.
    """
    from scripts.v2.entity_resolver_v6.types import MentionType
    import re as _re

    # Canonical format (regex shape): regex contains comma + first name
    if _re.match(r"^\^[A-Za-zЀ-ӿ' -]+,\s+\w", regex):
        return MentionType.CANONICAL_FORMAT

    # Canonical format (query shape): query has «alias_key, FirstName»
    # pattern — user wrote it themselves, alias_key was extracted as
    # bare surname by v5. Check the query for «{alias_key},\s+\w+».
    idx = q_lc.find(alias_key)
    if idx >= 0:
        after_alias = q_lc[idx + len(alias_key):]
        if _re.match(r"^,\s+[a-zа-яё][a-zа-яё. ]+", after_alias):
            return MentionType.CANONICAL_FORMAT

    # Alias hit: alias_key contains a space (multi-word)
    if " " in alias_key:
        return MentionType.ALIAS_HIT
    # RU stem: alias_key has Cyrillic
    if any("а" <= c <= "я" or "А" <= c <= "Я" or c in "ёЁ"
           for c in alias_key):
        return MentionType.RU_STEM
    # Otherwise: surname-only OR full_name (if first-name token present)
    if idx > 0:
        prefix = q_lc[:idx].rstrip().split()
        if prefix:
            prev_token = prefix[-1].strip(",.;:!?'\"")
            if (len(prev_token) >= 2 and prev_token.isalpha()
                    and prev_token not in _NON_FIRST_NAME_TOKENS):
                return MentionType.FULL_NAME
    return MentionType.SURNAME_ONLY


def _extract_extra_tokens(q_lc: str, alias_key: str) -> tuple[str, ...]:
    """Find tokens in q_lc that look like first-name context for alias_key.

    Looks BOTH preceding AND following the alias_key:
      - preceding tokens (full_name pattern): «Christopher Marlowe»
      - following tokens after comma (canonical-format pattern):
        «Doyle, Arthur Conan»

    Returns lowercase tokens (max 3) excluding:
      - the alias_key itself
      - other known aliases (avoid picking secondary authors as
        first-name context)
      - common stopwords / prepositions / verbs
    """
    try:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
    except ImportError:
        return ()

    idx = q_lc.find(alias_key)
    if idx < 0:
        return ()

    extras: list[str] = []

    # Side A — preceding tokens (full_name pattern)
    prefix = q_lc[:idx].rstrip()
    if prefix:
        raw_tokens = prefix.split()
        for tok in reversed(raw_tokens[-3:]):
            clean = tok.strip(",.;:!?'\"").lower()
            if not clean or len(clean) < 2:
                break
            if clean in _NON_FIRST_NAME_TOKENS:
                break
            if clean in AUTHOR_ALIASES:
                break
            if not all(c.isalpha() or c in ".'" for c in clean):
                break
            extras.append(clean)
        extras.reverse()

    # Side B — following tokens after comma (canonical-format pattern)
    # Look for «{alias_key}, FirstName Middle...»
    after = q_lc[idx + len(alias_key):]
    import re as _re
    m = _re.match(r"^,\s+([a-zа-яё][a-zа-яё. ]+?)(?=[,.;:!?]|$)", after)
    if m:
        for tok in m.group(1).split()[:3]:
            clean = tok.strip(",.;:!?'\"").lower()
            if not clean or len(clean) < 2:
                continue
            if clean in _NON_FIRST_NAME_TOKENS:
                continue
            if clean in AUTHOR_ALIASES:
                break
            if not all(c.isalpha() or c in ".'" for c in clean):
                continue
            if clean not in extras:
                extras.append(clean)

    return tuple(extras[:3])


_NON_FIRST_NAME_TOKENS = frozenset({
    "и", "а", "но", "у", "в", "на", "от", "из", "к", "с", "о",
    "что", "как", "почему", "зачем", "когда", "где", "куда",
    "сравни", "напиши", "покажи", "дай", "найди", "слова",
    "слов", "книги", "книг", "автор", "авторы", "автора",
    "написал", "написала", "написали",
    "by", "of", "and", "or", "the", "a", "an", "from", "to",
    "books", "author", "authors", "compare", "show", "give",
    "find", "wrote", "works",
    # Punctuation/separators commonly between authors
    "—", "-", ":", ";",
})


def to_resolve_result(d: ResolverDecision, raw_query: str) -> "ResolveResult":
    """Adapt v6 ResolverDecision → v5 ResolveResult for backwards-compat.

    v5 contract (used by rag_v2, plan builders, tool wrappers):
        ResolveResult(decision: str, resolved: dict|None, confidence: float,
                      candidates: list[Candidate], normalization_trace: list[str],
                      query_raw: str, query_normalized: str,
                      confidence_reason: str)
    """
    try:
        from scripts.v2.entity_resolver import (
            normalize_query, ru_lemmatize_author_query, ResolveResult,
        )
    except ImportError:
        return None  # type: ignore

    norm = normalize_query(raw_query)
    q_lc = norm.output
    q_lem, ru_trace = ru_lemmatize_author_query(q_lc)
    full_trace = list(norm.steps) + ru_trace

    # Map v6 Decision enum to v5 string literal
    decision_str = {
        Decision.RESOLVED: "resolved",
        Decision.CLARIFY_NEEDED: "clarify_needed",
        Decision.NOT_FOUND: "not_found",
    }[d.decision]

    resolved_dict = None
    if d.resolved is not None:
        resolved_dict = {
            "author_regex": d.resolved.key,
            "display": d.resolved.display,
            "prominence": d.resolved.prominence,
            "books_in_corpus": d.resolved.books_in_corpus,
            "source": d.resolved.source,
        }

    cand_list = (
        [s.candidate for s in d.all_scores[:5]]
        if d.all_scores
        else d.clarify_candidates
    )

    return ResolveResult(
        decision=decision_str,
        resolved=resolved_dict,
        confidence=d.confidence,
        candidates=cand_list,
        normalization_trace=full_trace,
        query_raw=raw_query,
        query_normalized=q_lem,
        confidence_reason=d.reason,
    )
