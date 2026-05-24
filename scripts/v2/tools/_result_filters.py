"""Unified result post-processing layer.

Sprint 20+ (Stan 2026-05-20 «делай большой архитектурный апдейт»):
Round 11 external Claude test surfaced 8 hygiene bugs across multiple
tools. Before this module each wrapper had its own ad-hoc filters
(surname blocklist, corpus_artifacts, _drop_author_self_name, ...) —
duplicated code, easy to forget one. This module is the central place.

What it filters
---------------
Each function takes a list of row-dicts and returns (filtered_rows,
dropped_count). Composable via `apply_filters([fn, fn, ...], rows)`.

1. drop_null_authors(rows, *, author_key='author')
   B5 — top_authors_by tokens returned NaN-author as #1 with 204M tokens.
   Drops rows where author is None / '' / 'NaN' / 'nan' / 'null'.

2. drop_iso_language_codes(rows, *, word_key='word' or path into nested)
   B7 — enrich_word returns 'ang' / 'gem-pro' / 'ine-pro' as «related
   forms». These are ISO-639 language codes, not words. Match pattern
   `^[a-z]{2,3}(-[a-z]+)?$` length ≤ 12 with hyphen present or known
   short language code stem.

3. dedup_by_key(rows, *, key='snippet')
   B17, B18 — multi-author word_contexts and topic_book_search returned
   identical snippets under different PG ids. Generic dedup by normalized
   key (snippet text hashed, PG id stripped of trailing punctuation).

4. dedup_book_editions(rows, *, title_key='title', id_key='id')
   B10 + B21 — «Moby Dick» (PG2701) + «Moby Dick; Or, The Whale»
   (PG2489) both surface for same query. Twain count 211 because of
   multi-edition entries. Normalizes title (strip subtitle after «;» or
   «,», lowercase, drop articles), groups by (author, normalized_title),
   keeps highest-downloads row per group.

5. cap_corpus_artifacts_short(rows, *, word_key='word')
   B19 — extends existing `corpus_artifacts` filter: tighten consonant-
   only check from len ≥ 3 to len ≥ 2 so OCR fragments like 'th' get
   caught. Roman-numeral / single-char rules unchanged.

Architecture
------------
Tool wrappers compose these via the `apply_filters` helper:

    from scripts.v2.tools._result_filters import (
        drop_null_authors, dedup_book_editions, apply_filters,
    )
    rows, dropped_map = apply_filters([
        drop_null_authors,
        lambda r: dedup_book_editions(r, title_key='title'),
    ], rows)

The wrapper logs `dropped_map = {'drop_null_authors': 2,
'dedup_book_editions': 5}` into `raw["_filter_drops"]` so the renderer
can mention them and the admin dashboard can see filter rates.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Tuple

log = logging.getLogger("wordcracker.v2.tools._result_filters")


# ---------- null authors ----------

_NULL_AUTHOR_TOKENS = frozenset({
    "", "nan", "null", "none", "n/a", "na", "unknown",
    # Generic stand-ins that survive metadata cleaning
    "anonymous", "various", "(anonymous)", "(various)",
    # Sprint 22+ Round 12 minor — institutional/agency «authors» that
    # are aggregate buckets, not real writers. Stan Q1: CIA showed up
    # as #3 in top_authors_by(metric=tokens) — millions of declassified
    # PDFs released to Internet Archive collapse under one «author»
    # field but it's not literary.
    "central intelligence agency", "cia",
    "united states", "u.s. government", "us government",
    "great britain", "uk government",
    "library of congress",
    "internet archive",
    "various authors", "miscellaneous",
    # Phase 3 W-4 (Stan 2026-05-22) — top-lists of authors were
    # dominated by US-government / commission «authors»: Warren
    # Commission report, congressional testimony bundles, FBI files,
    # NASA technical papers. None are «авторы» in the literary sense
    # the user means by «топ авторов».
    "president's commission on the assassination of president kennedy",
    "warren commission",
    "u.s. congress", "us congress", "congress",
    "federal bureau of investigation", "fbi",
    "national aeronautics and space administration", "nasa",
    "u.s. department of state", "us department of state",
    "u.s. department of justice", "department of justice",
    "u.s. navy", "u.s. army", "u.s. air force", "u.s. marine corps",
    "us navy", "us army", "us air force",
    "british army", "royal navy",
    "smithsonian institution", "smithsonian",
    "national park service",
    "u.s. supreme court", "supreme court",
    "house of representatives",
    "u.s. senate", "us senate", "senate",
    # Project Gutenberg housekeeping artifacts that occasionally
    # surface as «authors» when metadata join misfires.
    "project gutenberg", "gutenberg",
    "project gutenberg literary archive foundation",
    # Common publisher / society aggregate entries
    "publishers' weekly", "publisher's weekly",
    "american mathematical society",
    "royal society",
    "national geographic society",
    # W-4 reconciliation (2026-05-24) — additional aggregate buckets
    # that surface in token-volume topcharts:
    "world bank",
    "united nations",
    "u.s. patent office", "us patent office", "patent office",
    "smithsonian astrophysical observatory",
    "national bureau of standards",
    "geological survey",
    "u.s. geological survey", "us geological survey",
    "american library association",
    "modern language association",
    "encyclopaedia britannica",  # also caught by «encyclop» substring
    "various", "various contributors",
    "editors of the encyclopaedia britannica",
    "compiled",                  # «Compiled, » author cell from PG
    "selected",                  # «Selected, » prefix
    "translator",                # rare «Translator, » metadata noise
})

# Phase 3 W-4 — substrings that, if present anywhere in the author field,
# flag the row as a non-literary aggregate. PG metadata often joins the
# parent agency before the commission name, e.g.
#     "United States. Warren Commission"
#     "United States. Congress. House Committee on …"
#     "United States. National Aeronautics and Space Administration"
# Set-membership against the full string misses these — substring check
# catches them deterministically.
_NULL_AUTHOR_SUBSTRINGS: tuple[str, ...] = (
    "warren commission",
    "congressional",
    "congress.",
    "house committee",
    "senate committee",
    "department of state",
    "department of justice",
    "department of war",
    "central intelligence",
    "national aeronautics",
    "library of congress",
    "smithsonian",
    "naval academy",
    "war department",
    "supreme court of the united",
    "house of representatives",
    "department of the navy",
    "department of the army",
    "department of agriculture",
    "department of commerce",
    "department of the interior",
    "bureau of investigation",
    "bureau of naval personnel",
    "office of strategic",
    # Generic publisher-aggregate buckets that masquerade as authors
    "various authors", "anonymous",
    # W-4 reconciliation (2026-05-24) — additional substrings observed
    # in PG metadata aggregates that escape the token set:
    "literary archive foundation",
    "patent office",
    "geological survey",
    "office of the secretary",
    "national bureau",
    "encyclopaedia",     # «Editors of the Encyclopaedia Britannica»
    "encyclopedia",
    "joint chiefs of staff",
    "bureau of the census",
    "census bureau",
)


def drop_null_authors(rows: list[dict], *,
                       author_key: str = "author") -> Tuple[list[dict], int]:
    """B5 + Phase 3 W-4 — drop rows where author is null/NaN/anonymous-like
    OR matches a non-literary aggregate (commission / agency / department).

    `top_authors_by(metric=tokens)` returned a NaN author as #1 with
    204M tokens because the metadata join produced a null cell that
    the SQL aggregate happily summed. W-4 extends the filter to catch
    US-government commission and agency «authors» (CIA, Warren
    Commission, Library of Congress) that dominate token-volume topcharts.
    """
    if not rows:
        return rows, 0
    out: list[dict] = []
    for r in rows:
        v = r.get(author_key) if isinstance(r, dict) else None
        if v is None:
            continue
        s = str(v).strip().lower()
        if not s or s in _NULL_AUTHOR_TOKENS:
            continue
        if any(sub in s for sub in _NULL_AUTHOR_SUBSTRINGS):
            continue
        out.append(r)
    return out, len(rows) - len(out)


# ---------- ISO language codes ----------

# Common ISO-639-2/3 / Wiktionary language codes seen in word_etymology
# «related forms» output. Not exhaustive — but covers what we've seen
# leak in production.
_ISO_LANG_CODES = frozenset({
    "ang", "ang-old", "ang-mid", "enm", "enm-wmi",
    "gmw-pro", "gmw", "gem-pro", "gem",
    "ine-pro", "ine", "lat", "lat-cla",
    "fro", "frm", "fra", "fr",
    "got", "non", "osx", "ohg", "gmh", "goh",
    "grc", "grk", "ell", "ang-nor",
    "wmi", "swe", "deu", "nld", "rus", "spa",
})

# Pattern для unknown codes — 2-3 ASCII lowercase, optionally hyphenated
# with another short suffix. «en», «de», «la-vul», etc.
_ISO_CODE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,5})?$")


def looks_like_iso_code(token: str) -> bool:
    if not token:
        return False
    s = token.strip().lower()
    if not s:
        return False
    if s in _ISO_LANG_CODES:
        return True
    # 2-3 char or hyphenated — likely code, especially if all lowercase
    # ASCII alphanum. Vowel-less 2-char tokens caught (de, fr, la, ...).
    return bool(_ISO_CODE_RE.match(s))


def drop_iso_language_codes(rows: list[dict], *,
                              word_key: str = "word"
                              ) -> Tuple[list[dict], int]:
    """B7 — enrich_word / word_etymology «related forms» list mixes ISO-639
    language codes with actual word forms. Drop the codes.

    Conservative — only drops 2-3 char hyphenated tokens or those in
    the explicit `_ISO_LANG_CODES` set. A real word like «cat» (3 char)
    would match the regex; we keep it because vowels distinguish it
    from codes like «ang» / «gem». The regex above doesn't exclude
    vowelled 3-char strings, so we ALSO check curated set first.
    """
    if not rows:
        return rows, 0
    out: list[dict] = []
    for r in rows:
        token = r.get(word_key) if isinstance(r, dict) else None
        if not isinstance(token, str):
            out.append(r)
            continue
        s = token.strip().lower()
        # Explicit-set hits = drop. Pattern-only matches kept (avoid
        # dropping real words like «cat»/«dog»).
        if s in _ISO_LANG_CODES:
            continue
        # Hyphenated short tokens almost always codes (gmw-pro, ang-nor)
        if "-" in s and len(s) <= 12 and _ISO_CODE_RE.match(s):
            continue
        out.append(r)
    return out, len(rows) - len(out)


# ---------- generic dedup ----------


def dedup_by_key(rows: list[dict], *, key: str = "snippet",
                 normalize: Callable[[Any], str] | None = None
                 ) -> Tuple[list[dict], int]:
    """B17, B18 — multi-author word_contexts and topic_book_search
    repeat identical snippets across different PG ids. Generic dedup
    that keeps the first occurrence.

    `normalize` defaults to str.lower().strip() to absorb whitespace /
    case differences. Pass a custom normalizer for snippet-hash dedup.
    """
    if not rows:
        return rows, 0
    if normalize is None:
        def normalize(v: Any) -> str:
            return (str(v) if v is not None else "").strip().lower()
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        v = r.get(key) if isinstance(r, dict) else None
        if v is None:
            out.append(r)  # missing key → keep, don't punish
            continue
        norm = normalize(v)
        if not norm:
            out.append(r)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(r)
    return out, len(rows) - len(out)


# ---------- near-duplicate (overlapping-window) snippet dedup ----------

# Tokenizer for snippet shingles — keep alphanumerics, lowercase, split
# on everything else. Apostrophes inside words preserved so «don't»
# stays one token.
_SNIPPET_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9']+")


def _snippet_token_set(text: str) -> frozenset[str]:
    if not isinstance(text, str):
        return frozenset()
    toks = _SNIPPET_TOKEN_RE.findall(text.lower())
    # Drop very short / numeric-only tokens — they're noise (articles,
    # numbers) that inflate Jaccard scores on unrelated passages.
    return frozenset(t for t in toks if len(t) >= 3 and not t.isdigit())


def dedup_overlapping_snippets(
    rows: list[dict], *,
    key: str = "snippet",
    threshold: float = 0.45,
) -> Tuple[list[dict], int]:
    """W-6 (2026-05-24) — collapse near-duplicate snippets that overlap
    on the same source sentence.

    Stan prod 2026-05-22 «примеры heart у Дойла»: after dedup_by_key
    fired the exact dups, the surviving samples still contained pairs
    like ctx1 = «...his heart was heavy with the news he had to deliver»
    and ctx2 = «with the news he had to deliver. His heart pounded
    against his ribs» — two ±10-token windows on the *same paragraph*
    that the rag retriever surfaced separately because the windows
    happen to bracket the same target word from opposite sides.
    dedup_by_key (exact match) can't catch this; we need overlap.

    Token-set Jaccard ≥ `threshold` → treat as duplicate. Default 0.45
    was tuned empirically against Stan's prod windows: ±10-token
    windows around the same target on a 25-token sentence share ~50%
    content tokens (≈6 / 12) after short-stopword pruning. A lower
    bound at 0.45 catches that class without false-collapsing two
    genuinely different snippets that incidentally share «heart, the,
    his» (Jaccard ~0.1).

    Order is preserved; the FIRST occurrence wins (matches dedup_by_key
    semantics so downstream callers don't have to think about it).

    Tokens shorter than 3 chars and bare digits are excluded from the
    set so short stopwords/numeric noise don't drive the score.

    Skips rows missing the key (consistent with dedup_by_key).
    """
    if not rows:
        return rows, 0
    if threshold <= 0 or threshold > 1:
        # Defensive: out-of-range threshold disables the filter rather
        # than ValueError-ing inside a render-path helper.
        return rows, 0
    out: list[dict] = []
    kept_sets: list[frozenset[str]] = []
    for r in rows:
        v = r.get(key) if isinstance(r, dict) else None
        if not isinstance(v, str) or not v.strip():
            out.append(r)
            kept_sets.append(frozenset())
            continue
        tok = _snippet_token_set(v)
        if not tok:
            out.append(r)
            kept_sets.append(tok)
            continue
        duplicate = False
        for prev in kept_sets:
            if not prev:
                continue
            inter = len(tok & prev)
            if inter == 0:
                continue
            union = len(tok | prev)
            if union == 0:
                continue
            jacc = inter / union
            if jacc >= threshold:
                duplicate = True
                break
        if duplicate:
            continue
        out.append(r)
        kept_sets.append(tok)
    return out, len(rows) - len(out)


# ---------- book edition dedup ----------

# Articles & punctuation noise to strip before comparing book titles.
_TITLE_PREFIX_STRIP = re.compile(
    r"^(?:the|a|an|der|die|das|le|la|les|el|los|las|il)\s+",
    re.IGNORECASE,
)
_TITLE_SUFFIX_STRIP = re.compile(
    # Drop subtitles after « ; » or «, or » — W-17 extends the keyword
    # list with «for these times» (Hard Times) so the
    # «Hard Times — For These Times» edition collapses too.
    r"\s*[;,—–]\s*(?:or|and|with|illustrated|complete|the\s+real|"
    r"for\s+these\s+times|"
    r"abridged|annotated|critical|definitive|expanded|first|"
    r"original|revised|special|standard|unabridged|"
    r"a\s+(?:novel|tale|story|romance|history))\b.*$",
    re.IGNORECASE,
)
# W-17 (Phase 5 P2, 2026-05-23) — additional dedup forms that the
# original SUFFIX regex missed and which let Frankenstein leak as 5
# distinct PG ids and Hard Times as 2:
#   * parenthetical edition tags:  «Frankenstein (1818 edition)»
#                                   «Pride and Prejudice (Illustrated)»
#   * bare «Or» without «;»/«,»:  «Frankenstein Or The Modern Prometheus»
#                                  «Hard Times For These Times»
# Applied AFTER the legacy SUFFIX regex so the cheaper «;/,» path still
# wins when it already matches.
_TITLE_PARENS_STRIP = re.compile(r"\s*\([^)]*\)\s*$")
# Bare «Or <subtitle>» — needs at least one word after «Or» to be a
# real subtitle (not just a song lyric ending in «or»). «For These
# Times» trailing the canonical title is treated as a subtitle even
# without a following word.
_TITLE_BARE_OR_STRIP = re.compile(
    r"\s+or\s+(?:the\s+|a\s+)?[A-ZА-ЯЁa-z].*$|"
    r"\s+for\s+these\s+times\b.*$",
    re.IGNORECASE,
)


def _normalize_book_title(title: str) -> str:
    """«Moby Dick; Or, The Whale» → «moby dick». «The Adventures of
    Sherlock Holmes» → «adventures of sherlock holmes».

    W-17 — also catches:
      * «Frankenstein (1818 edition)»       → «frankenstein»
      * «Pride and Prejudice (Illustrated)» → «pride and prejudice»
      * «Frankenstein Or The Modern Prometheus» → «frankenstein»
      * «Hard Times For These Times»        → «hard times»
    """
    if not title:
        return ""
    s = str(title).strip()
    # Strip subtitles & «; Or, ...» suffixes
    s = _TITLE_SUFFIX_STRIP.sub("", s)
    # W-17 — strip parenthetical edition / illustrator / year tags
    # before lowercasing so the «(1818 edition)» regex anchors cleanly.
    s = _TITLE_PARENS_STRIP.sub("", s)
    # W-17 — strip bare «Or <subtitle>» / «For These Times» tails. The
    # bare-Or form lacks the leading «;» / «,», so the legacy regex
    # skipped it.
    s = _TITLE_BARE_OR_STRIP.sub("", s)
    # Lowercase
    s = s.lower()
    # Strip leading article
    s = _TITLE_PREFIX_STRIP.sub("", s)
    # Collapse whitespace
    s = " ".join(s.split())
    return s


def dedup_book_editions(rows: list[dict], *,
                         title_key: str = "title",
                         author_key: str = "author",
                         keep_key: str | None = "downloads",
                         ) -> Tuple[list[dict], int]:
    """B10, B21 — collapse multiple editions of the same book.

    Groups by (author, normalized_title). Keeps the row with the
    highest `keep_key` value (downloads by default) — that's usually
    the canonical / most-popular edition. If `keep_key` is None or
    missing, keeps the FIRST occurrence per group.

    Stan 2026-05-19 reports:
      Q21 vocab_passport — «Moby Dick TTR=0.440089» + «Moby Dick; Or,
        The Whale TTR=0.078766» (same book, two PG ids, different
        tokenization → wildly different TTR)
      Q37 country_compare — «Twain, Mark 211 книг» (edition dups)
    """
    if not rows:
        return rows, 0
    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []  # preserve insertion order
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = r.get(title_key) or ""
        author = (r.get(author_key) or "").strip().lower()
        norm_title = _normalize_book_title(title)
        if not norm_title:
            # Title missing — pass through (no key to group on)
            groups[(author, str(id(r)))] = r
            order.append((author, str(id(r))))
            continue
        gkey = (author, norm_title)
        if gkey not in groups:
            groups[gkey] = r
            order.append(gkey)
            continue
        # Conflict — keep row with highest keep_key
        if keep_key:
            existing = groups[gkey]
            new_v = r.get(keep_key) or 0
            old_v = existing.get(keep_key) or 0
            try:
                if float(new_v) > float(old_v):
                    groups[gkey] = r
            except (TypeError, ValueError):
                pass
        # else: keep first (already there)
    out = [groups[k] for k in order]
    return out, len(rows) - len(out)


# ---------- tightened corpus artifacts (override) ----------

# This is in addition to scripts/v2/tools/authors/_corpus_artifacts.py.
# B19 — «th» (2 chars, consonant-only) leaked through the ≥3 length
# requirement. Drop fully-consonant tokens of length ≥ 2 here.


def drop_short_consonant_clusters(rows: list[dict], *,
                                    word_key: str = "word",
                                    min_len: int = 2,
                                    ) -> Tuple[list[dict], int]:
    """B19 — extend existing corpus_artifacts filter to len ≥ 2 for
    consonant-only tokens. Catches OCR fragments «th» / «pp» / «bk».
    """
    if not rows:
        return rows, 0
    vowels = set("aeiouy")  # include y as vowel for safety
    out: list[dict] = []
    for r in rows:
        token = r.get(word_key) if isinstance(r, dict) else None
        if not isinstance(token, str):
            out.append(r)
            continue
        s = token.strip().lower()
        if len(s) >= min_len and s.isalpha() and not any(c in vowels for c in s):
            continue  # drop
        out.append(r)
    return out, len(rows) - len(out)


# ---------- pipeline runner ----------


def apply_filters(
    filters: list[Callable[[list[dict]], Tuple[list[dict], int]]],
    rows: list[dict],
) -> Tuple[list[dict], dict[str, int]]:
    """Compose multiple filters; return (kept_rows, {filter_name: dropped}).

    Filter functions may be raw or `functools.partial`-wrapped — the
    name reported uses `__name__` or the partial's `func.__name__`.
    """
    drops: dict[str, int] = {}
    for fn in filters:
        before = len(rows)
        rows, dropped = fn(rows)
        if dropped:
            name = getattr(fn, "__name__", None) or \
                    getattr(getattr(fn, "func", None), "__name__", None) or \
                    "filter"
            drops[name] = drops.get(name, 0) + dropped
    return rows, drops


__all__ = [
    "apply_filters",
    "dedup_book_editions",
    "dedup_by_key",
    "dedup_overlapping_snippets",
    "drop_iso_language_codes",
    "drop_null_authors",
    "drop_short_consonant_clusters",
    "looks_like_iso_code",
]
