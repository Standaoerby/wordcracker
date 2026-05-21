"""RenderableView — declarative description of how to render tool data.

Part of the v5 architectural refactor ([[architecture_refactor_v5_plan]]).
Phase 0: types exist, tools start emitting them, but the renderer does NOT
read them yet — full pipeline still flows through RENDER_PROMPT. This file
introduces the contract without behavioural change.

Contract: tool returns `ToolResult` which optionally carries `view:
RenderableView`. When v5 renderer goes live (Phase 3), `view` becomes the
single source of truth for what the user sees — fabrication becomes
structurally impossible because renderer assembles markdown from a typed
view payload, not from a free-form prompt rule.

Design notes:
- dataclasses, not pydantic — no new dep.
- `view_type` is a hard enum: every tool maps to exactly one view_type
  (some tools may emit `BUNDLE` to compose multiple).
- `empty_state` is REQUIRED when data is empty — renderer must show the
  empty state (not silently skip the view), and the empty state carries
  the human-readable reason. This is the structural fix for B-R14-3
  (fabrication when compare_authors returned empty).
- `caveats` are surfaced verbatim — they map to render notes today, but
  the v5 renderer will render them deterministically (no prompt
  interpretation).
- `data_validity` is reported at ToolResult level (see _types.py
  extension); the view itself is a presentation contract, not a
  validity contract.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Literal


# ---------------------------------------------------------------------
# ViewType — registry of all renderable shapes
# ---------------------------------------------------------------------

class ViewType(str, Enum):
    """Every tool result maps to exactly one ViewType.

    The starting roster covers the ~32 v2 tools; we expect 12-15 distinct
    view shapes in practice, with `TOP_N_TABLE` and `SUMMARY` covering
    the bulk. New view types should be added sparingly — prefer reusing
    an existing shape with a different `payload` schema.
    """

    # Tabular — array of similar dicts (rank/name/value/share columns)
    TOP_N_TABLE = "top_n_table"

    # Pairwise / triplet comparison with metrics + directions
    COMPARISON_PANEL = "comparison_panel"

    # Word lookup bundle: translation + IPA + POS + definition + 2-3
    # corpus snippets + etymology (B101 contract — all facets in one view)
    ETYMOLOGY_BUNDLE = "etymology_bundle"

    # Single-book readability: Flesch + FK + CEFR + word count
    READABILITY_SUMMARY = "readability_summary"

    # Word frequency over epochs (table or chart-friendly array)
    TIMELINE_CHART = "timeline_chart"

    # author_attribution result: ranked candidates with Burrows Delta
    ATTRIBUTION_RESULT = "attribution_result"

    # Book recommendation list with reasons
    RECOMMENDATION_LIST = "recommendation_list"

    # Corpus overview / corpus_meta snapshot
    CORPUS_META_SNAPSHOT = "corpus_meta_snapshot"

    # Word context snippets (word_contexts standalone)
    WORD_CONTEXTS = "word_contexts"

    # Collocates (word_collocates result)
    COLLOCATES = "collocates"

    # Author profile (author_profile composite — 6 sub-views merged)
    AUTHOR_PROFILE = "author_profile"

    # Vocab passport (vocab_passport composite)
    VOCAB_PASSPORT = "vocab_passport"

    # Author metadata (years, nationality, book count)
    AUTHOR_METADATA = "author_metadata"

    # Book lookup result (find_book / resolve_book_title)
    BOOK_LOOKUP = "book_lookup"

    # Author lookup result (list of books for an author)
    AUTHOR_LOOKUP = "author_lookup"

    # Emotion profile per book
    EMOTION_PROFILE = "emotion_profile"

    # Learning words (B1/B2/C1 vocabulary for a book/author)
    LEARNING_WORDS = "learning_words"

    # Export file artifact (Anki CSV / Markdown / JSON)
    EXPORT_ARTIFACT = "export_artifact"

    # Introduction / capabilities / help (static)
    INTRODUCTION = "introduction"

    # Clarify — structured "I need more info" with alternatives
    CLARIFY = "clarify"

    # Out-of-scope (copyright, role-injection, OOS) with WHY/WHAT/WHICH
    OUT_OF_SCOPE = "out_of_scope"

    # Not found — entity resolution returned no candidate above threshold
    NOT_FOUND = "not_found"

    # Friendly error (renderer/network/timeout) with retry hint
    ERROR_FRIENDLY = "error_friendly"

    # Composite — multiple sub-views (for plan steps that fan out)
    BUNDLE = "bundle"


# ---------------------------------------------------------------------
# EmptyState — REQUIRED when data is empty
# ---------------------------------------------------------------------

class EmptyReason(str, Enum):
    """Why the data array is empty. NOT-NULL when view payload is empty.

    Renderer uses this to produce a human-readable explanation instead of
    rendering an empty table. This is the structural fix for B-R14-3
    (renderer fabricating fake signature words when compare_authors
    returned empty).
    """

    # Filter was too strict (min_corpus_count high, etc.)
    FILTERED_OUT = "filtered_out"

    # Entity resolved but corpus has no matching records
    NO_RECORDS_IN_CORPUS = "no_records_in_corpus"

    # Entity did not resolve (combined with NOT_FOUND view at top level)
    ENTITY_UNRESOLVED = "entity_unresolved"

    # Tool ran but the feature is semantically broken (learning_words B2 = 0
    # across ALL books in R14 → broken, not empty_expected)
    TOOL_BROKEN = "tool_broken"

    # Feature legitimately has nothing to show (no Latin words at all in
    # a children's book — empty_expected)
    NO_SIGNAL_EXPECTED = "no_signal_expected"

    # Composite query: one side empty, the other has data (compare_authors)
    PARTIAL_EMPTY = "partial_empty"


@dataclass
class EmptyState:
    """Required field on RenderableView when payload is empty.

    Carries the human-readable explanation the renderer surfaces. Without
    this, renderer would have to invent (or fabricate) the explanation —
    that's what B-R14-3 was.
    """
    reason: EmptyReason
    message_ru: str           # canonical Russian explanation
    message_en: str           # fallback English
    filters_applied: dict = field(default_factory=dict)   # which filters caused it
    suggestion: str | None = None                          # what to try instead

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "message_ru": self.message_ru,
            "message_en": self.message_en,
            "filters_applied": self.filters_applied,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmptyState":
        """Roundtrip from to_dict() output. Used by cache._from_payload
        to restore EmptyState across disk cache reload."""
        reason_str = d.get("reason") or "no_signal_expected"
        try:
            reason = EmptyReason(reason_str)
        except ValueError:
            reason = EmptyReason.NO_SIGNAL_EXPECTED
        return cls(
            reason=reason,
            message_ru=d.get("message_ru", ""),
            message_en=d.get("message_en", ""),
            filters_applied=d.get("filters_applied") or {},
            suggestion=d.get("suggestion"),
        )


# ---------------------------------------------------------------------
# Provenance — what was requested, what was returned, what was filtered
# ---------------------------------------------------------------------

@dataclass
class Provenance:
    """How this view came to be. Anchors count-honesty (B-R14-1, count
    honesty rule 14) and gives the renderer the receipts it needs.

    Fields:
      requested  — what user asked for (top_n=100, level=B2, etc.)
      returned   — what's actually in payload after filters
      filtered   — counts removed per filter stage
      sources    — list of source labels (corpus version, OL, etc.)
    """
    requested: dict = field(default_factory=dict)
    returned: dict = field(default_factory=dict)
    filtered: dict = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v}

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        return cls(
            requested=d.get("requested") or {},
            returned=d.get("returned") or {},
            filtered=d.get("filtered") or {},
            sources=list(d.get("sources") or []),
            notes=list(d.get("notes") or []),
        )


# ---------------------------------------------------------------------
# DataValidity — semantic-level signal beyond ok/fail
# ---------------------------------------------------------------------

class DataValidity(str, Enum):
    """Beyond `ToolResult.ok`. Tells the renderer (and golden tests)
    whether the data is semantically meaningful, partially meaningful,
    or known-broken.

    The R14 lesson (B-R14-7 `learning_words` returned 0 B2 words on ALL
    books): success=True + data=[] is ambiguous. Was the tool empty
    because the filter was strict (OK), or because the level mapping is
    broken (NOT OK)? `data_validity` makes that explicit.
    """

    # Data is present and within expected shape/size
    OK = "ok"

    # Data partially present — some sources fetched, some failed
    # (multi-source enrichment with one source down)
    PARTIAL = "partial"

    # Data is empty, but that is the expected outcome for these inputs
    # (e.g. no Latin words in a children's book)
    EMPTY_EXPECTED = "empty_expected"

    # Data is empty AND that is suspicious (golden test failed, feature
    # never returns data for this input class). Renderer SHOULD surface
    # "this feature may not be working correctly" instead of pretending
    # everything is fine.
    EMPTY_UNEXPECTED = "empty_unexpected"

    # Tool itself is broken (level=B2 returns 0 for every book — config
    # error or upstream data shift). Golden tests catch this in CI; in
    # prod we surface a friendly "feature temporarily unavailable" view.
    BROKEN = "broken"


# ---------------------------------------------------------------------
# RenderableView — the contract
# ---------------------------------------------------------------------

@dataclass
class RenderableView:
    """Declarative description of how to render a ToolResult's data.

    Carried alongside `ToolResult.data` (which remains the raw payload for
    downstream tools that chain on this one). The renderer reads `view`
    instead of inferring the shape from data via prompt rule 18 (B-R14-3
    fabrication root cause).

    Phase 0: tools start emitting this; renderer ignores it. Phase 3:
    renderer uses it as the single source of truth (template executor +
    narrow prose binder per [[architecture_refactor_v5_plan]] §3 P1-P2).

    Fields:
      view_type   — discriminator
      payload     — typed by view_type (renderer template knows the
                    schema per view_type; e.g. TOP_N_TABLE expects
                    {"columns":[...], "rows":[...], "count_returned":N,
                    "count_requested":M, "headline":str|None})
      headline    — optional one-line title (used by some view_types)
      caveats     — render notes for footer / disclaimer line
      empty_state — REQUIRED when data is empty; renderer uses this
                    instead of rendering an empty table
      provenance  — what was asked vs returned vs filtered
      language    — 'ru' | 'en' — controls deterministic phrasing in templates
    """
    view_type: ViewType
    payload: dict = field(default_factory=dict)
    headline: str | None = None
    caveats: list[str] = field(default_factory=list)
    empty_state: EmptyState | None = None
    provenance: Provenance | None = None
    language: Literal["ru", "en"] = "ru"

    def to_dict(self) -> dict:
        d = {
            "view_type": self.view_type.value,
            "payload": self.payload,
            "headline": self.headline,
            "caveats": list(self.caveats),
            "empty_state": self.empty_state.to_dict() if self.empty_state else None,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "language": self.language,
        }
        return {k: v for k, v in d.items() if v not in (None, [], {})}

    @classmethod
    def from_dict(cls, d: dict) -> "RenderableView":
        """Roundtrip from to_dict() output.

        v3.3.1 — closes Stage 3 silent failure root cause. cache._from_payload
        previously did not restore .view across disk roundtrip → every cache
        hit returned ToolResult with view=None → render_v5 selected
        ERROR_FRIENDLY fallback ("no_views_in_results"). This classmethod
        is the missing piece — caches now serve back full view objects.
        """
        vt_str = d.get("view_type") or "top_n_table"
        try:
            vt = ViewType(vt_str)
        except ValueError:
            vt = ViewType.TOP_N_TABLE
        empty_state_dict = d.get("empty_state")
        empty_state = (EmptyState.from_dict(empty_state_dict)
                        if empty_state_dict else None)
        provenance_dict = d.get("provenance")
        provenance = (Provenance.from_dict(provenance_dict)
                       if provenance_dict else None)
        lang = d.get("language", "ru")
        if lang not in ("ru", "en"):
            lang = "ru"
        return cls(
            view_type=vt,
            payload=d.get("payload") or {},
            headline=d.get("headline"),
            caveats=list(d.get("caveats") or []),
            empty_state=empty_state,
            provenance=provenance,
            language=lang,
        )

    def is_empty(self) -> bool:
        """View-type-aware empty detection.

        Text-style views (CLARIFY, OUT_OF_SCOPE, INTRODUCTION,
        ERROR_FRIENDLY, NOT_FOUND, AUTHOR_METADATA, BOOK_LOOKUP,
        READABILITY_SUMMARY, CORPUS_META_SNAPSHOT, EXPORT_ARTIFACT) are
        never empty as long as their payload has any non-trivial content;
        they're single-record / prose views by design.

        List-style views (TOP_N_TABLE, COMPARISON_PANEL, etc.) are empty
        when their canonical content array is empty AND no metric scalars
        present. ETYMOLOGY_BUNDLE is empty when all slots are False.
        """
        if not self.payload:
            return True

        vt = self.view_type

        if vt in _TEXT_STYLE_VIEW_TYPES:
            return _payload_is_trivial(self.payload)

        if vt == ViewType.ETYMOLOGY_BUNDLE:
            slots = self.payload.get("slots_available") or {}
            if any(slots.values()):
                return False
            return not (self.payload.get("snippets") or [])

        if vt == ViewType.COMPARISON_PANEL:
            entities = self.payload.get("entities") or []
            if entities and any(
                e.get("signature_words") or e.get("metrics") for e in entities
            ):
                return False
            return True

        if vt == ViewType.READABILITY_SUMMARY:
            for k in ("flesch", "flesch_kincaid", "cefr", "word_count"):
                if self.payload.get(k) is not None:
                    return False
            return True

        if vt == ViewType.CORPUS_META_SNAPSHOT:
            for k in ("n_books", "n_authors", "n_tokens"):
                if self.payload.get(k) is not None:
                    return False
            return True

        # Fall-through: list-style detection
        for k in ("rows", "items", "contexts", "candidates", "snippets",
                  "words", "books", "authors", "series", "collocates",
                  "emotions"):
            v = self.payload.get(k)
            if isinstance(v, list) and v:
                return False
        for k in ("flesch", "flesch_kincaid", "score", "value", "count",
                  "ttr", "lex_div"):
            if self.payload.get(k) is not None:
                return False
        return True

    def validate(self) -> list[str]:
        """Return list of contract violations. Empty list = valid."""
        issues: list[str] = []
        if self.is_empty() and self.empty_state is None:
            issues.append(
                f"view_type={self.view_type.value} has empty payload but "
                f"no empty_state — renderer cannot explain why."
            )
        if self.empty_state is not None and not self.is_empty():
            issues.append(
                f"view_type={self.view_type.value} has empty_state but "
                f"payload is non-empty — contradiction."
            )
        return issues


# Text-style view types: payload is text / single-record. These views
# are never empty unless their payload is trivially blank — they do NOT
# use the array-based empty detection.
_TEXT_STYLE_VIEW_TYPES: frozenset = frozenset({
    ViewType.CLARIFY,
    ViewType.OUT_OF_SCOPE,
    ViewType.NOT_FOUND,
    ViewType.ERROR_FRIENDLY,
    ViewType.INTRODUCTION,
    ViewType.AUTHOR_METADATA,
    ViewType.BOOK_LOOKUP,
    ViewType.EXPORT_ARTIFACT,
    # Composite views — populated by author_profile / vocab_passport
    # tools with mixed-shape payload (metadata + arrays + scalars).
    ViewType.AUTHOR_PROFILE,
    ViewType.VOCAB_PASSPORT,
})


def _payload_is_trivial(payload: dict) -> bool:
    """A payload is trivial when EVERY value is None / empty str / empty
    list / empty dict. Used by text-style view types where we just want
    to confirm there's *something* to render."""
    for v in payload.values():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        return False
    return True


# ---------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------

def top_n_table_view(
    *,
    rows: list[dict],
    columns: list[str],
    count_requested: int | None = None,
    count_returned: int | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    empty_state: EmptyState | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    """Build a TOP_N_TABLE view. Asserts count_returned == len(rows) if both given."""
    if count_returned is not None and len(rows) != count_returned:
        raise ValueError(
            f"top_n_table count mismatch: count_returned={count_returned} "
            f"but len(rows)={len(rows)}"
        )
    return RenderableView(
        view_type=ViewType.TOP_N_TABLE,
        payload={
            "columns": columns,
            "rows": rows,
            "count_requested": count_requested,
            "count_returned": count_returned if count_returned is not None else len(rows),
        },
        headline=headline,
        caveats=list(caveats or []),
        empty_state=empty_state,
        provenance=provenance,
        language=language,
    )


def empty_view(
    *,
    view_type: ViewType,
    reason: EmptyReason,
    message_ru: str,
    message_en: str,
    filters_applied: dict | None = None,
    suggestion: str | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    """Build an empty view with an EmptyState. Use when tool returned no
    rows but everything else worked — this is the canonical replacement
    for "renderer guesses what happened" (B-R14-3)."""
    return RenderableView(
        view_type=view_type,
        payload={},
        empty_state=EmptyState(
            reason=reason,
            message_ru=message_ru,
            message_en=message_en,
            filters_applied=filters_applied or {},
            suggestion=suggestion,
        ),
        provenance=provenance,
        language=language,
    )


def clarify_view(
    *,
    question_ru: str,
    alternatives: list[str] | None = None,
    why: str | None = None,
) -> RenderableView:
    """Structured clarify. Renderer always asks `question_ru`, lists
    `alternatives` as a bullet list if provided."""
    return RenderableView(
        view_type=ViewType.CLARIFY,
        payload={
            "question": question_ru,
            "alternatives": list(alternatives or []),
            "why": why,
        },
        language="ru",
    )


def not_found_view(
    *,
    entity_type: Literal["author", "book", "word"],
    query: str,
    message_ru: str,
    candidates: list[dict] | None = None,
) -> RenderableView:
    """Resolver returned nothing above threshold. `candidates` is the
    runner-up list — renderer can show them as 'did you mean'."""
    return RenderableView(
        view_type=ViewType.NOT_FOUND,
        payload={
            "entity_type": entity_type,
            "query": query,
            "message_ru": message_ru,
            "candidates": candidates or [],
        },
        language="ru",
    )


# ---------------------------------------------------------------------
# Module-level marker for v5 readiness checks
# ---------------------------------------------------------------------

V5_VIEW_TYPES_VERSION = "0.1"
V5_FOUNDATION_TS = time.strftime("%Y-%m-%d", time.gmtime())
