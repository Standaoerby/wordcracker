"""v6 Stage 1 — Mention Detection.

Identify all entity references in a normalized query, classify each by
type (canonical_format, full_name, alias_hit, ru_stem, surname_only),
and capture extra disambiguating tokens.

The output drives Stage 3 scoring — different mention types get different
scoring weights (e.g. FULL_NAME emphasizes token_overlap, SURNAME_ONLY
emphasizes prominence_prior).

Detection precedence (highest wins overlapping spans):
  1. CANONICAL_FORMAT  — explicit «Surname, First» with comma
  2. ALIAS_HIT         — multi-word alias matches («h. g. wells»)
  3. FULL_NAME         — [first_token] [surname-alias] adjacent
  4. RU_STEM           — single Russian declension form
  5. SURNAME_ONLY      — single-token surname alias

This precedence ensures the richest mention type wins for any text span.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from scripts.v2.entity_resolver_v6.types import Mention, MentionType

if TYPE_CHECKING:
    pass


def detect_mentions(q_normalized: str) -> list[Mention]:
    """Return ordered list of mentions found in `q_normalized`.

    `q_normalized` is assumed lowercase, NFKC-folded, RU-lemmatized
    (already passed through `normalize_query` + `ru_lemmatize_author_query`
    from v5 entity_resolver). Detection is case-insensitive on this input.

    Empty list when no mentions detected.
    """
    if not q_normalized or not q_normalized.strip():
        return []

    # Lazy import to avoid circular dep at module load
    try:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
    except ImportError:
        return []

    all_mentions: list[Mention] = []

    # Stage 1a — CANONICAL_FORMAT (highest precedence)
    # Pattern: «Surname, First [Middle...]» — surname is a SINGLE token
    # (no spaces), then comma, then space, then first-name token(s).
    # CRITICAL: surname MUST be a known alias — otherwise commas in
    # ordinary RU text («слова, которые») masquerade as canonical format
    # and pollute author extraction. R-22 fix.
    for m in _CANONICAL_FORMAT_RE.finditer(q_normalized):
        surname_part = m.group(1).strip()
        rest_part = m.group(2).strip()
        if not surname_part or not rest_part:
            continue
        # Reject if surname has any whitespace — accidental match
        if " " in surname_part:
            continue
        # Require surname to be a known alias — gates against false
        # positives like «слова, которые»
        alias_key = surname_part
        regex = AUTHOR_ALIASES.get(alias_key)
        if not regex:
            continue  # not an actual author surname — skip
        # extra_tokens = first/middle name tokens
        extra = tuple(t.strip(".,") for t in rest_part.split() if t.strip(".,"))
        all_mentions.append(Mention(
            surface=m.group(0),
            type=MentionType.CANONICAL_FORMAT,
            span=m.span(),
            alias_key=alias_key,
            regex=regex,
            extra_tokens=extra,
        ))

    # Stage 1b — ALIAS_HIT (multi-word aliases)
    # Iterate AUTHOR_ALIASES, find multi-word keys present in q_normalized.
    # extra_tokens extracted from the key (all tokens minus the surname
    # token which is also a single-word alias). Lets Stage 3 disambiguate
    # «christopher marlowe» → Marlowe, Christopher via token_overlap.
    multi_word_keys = [k for k in AUTHOR_ALIASES if " " in k]
    # Sort longest first so «h. g. wells» wins over «wells»
    multi_word_keys.sort(key=len, reverse=True)
    for key in multi_word_keys:
        idx = q_normalized.find(key)
        if idx < 0:
            continue
        span = (idx, idx + len(key))
        # Skip if already covered by a higher-precedence mention
        if _span_overlaps(span, all_mentions):
            continue
        # Extract extra tokens: all key tokens that aren't the surname-only
        # alias themselves. E.g. «christopher marlowe» surname is «marlowe»
        # (a single-word alias), so extra = («christopher»,).
        key_tokens = [t for t in key.split() if t]
        single_word_aliases = {k for k in AUTHOR_ALIASES if " " not in k}
        # Surname is whichever token is itself a single-word alias
        # mapping to the SAME regex. If multiple match, pick last (most
        # likely the surname position in Western naming).
        target_regex = AUTHOR_ALIASES[key]
        surname_token = None
        for tok in reversed(key_tokens):
            if (tok in single_word_aliases
                    and AUTHOR_ALIASES.get(tok) == target_regex):
                surname_token = tok
                break
        if surname_token is None:
            extra = ()
        else:
            extra = tuple(t for t in key_tokens if t != surname_token)
        all_mentions.append(Mention(
            surface=key,
            type=MentionType.ALIAS_HIT,
            span=span,
            alias_key=key,
            regex=target_regex,
            extra_tokens=extra,
        ))

    # Stage 1c — FULL_NAME (first_token + surname-alias adjacent)
    # Pattern: [word] [word-which-is-surname-alias]
    # Walk tokens, for each pair check if second token is a single-word alias
    # AND first token isn't itself an alias / preposition.
    # Reuse v5 _AMBIGUOUS_SHORT_ALIASES (contains «по» which doubles as
    # preposition) — never treat ambiguous shorts as FULL_NAME surname-tok.
    try:
        from scripts.v2.planner.entities import _AMBIGUOUS_SHORT_ALIASES
    except ImportError:
        _AMBIGUOUS_SHORT_ALIASES = frozenset()
    single_word_keys = {k for k in AUTHOR_ALIASES if " " not in k}
    tokens_with_spans = list(_tokenize_with_spans(q_normalized))
    for i in range(len(tokens_with_spans) - 1):
        tok_first, span_first = tokens_with_spans[i]
        tok_second, span_second = tokens_with_spans[i + 1]
        # Second token must be a surname alias
        if tok_second not in single_word_keys:
            continue
        # Second token must NOT be an ambiguous short alias (e.g. «по»
        # which is also a Russian preposition)
        if tok_second in _AMBIGUOUS_SHORT_ALIASES:
            continue
        # First token must NOT be itself an alias (avoid double-counting
        # «По Лавкрафта» from picking up «По» as first_token)
        if tok_first in single_word_keys:
            continue
        # First token must look like a name (≥2 chars, alphabetic, not
        # a common preposition/conjunction).
        if not _looks_like_first_name(tok_first):
            continue
        # Combined span from first to second
        span = (span_first[0], span_second[1])
        if _span_overlaps(span, all_mentions):
            continue
        all_mentions.append(Mention(
            surface=q_normalized[span[0]:span[1]],
            type=MentionType.FULL_NAME,
            span=span,
            alias_key=tok_second,
            regex=AUTHOR_ALIASES[tok_second],
            extra_tokens=(tok_first,),
        ))

    # Stage 1d — RU_STEM
    # RU stems like «толстого» (RU genitive of «толстой») are already in
    # AUTHOR_ALIASES (via curated dict). They appear here as SURNAME_ONLY
    # unless we explicitly classify them. For now, distinguish by checking
    # if the alias-key contains Cyrillic letters.
    # (No special action required vs SURNAME_ONLY in current model — RU
    # stems and bare English surnames both lack first-name context.
    # Kept as a separate type for future Stage 5 / scoring tweaks.)

    # Stage 1e — SURNAME_ONLY / RU_STEM (single-word alias hits)
    # Reuse v5's preposition collision guard so «по Wodehouse» doesn't
    # extract «по» as Poe author. Ambiguous short aliases like «по» get
    # context-checked.
    try:
        from scripts.v2.planner.entities import _is_preposition_collision
    except ImportError:
        _is_preposition_collision = None
    for key in single_word_keys:
        # Find ALL occurrences in q_normalized
        start = 0
        while True:
            idx = q_normalized.find(key, start)
            if idx < 0:
                break
            # Ensure word boundary (not «wells» inside «wellsboro»)
            j = idx + len(key)
            if not _is_word_boundary(q_normalized, idx, j):
                start = j
                continue
            span = (idx, j)
            start = j
            if _span_overlaps(span, all_mentions):
                continue
            # «по» preposition guard — skip when context says preposition
            if _is_preposition_collision is not None:
                try:
                    if _is_preposition_collision(q_normalized, idx, j, key):
                        continue
                except Exception:
                    pass
            # Determine RU_STEM vs SURNAME_ONLY by alphabet
            mtype = (MentionType.RU_STEM if _is_cyrillic(key)
                     else MentionType.SURNAME_ONLY)
            all_mentions.append(Mention(
                surface=key,
                type=mtype,
                span=span,
                alias_key=key,
                regex=AUTHOR_ALIASES[key],
                extra_tokens=(),
            ))

    # Sort by span start, then by precedence (CANONICAL > ALIAS > FULL >
    # RU > SURNAME). Within same precedence, earlier wins.
    all_mentions.sort(key=lambda m: (m.span[0], _PRECEDENCE[m.type]))

    return all_mentions


# ============================ helpers ============================

_CANONICAL_FORMAT_RE = re.compile(
    # Surname: 2+ letters NO SPACES (single token), followed by comma
    # First name(s): letters + dots/spaces (e.g. "H. G." with two initials).
    # Lookahead doesn't break on dot — allows "h. g." capture; breaks on
    # other punctuation/end-of-string.
    r"\b([a-zа-яёA-ZА-ЯЁ']{2,}),\s+([a-zа-яёA-ZА-ЯЁ][a-zа-яёA-ZА-ЯЁ. ]+?)"
    r"(?=[\s,;:!?]|$|\s*$)"
)


# Lower number = higher precedence
_PRECEDENCE = {
    MentionType.CANONICAL_FORMAT: 0,
    MentionType.ALIAS_HIT: 1,
    MentionType.FULL_NAME: 2,
    MentionType.RU_STEM: 3,
    MentionType.SURNAME_ONLY: 4,
}


# Words that often appear before surnames but aren't first names —
# prepositions, particles, verb forms.
_NON_FIRST_NAME_TOKENS = frozenset({
    "и", "а", "но", "у", "в", "на", "от", "из", "к", "с", "о",
    "что", "как", "почему", "зачем", "когда", "где", "куда",
    "сравни", "напиши", "покажи", "дай", "найди", "слова",
    "слов", "книги", "книг", "автор", "авторы", "автора",
    "написал", "написала", "написали",
    "by", "of", "and", "or", "the", "a", "an", "from", "to",
    "books", "author", "authors", "compare", "show", "give",
    "find", "wrote", "works",
})


def _looks_like_first_name(tok: str) -> bool:
    """Heuristic: is `tok` a plausible first-name token?

    Requirements:
      - 2+ characters
      - alphabetic (letters + maybe dots / apostrophes)
      - not in stop-word / verb list
    """
    if len(tok) < 2:
        return False
    if tok in _NON_FIRST_NAME_TOKENS:
        return False
    # Allow letters, dots (initials), apostrophes
    return all(c.isalpha() or c in ".'" for c in tok)


def _is_cyrillic(s: str) -> bool:
    """True if any char is Cyrillic."""
    return any("а" <= c <= "я" or "А" <= c <= "Я" or c in "ёЁ" for c in s)


def _is_word_boundary(s: str, i: int, j: int) -> bool:
    """True iff position i..j is word-bounded in s.

    Left boundary: i == 0 or s[i-1] is not alphanumeric.
    Right boundary: j == len(s) or s[j] is not alphanumeric.
    """
    left_ok = (i == 0) or (not s[i - 1].isalpha())
    right_ok = (j >= len(s)) or (not s[j].isalpha())
    return left_ok and right_ok


def _tokenize_with_spans(s: str):
    """Yield (token, (start, end)) for each whitespace-separated token.

    Strips punctuation from token edges but keeps span on original.
    """
    i = 0
    n = len(s)
    while i < n:
        # Skip whitespace + punctuation
        while i < n and not s[i].isalpha():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and (s[j].isalpha() or s[j] in ".'"):
            j += 1
        tok = s[i:j].strip(".,;:!?'")
        if tok:
            yield tok.lower(), (i, j)
        i = j


def _span_overlaps(span: tuple[int, int],
                    existing: list[Mention]) -> bool:
    """True iff `span` overlaps with any mention already in `existing`."""
    a, b = span
    for m in existing:
        c, d = m.span
        # Overlap test: not (a >= d or b <= c)
        if not (a >= d or b <= c):
            return True
    return False
