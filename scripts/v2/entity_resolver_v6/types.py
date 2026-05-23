"""v6 entity resolver — typed data classes.

T1 (D-P1-6, 2026-05-23) — the v5 `Candidate` / `ResolveResult` /
`ResolveDecision` definitions moved here so v6 modules import from
within the package instead of looping back through
`scripts.v2.entity_resolver`. The old module is now a thin re-export
shim.

Core types:
  * Mention            — a detected entity reference (surface + type + context)
  * CandidateScore     — KB candidate with multi-factor score breakdown
  * ResolverDecision   — final output: resolved / clarify / not_found
  * Candidate          — one possible resolution (used inside ResolveResult)
  * ResolveResult      — v5-style outcome shape consumed by rag_v2 / plan / view
  * NormalizationResult is in normalize.py (kept next to normalize_query)

Mention is frozen — once detected, its surface form / type / span are
immutable. CandidateScore is built fresh during scoring (not mutated).
ResolverDecision aggregates everything for trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class MentionType(str, Enum):
    """How the user referred to the entity.

    Detection precedence (highest wins overlapping spans):
      CANONICAL_FORMAT > ALIAS_HIT > FULL_NAME > RU_STEM > SURNAME_ONLY
    """

    # "Doyle, Arthur Conan" — explicit canonical form with comma
    CANONICAL_FORMAT = "canonical_format"

    # "Christopher Marlowe", "Конан Дойл" — first + surname tokens
    FULL_NAME = "full_name"

    # "h. g. wells", "конан дойл" — multi-word alias in AUTHOR_ALIASES
    # (lowercased, hit on the FULL key, not via surname fallback)
    ALIAS_HIT = "alias_hit"

    # "Толстого", "Достоевского" — Russian declension of surname-only
    RU_STEM = "ru_stem"

    # "Wells", "Doyle" — bare surname (alias hit on single token)
    SURNAME_ONLY = "surname_only"


@dataclass(frozen=True)
class Mention:
    """A detected entity mention.

    Fields:
      surface       — exact substring user typed (preserved case for trace)
      type          — classification (see MentionType)
      span          — (start, end) offsets in NORMALIZED query
      alias_key     — key used in AUTHOR_ALIASES to get regex (lowercase)
      regex         — surname regex from alias lookup, e.g. "^Wells,"
      extra_tokens  — tokens user provided BEYOND the surname/alias key
                      (lowercased). Used for token-overlap scoring.
                      Example: "Christopher Marlowe" → ("christopher",)
                      Example: "Конан Дойл" → ("конан",)
                      Example: "Wells" → ()
    """

    surface: str
    type: MentionType
    span: tuple[int, int]
    alias_key: str
    regex: str
    extra_tokens: tuple[str, ...] = ()


@dataclass
class Candidate:
    """One possible resolution. The resolver returns a ranked list.

    Fields:
      key                — what we matched: author_regex or pg_id
      display            — canonical name to show user ("Doyle, Arthur Conan")
      score              — match quality 0-100 (alias=100, fuzzy=actual score)
      source             — "alias_curated" / "corpus_exact" / "fuzzy" /
                           "ru_title_alias" / "v6_kb_lookup" /
                           "v6_alias_fallback" / "v6_fuzzy"
      prominence         — downloads (authors) or downloads (books)
      books_in_corpus    — author-only: how many books we have
    """
    key: str
    display: str
    score: int
    # T1 (D-P1-6) — source is widened to include the v6 labels that
    # candidates.py / main.py emit so a single Candidate type covers
    # both v5-style and v6-style sources without a second class.
    source: Literal[
        "alias_curated", "corpus_exact", "fuzzy", "ru_title_alias",
        "v6_kb_lookup", "v6_alias_fallback", "v6_fuzzy",
    ]
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


@dataclass
class CandidateScore:
    """A scored candidate from Stage 3 multi-factor scoring.

    Score components (each in [0, 1]):
      string_sim       — Jaccard token overlap between mention.surface
                         and candidate.display (lowercased)
      token_overlap    — fraction of mention.extra_tokens that appear
                         (substring) in candidate.display.lower()
      prominence_prior — log-normalized downloads relative to max
                         candidate in this group

    combined is the weighted sum (weights depend on mention.type).
    """

    candidate: Candidate
    string_sim: float
    token_overlap: float
    prominence_prior: float
    combined: float


class Decision(str, Enum):
    RESOLVED = "resolved"
    CLARIFY_NEEDED = "clarify_needed"
    NOT_FOUND = "not_found"


# v5-style decision string (used by `ResolveResult` for backwards-compat
# with rag_v2 / plan / view consumers).
ResolveDecision = Literal["resolved", "clarify_needed", "not_found"]


@dataclass
class ResolverDecision:
    """Final output of the v6 pipeline.

    Fields:
      decision           — resolved / clarify_needed / not_found
      resolved           — the single Candidate when decision=resolved
      clarify_candidates — top 5 candidates (sorted by combined score)
                           when decision=clarify_needed
      confidence         — combined score of top-1 (or 0.0 if not_found)
      reason             — human-readable explanation for trace
      mention            — the primary Mention that drove the decision
      all_scores         — full scored list for trace (debug/JSONL)
    """

    decision: Decision
    resolved: "Candidate | None"
    clarify_candidates: list["Candidate"] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    mention: "Mention | None" = None
    all_scores: list[CandidateScore] = field(default_factory=list)


@dataclass
class ResolveResult:
    """Outcome of a resolve_* call — v5-style adapter shape.

    Consumed by rag_v2, plan builders, tool wrappers, view renderers.

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


__all__ = [
    "Mention",
    "MentionType",
    "Candidate",
    "CandidateScore",
    "Decision",
    "ResolverDecision",
    "ResolveDecision",
    "ResolveResult",
]
