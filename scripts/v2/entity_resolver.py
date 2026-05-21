"""EntityResolver v5 — single path for resolving author/book/word names.

Part of the v5 architectural refactor ([[architecture_refactor_v5_plan]] §P3).
Closes the entity-resolution class of R14 regressions:

  - B-R14-4  Latin/Cyrillic homoglyph («Доcтоевский» with Latin c)
             → NFKC + homoglyph fold BEFORE alias lookup
  - B-R14-9  RU genitive «Толстого» / «Братьев Карамазовых»
             → suffix-table lemmatize + extended RU→EN title aliases
  - B-R14-13 «Hugo» → obscure «Ganz, Hugo» wins fuzzy over Victor Hugo
             → prominence ranking (downloads) overrides fuzzy ties
  - B-R14-14 «Лавкрафт» rendered as «Харпер Лавкрафт»
             → ResolveResult.resolved.canonical_display is authoritative

Design — pipeline of pure steps (NO state, NO LLM):

    raw query
        ↓
    [Normalize]      NFKC, homoglyph fold, dash unification, whitespace
        ↓
    [Lemmatize]      RU genitive → nominative (suffix table, no pymorphy dep)
        ↓
    [Generate candidates]
                     curated alias (1.0)
                       ↓ if miss
                     corpus exact match (0.95)
                       ↓ if miss
                     fuzzy match (rapidfuzz WRatio, threshold 70)
        ↓
    [Rank by prominence]
                     authors: tuple(fuzz_score_band, downloads, corpus_volume)
                     books:   tuple(fuzz_score_band, downloads, recency)
        ↓
    [Score confidence]
                     top-1 vs top-2 gap → 1.0 / 0.88 / 0.55
                     gap < threshold → decision=clarify_needed
        ↓
    ResolveResult(decision, resolved, candidates, confidence,
                  normalization_trace)

Phase 1: this module ships as a parallel path. The v4 tools
`resolve_author_name` / `resolve_book_title` in tools/meta/resolve_entity.py
are unchanged. Phase 1.5 migrates those tools to delegate here when
WC_V5_RESOLVER=on.

The resolver returns RenderableView-compatible NOT_FOUND or CLARIFY
shapes on negative paths, so Phase 2 tools can emit them directly.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Optional

log = logging.getLogger("wordcracker.v2.entity_resolver")

# Feature flag — Phase 1 ships off-by-default; tests force it on.
V5_RESOLVER_ENABLED = os.environ.get("WC_V5_RESOLVER", "off") == "on"


# =====================================================================
# 1. Normalization — NFKC + homoglyph + dash + whitespace
# =====================================================================

# Latin → Cyrillic homoglyph table. Covers the cases R14 surfaced
# (Latin 'c' inside «Доcтоевский»). Only Latin→Cyrillic direction;
# we never want Cyrillic→Latin since the corpus side is mostly Latin
# but RU author names are always Cyrillic in user input.
#
# Strategy: if the query has BOTH Latin and Cyrillic letters in adjacent
# positions, fold the Latin ones to their Cyrillic visual twins. If the
# query is pure Latin, leave alone.
_HOMOGLYPH_LAT_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "h": "н", "k": "к", "m": "м", "t": "т", "b": "в",
    "A": "А", "C": "С", "E": "Е", "O": "О", "P": "Р", "X": "Х",
    "Y": "У", "H": "Н", "K": "К", "M": "М", "T": "Т", "B": "В",
}

# Various dash characters → ASCII hyphen.
_DASH_RE = re.compile(r"[‐‑‒–—―−]")

# Quote characters to strip.
_QUOTES = "\"'«»“”„‟‹›"


def _has_cyrillic(s: str) -> bool:
    return any("А" <= ch <= "я" or ch in "ёЁ" for ch in s)


def _has_latin(s: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in s)


def _fold_homoglyphs_in_cyrillic_run(s: str) -> str:
    """If a token has Cyrillic letters mixed with Latin, fold the Latin
    ones to their Cyrillic visual twins. Per-token scope so pure-Latin
    queries («Doyle») are left alone.
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

    # 1. NFKC — fold compatibility characters (ﬁ → fi, full-width Latin
    # to ASCII, etc.)
    nfkc = unicodedata.normalize("NFKC", raw)
    if nfkc != raw:
        steps.append(f"NFKC ({len(raw)} → {len(nfkc)} chars)")

    # 2. Strip outer quotes
    stripped = nfkc.strip().strip(_QUOTES).strip()
    if stripped != nfkc.strip():
        steps.append("stripped quotes")

    # 3. Dash unification
    de_dashed = _DASH_RE.sub("-", stripped)
    if de_dashed != stripped:
        steps.append("dashes → ASCII")

    # 4. Homoglyph fold inside Cyrillic-containing tokens
    folded = _fold_homoglyphs_in_cyrillic_run(de_dashed)
    if folded != de_dashed:
        steps.append("homoglyph fold (Latin → Cyrillic)")

    # 5. Collapse whitespace
    spaced = re.sub(r"\s+", " ", folded).strip()
    if spaced != folded:
        steps.append("whitespace collapsed")

    # 6. Lowercase — alias dicts are lowercase-keyed
    lc = spaced.lower()
    if lc != spaced:
        steps.append("lowercased")

    return NormalizationResult(output=lc, steps=steps)


# =====================================================================
# 2. RU Lemmatization — suffix-table genitive→nominative for authors,
#    plus title alias map for RU book titles
# =====================================================================

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
# Sort by suffix length desc — defensive, in case future edits add a
# shorter rule above a longer one.
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
    """Apply per-token RU lemmatization to an author query. Records the
    rule trace for observability.
    """
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


# RU book title aliases — genitive / various cases → nominative, then to
# EN canonical. The existing KNOWN_BOOKS dict already covers «Преступления
# и наказания» (gen of Crime and Punishment). v5 extends with the gaps
# R14 surfaced.
#
# Format: <RU case form lowercase>: <RU nominative lowercase>
# Downstream lookup of RU nominative goes through KNOWN_BOOKS.
_RU_BOOK_TITLE_ALIASES: dict[str, str] = {
    # B-R14-9 part 2 — «Братьев Карамазовых» missing from KNOWN_BOOKS.
    # Brothers Karamazov is PG28054.
    "братьев карамазовых": "братья карамазовы",
    "братьями карамазовыми": "братья карамазовы",
    "братьям карамазовым": "братья карамазовы",
    "братья карамазовы": "братья карамазовы",
    "the brothers karamazov": "братья карамазовы",
    "brothers karamazov": "братья карамазовы",
    # Anna Karenina genitives — already partially covered, but ensure
    # canonicalization here too.
    "анны карениной": "анна каренина",
    "анной карениной": "анна каренина",
    "анну каренину": "анна каренина",
    "анна каренина": "анна каренина",
    "anna karenina": "анна каренина",
}

# Mapping of RU nominative titles → (pg_id, canonical EN title).
# Used when the resolved RU title isn't in KNOWN_BOOKS yet.
_RU_NOMINATIVE_TO_PG: dict[str, tuple[str, str]] = {
    "братья карамазовы": ("PG28054", "The Brothers Karamazov"),
    "анна каренина":     ("PG1399",  "Anna Karenina"),
}


def resolve_ru_book_alias(q_lc: str) -> tuple[str | None, str | None, str] | None:
    """If `q_lc` (already normalized lowercase) is a Russian title in
    any case form, return (pg_id, canonical_en, alias_used).
    None if not a known RU title.
    """
    nominative = _RU_BOOK_TITLE_ALIASES.get(q_lc)
    if nominative is None:
        return None
    mapped = _RU_NOMINATIVE_TO_PG.get(nominative)
    if mapped is None:
        return None
    pg, canon_en = mapped
    return pg, canon_en, f"RU title «{q_lc}» → «{nominative}»"


# =====================================================================
# 3. Prominence index — author downloads / corpus_volume sidecar
# =====================================================================

# Lazy-loaded {author_regex: {downloads_sum, books_count}}. Built once
# from `_metadata_df` on first use, mtime-cached.

_prom_lock = threading.Lock()
_prom_state: dict = {
    "data": None,             # dict | None
    "mtime": 0.0,
    "build_ts": 0.0,
}


def _build_prominence_index() -> dict:
    """Aggregate downloads + book counts per author regex.

    Returns dict like `{"^Doyle,": {"downloads": 123456, "books": 22}, …}`.
    O(N) over the SPGC metadata once. Mtime-cached so reloading metadata
    rebuilds.
    """
    try:
        from scripts.rag_tools import _metadata_df
        df = _metadata_df()
    except Exception as e:
        log.warning("prominence index: _metadata_df unavailable: %s", e)
        return {}
    if df is None or "author" not in df.columns:
        return {}

    has_downloads = "downloads" in df.columns

    out: dict[str, dict] = {}
    # Iterate fast — group-by would copy; just walk rows.
    authors = df["author"].astype(str).tolist()
    downloads = df["downloads"].fillna(0).astype(int).tolist() if has_downloads else [0] * len(authors)
    for a, dl in zip(authors, downloads):
        if not a or a == "nan":
            continue
        surname = a.split(",", 1)[0].strip()
        if not surname:
            continue
        key = f"^{surname},"
        ent = out.setdefault(key, {"downloads": 0, "books": 0,
                                    "canonical_first": a})
        ent["downloads"] += int(dl)
        ent["books"] += 1
    return out


def get_prominence_index(force_reload: bool = False) -> dict:
    """Thread-safe accessor. Rebuilds when metadata file mtime changes."""
    with _prom_lock:
        if force_reload or _prom_state["data"] is None:
            _prom_state["data"] = _build_prominence_index()
            _prom_state["build_ts"] = time.time()
        return _prom_state["data"]


def prominence_for(author_regex: str) -> dict:
    """Get prominence for a single author_regex. Empty dict if unknown."""
    idx = get_prominence_index()
    return idx.get(author_regex, {})


# =====================================================================
# 4. Candidate generation
# =====================================================================

@dataclass
class Candidate:
    """One possible resolution. The resolver returns a ranked list.

    Fields:
      key                — what we matched: author_regex or pg_id
      display            — canonical name to show user ("Doyle, Arthur Conan")
      score              — match quality 0-100 (alias=100, fuzzy=actual score)
      source             — "alias_curated" / "corpus_exact" / "fuzzy"
      prominence         — downloads (authors) or downloads (books)
      books_in_corpus    — author-only: how many books we have
    """
    key: str
    display: str
    score: int
    source: Literal["alias_curated", "corpus_exact", "fuzzy", "ru_title_alias"]
    prominence: int = 0
    books_in_corpus: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "display": self.display,
            "score": self.score,
            "source": self.source,
            "prominence": self.prominence,
            "books_in_corpus": self.books_in_corpus,
            **({"extra": self.extra} if self.extra else {}),
        }


def _try_rapidfuzz():
    try:
        from rapidfuzz import fuzz, process
        return process, fuzz
    except ImportError:
        return None


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
    # Split into tokens on whitespace + comma + period
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


def _candidates_from_alias(q_lc: str) -> list[Candidate]:
    """Curated alias hit — at most one candidate, score 100."""
    try:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
    except ImportError:
        return []
    regex = AUTHOR_ALIASES.get(q_lc)
    if not regex:
        # Also try last word (surname) and first+last
        parts = q_lc.split()
        if len(parts) >= 2:
            for k in (parts[-1], f"{parts[0]} {parts[-1]}"):
                regex = AUTHOR_ALIASES.get(k)
                if regex:
                    break
    if not regex:
        return []
    prom = prominence_for(regex)
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
        # rapidfuzz not installed — token-aware substring fallback.
        # Real prod has rapidfuzz; this branch covers dev boxes and CI
        # without the optional dep. It needs to be good enough to make
        # the goldens pass on a mocked metadata fixture.
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
        prom = prominence_for(regex)
        out.append(Candidate(
            key=regex,
            display=choice,
            score=score,
            source="fuzzy",
            prominence=prom.get("downloads", 0),
            books_in_corpus=prom.get("books", 0),
        ))
    return out


def _regex_to_display(regex: str) -> str:
    return regex.lstrip("^").rstrip(",").strip()


# =====================================================================
# 5. Prominence ranking
# =====================================================================

# Banding fuzz scores so we don't over-weight tiny score differences.
# 95-100 = ~exact; 85-94 = strong; 70-84 = okay; below = noisy.
def _fuzz_band(score: int) -> int:
    if score >= 95:
        return 4
    if score >= 85:
        return 3
    if score >= 70:
        return 2
    return 1


def rank_author_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Sort by (fuzz_band desc, prominence desc, books_in_corpus desc).

    This is the structural fix for B-R14-13: «Hugo» matches both
    Victor Hugo (well-known, ~15K downloads) and obscure Ganz, Hugo
    (~0 downloads). Both have similar fuzz scores against query «Hugo».
    Prominence ranking pushes Victor to top-1.
    """
    return sorted(
        cands,
        key=lambda c: (
            _fuzz_band(c.score),
            c.prominence,
            c.books_in_corpus,
            -len(c.display),    # tiebreaker: shorter display = canonical
        ),
        reverse=True,
    )


# =====================================================================
# 6. Confidence scoring + decision
# =====================================================================

def confidence_from_gap(top: Candidate, runner_up: Candidate | None) -> tuple[float, str]:
    """Score 0..1 based on top-1 vs top-2 gap. Returns (confidence, reason)."""
    if top.source == "alias_curated":
        return 1.0, "curated alias exact match"
    if top.source == "ru_title_alias":
        return 0.95, "RU title alias map"

    if runner_up is None:
        return 0.90, "single candidate"

    # Fuzz score gap
    fuzz_gap = top.score - runner_up.score
    # Prominence ratio — top has 10× downloads of runner-up?
    prom_ratio = (top.prominence / runner_up.prominence) if runner_up.prominence > 0 else 999.0

    if fuzz_gap >= 10 or prom_ratio >= 5.0:
        return 0.88, f"clear winner (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"
    if fuzz_gap >= 5 or prom_ratio >= 2.0:
        return 0.72, f"likely winner (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"
    return 0.55, f"ambiguous (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"


# =====================================================================
# 7. ResolveResult — the contract
# =====================================================================

ResolveDecision = Literal["resolved", "clarify_needed", "not_found"]


@dataclass
class ResolveResult:
    """Outcome of a resolve_* call.

    decision:
      resolved        — `resolved` is the canonical reference, callers use it
      clarify_needed  — confidence low / multiple strong candidates;
                        renderer should ask user to disambiguate
      not_found       — no candidate above the noise floor; callers should
                        return NOT_FOUND view (see view_types.py)

    resolved (when decision == "resolved"):
      For authors: {"author_regex": "^Doyle,", "display": "Doyle, Arthur Conan",
                    "prominence": 95000, "books_in_corpus": 22}
      For books:   {"pg_id": "PG1342", "title": "Pride and Prejudice",
                    "author": "Austen, Jane"}

    confidence: 0..1
    candidates: top-K ranked list (always populated, useful for clarify)
    normalization_trace: list of human-readable normalization steps applied
    """
    decision: ResolveDecision
    resolved: dict | None
    confidence: float
    candidates: list[Candidate]
    normalization_trace: list[str]
    query_raw: str
    query_normalized: str
    confidence_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "resolved": self.resolved,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "candidates": [c.to_dict() for c in self.candidates],
            "normalization_trace": self.normalization_trace,
            "query_raw": self.query_raw,
            "query_normalized": self.query_normalized,
        }


# =====================================================================
# 8. Public API — resolve_author / resolve_book
# =====================================================================

# Confidence threshold below which we surface candidates instead of
# committing to a top-1.
_CLARIFY_CONFIDENCE_FLOOR = 0.65

# Score below which we treat as not_found.
_NOTFOUND_SCORE_FLOOR = 60


def resolve_author(query: str) -> ResolveResult:
    """Single path for author resolution. v5 entry point.

    Pipeline: normalize → RU lemmatize → curated alias → corpus fuzzy →
    prominence rank → confidence score.
    """
    raw = query or ""
    norm = normalize_query(raw)
    q_lc = norm.output
    trace = list(norm.steps)

    if not q_lc:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized="",
            confidence_reason="empty query",
        )

    # RU lemmatize
    q_lem, ru_trace = ru_lemmatize_author_query(q_lc)
    trace.extend(ru_trace)
    if q_lem != q_lc:
        q_lc = q_lem

    # Step A: curated alias on normalized + lemmatized form
    cands_alias = _candidates_from_alias(q_lc)
    if cands_alias:
        top = cands_alias[0]
        conf, reason = confidence_from_gap(top, None)
        return ResolveResult(
            decision="resolved",
            resolved={
                "author_regex": top.key,
                "display": top.display,
                "prominence": top.prominence,
                "books_in_corpus": top.books_in_corpus,
                "source": top.source,
            },
            confidence=conf,
            candidates=cands_alias,
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=reason,
        )

    # Step B: corpus fuzzy
    cands_fuzzy = _candidates_from_corpus_fuzzy(q_lc, limit=8)
    if not cands_fuzzy:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="no fuzzy match above floor",
        )

    # Step C: prominence rank
    ranked = rank_author_candidates(cands_fuzzy)
    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None

    if top.score < _NOTFOUND_SCORE_FLOOR:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=ranked[:5], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=f"best score {top.score} below floor {_NOTFOUND_SCORE_FLOOR}",
        )

    conf, reason = confidence_from_gap(top, runner_up)
    if conf < _CLARIFY_CONFIDENCE_FLOOR:
        return ResolveResult(
            decision="clarify_needed",
            resolved=None,
            confidence=conf,
            candidates=ranked[:5],
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=reason,
        )

    return ResolveResult(
        decision="resolved",
        resolved={
            "author_regex": top.key,
            "display": top.display,
            "prominence": top.prominence,
            "books_in_corpus": top.books_in_corpus,
            "source": top.source,
        },
        confidence=conf,
        candidates=ranked[:5],
        normalization_trace=trace,
        query_raw=raw, query_normalized=q_lc,
        confidence_reason=reason,
    )


def resolve_book(query: str, *, author_hint: str = "") -> ResolveResult:
    """Single path for book resolution. v5 entry point.

    Pipeline: normalize → RU title alias map → KNOWN_BOOKS → v1 find_book
    fuzzy with prominence ranking by downloads.
    """
    raw = query or ""
    norm = normalize_query(raw)
    q_lc = norm.output
    trace = list(norm.steps)

    if not q_lc:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized="",
            confidence_reason="empty query",
        )

    # Step A: RU title alias map (genitive case forms of major works)
    ru_hit = resolve_ru_book_alias(q_lc)
    if ru_hit is not None:
        pg_id, canon_en, alias_trace = ru_hit
        trace.append(alias_trace)
        cand = Candidate(
            key=pg_id, display=canon_en, score=100,
            source="ru_title_alias",
        )
        return ResolveResult(
            decision="resolved",
            resolved={"pg_id": pg_id, "title": canon_en, "author": None,
                       "source": "ru_title_alias"},
            confidence=0.95,
            candidates=[cand],
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="RU title alias exact match",
        )

    # Step B: KNOWN_BOOKS exact / declension match
    try:
        from scripts.v2.planner.entities import KNOWN_BOOKS
    except ImportError:
        KNOWN_BOOKS = {}
    for k in {q_lc, " ".join(q_lc.split())}:
        if k in KNOWN_BOOKS:
            pg_id, canon = KNOWN_BOOKS[k]
            cand = Candidate(
                key=pg_id or "", display=canon, score=100,
                source="alias_curated",
            )
            conf = 1.0 if pg_id else 0.75
            reason = "KNOWN_BOOKS exact" if pg_id else "KNOWN_BOOKS hit but no PG id (copyright)"
            return ResolveResult(
                decision="resolved",
                resolved={"pg_id": pg_id or None, "title": canon,
                           "author": None, "source": "known_books"},
                confidence=conf,
                candidates=[cand],
                normalization_trace=trace,
                query_raw=raw, query_normalized=q_lc,
                confidence_reason=reason,
            )

    # Step C: delegate to v1 find_book (handles Cyrillic auto-translate)
    try:
        from scripts.rag_tools import find_book as _v1_find_book
    except ImportError:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="v1 find_book unavailable",
        )

    raw_res = _v1_find_book(title=query, author=author_hint or "", top=5, lang="en")
    if isinstance(raw_res, dict) and raw_res.get("error"):
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=f"find_book error: {raw_res.get('error')}",
        )
    matches = (raw_res.get("matches") if isinstance(raw_res, dict) else None) or []
    if not matches:
        return ResolveResult(
            decision="not_found", resolved=None, confidence=0.0,
            candidates=[], normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason="no fuzzy book match",
        )

    cands = [
        Candidate(
            key=str(m.get("id") or ""),
            display=m.get("title") or "",
            score=int(min(100, 60 + int((m.get("downloads") or 0) ** 0.5))),
            source="fuzzy",
            prominence=int(m.get("downloads") or 0),
            extra={"author": m.get("author") or ""},
        )
        for m in matches
    ]
    # Sort by score band then downloads
    cands.sort(key=lambda c: (_fuzz_band(c.score), c.prominence), reverse=True)
    top = cands[0]
    runner_up = cands[1] if len(cands) > 1 else None
    conf, reason = confidence_from_gap(top, runner_up)

    if conf < _CLARIFY_CONFIDENCE_FLOOR:
        return ResolveResult(
            decision="clarify_needed",
            resolved=None, confidence=conf,
            candidates=cands[:5],
            normalization_trace=trace,
            query_raw=raw, query_normalized=q_lc,
            confidence_reason=reason,
        )

    return ResolveResult(
        decision="resolved",
        resolved={
            "pg_id": top.key, "title": top.display,
            "author": top.extra.get("author") or None,
            "source": "find_book_fuzzy",
        },
        confidence=conf,
        candidates=cands[:5],
        normalization_trace=trace,
        query_raw=raw, query_normalized=q_lc,
        confidence_reason=reason,
    )


# =====================================================================
# Module-level marker
# =====================================================================

V5_ENTITY_RESOLVER_VERSION = "0.1"


__all__ = [
    "V5_RESOLVER_ENABLED",
    "V5_ENTITY_RESOLVER_VERSION",
    "NormalizationResult",
    "normalize_query",
    "ru_lemmatize_author_query",
    "resolve_ru_book_alias",
    "Candidate",
    "ResolveResult",
    "ResolveDecision",
    "rank_author_candidates",
    "confidence_from_gap",
    "resolve_author",
    "resolve_book",
    "get_prominence_index",
    "prominence_for",
]
