"""view_builders — factory functions for RenderableView per ViewType.

Part of v5 Phase 2 ([[architecture_refactor_v5_plan]] §P1-P2).

Tools call these factories instead of hand-constructing RenderableView
dataclasses. Benefits:

  1. Pattern uniformity — every tool builds views the same way; reviewers
     can spot deviations.
  2. Count-honesty enforced — builders compute count_returned from the
     actual rows passed in, eliminating the class of bug where tool
     claims "30" but data has 19 (RENDER_PROMPT rule 14).
  3. Provenance attached automatically when caller provides filter spec.
  4. Empty-state autobuild — when rows=[], factory requires a reason
     and message, otherwise raises (closes B-R14-3 fabrication path
     structurally).
  5. Caveats normalized — common ones (count_after_filter, language,
     archaic disclosure) get standard phrasings.

Coverage: one factory per ViewType (excl. BUNDLE which is composite).
Tests in test_view_builders.py round-trip data → view → dict.

Phase 2: tools start emitting these via attach_view() helper at end of
their @tool function body. Phase 3: template_executor.render_view()
turns these into markdown deterministically (no LLM, no fabrication).
"""
from __future__ import annotations

from typing import Any, Literal

from scripts.v2.view_types import (
    DataValidity,
    EmptyReason,
    EmptyState,
    Provenance,
    RenderableView,
    ViewType,
    clarify_view as _legacy_clarify,
    empty_view as _legacy_empty,
    not_found_view as _legacy_not_found,
    top_n_table_view as _legacy_top_n,
)


# =====================================================================
# Provenance helper — used by many factories
# =====================================================================

def make_provenance(
    *,
    requested: dict | None = None,
    returned: dict | None = None,
    filtered: dict | None = None,
    sources: list[str] | None = None,
    notes: list[str] | None = None,
) -> Provenance:
    """Standard provenance attachment. Always returns a Provenance —
    tools that don't pass anything still get an empty one for shape
    consistency."""
    return Provenance(
        requested=requested or {},
        returned=returned or {},
        filtered=filtered or {},
        sources=list(sources or []),
        notes=list(notes or []),
    )


# =====================================================================
# Generic factory — TOP_N_TABLE (used by ~12 tools)
# =====================================================================

def build_top_n_table(
    *,
    rows: list[dict],
    columns: list[str],
    headline: str | None = None,
    requested_n: int | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
    empty_reason: EmptyReason | None = None,
    empty_message_ru: str | None = None,
    empty_message_en: str | None = None,
    empty_filters_applied: dict | None = None,
    empty_suggestion: str | None = None,
) -> RenderableView:
    """Build a TOP_N_TABLE view from already-filtered rows.

    Closes the count-honesty class of regressions: `count_returned` =
    len(rows) always, never inherits `requested_n`. Renderer reads this
    via template_executor and is structurally unable to write a wrong
    count (no LLM rule needed).

    Empty case: if rows=[], you MUST pass empty_reason + message_ru +
    message_en, else this raises. Tool author cannot accidentally emit
    an empty view that the renderer would have to "explain" via prompt.
    """
    if not rows:
        if not (empty_reason and empty_message_ru and empty_message_en):
            raise ValueError(
                "build_top_n_table: rows is empty but no empty_reason/"
                "empty_message_ru/empty_message_en provided. Empty views "
                "must carry an explicit empty_state — see B-R14-3."
            )
        return _legacy_empty(
            view_type=ViewType.TOP_N_TABLE,
            reason=empty_reason,
            message_ru=empty_message_ru,
            message_en=empty_message_en,
            filters_applied=empty_filters_applied,
            suggestion=empty_suggestion,
            provenance=provenance,
            language=language,
        )

    count_returned = len(rows)
    auto_caveats = list(caveats or [])
    # Standard count-honesty caveat when requested != returned. Wording
    # matches RENDER_PROMPT rule 14 phrasing so existing tests still
    # validate.
    if requested_n is not None and requested_n != count_returned:
        auto_caveats.append(
            f"запросил {requested_n}, после фильтра осталось {count_returned}"
        )

    return _legacy_top_n(
        rows=rows,
        columns=columns,
        count_requested=requested_n,
        count_returned=count_returned,
        headline=headline,
        caveats=auto_caveats,
        provenance=provenance,
        language=language,
    )


# =====================================================================
# COMPARISON_PANEL — closes B-R14-3 fabrication
# =====================================================================

def build_comparison_panel(
    *,
    entities: list[dict],            # list of {name, metrics:{...}, signature_words:[...]}
    metrics: list[dict],             # [{name, direction, scale, interpret}]
    shared_signatures: list[str] | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
    # Empty path
    empty_reason: EmptyReason | None = None,
    empty_message_ru: str | None = None,
    empty_message_en: str | None = None,
    empty_filters_applied: dict | None = None,
    empty_suggestion: str | None = None,
) -> RenderableView:
    """COMPARISON_PANEL — pairwise/triple comparison with metrics.

    Closes B-R14-3 (fabrication when compare_authors returns nothing).
    When `entities` is empty OR all of them have empty signature lists,
    you MUST pass empty_reason. Renderer cannot invent signature words
    because the view payload doesn't contain a slot for them.
    """
    all_empty = (
        not entities
        or all(
            not e.get("signature_words")
            and not e.get("metrics")
            for e in entities
        )
    )

    if all_empty:
        if not (empty_reason and empty_message_ru and empty_message_en):
            raise ValueError(
                "build_comparison_panel: no entity data and no empty_state — "
                "this is the B-R14-3 fabrication path. Either supply data "
                "or explain why it's empty."
            )
        return _legacy_empty(
            view_type=ViewType.COMPARISON_PANEL,
            reason=empty_reason,
            message_ru=empty_message_ru,
            message_en=empty_message_en,
            filters_applied=empty_filters_applied,
            suggestion=empty_suggestion,
            provenance=provenance,
            language=language,
        )

    payload = {
        "entities": entities,
        "metrics": metrics,
        "shared_signatures": list(shared_signatures or []),
    }
    return RenderableView(
        view_type=ViewType.COMPARISON_PANEL,
        payload=payload,
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# READABILITY_SUMMARY
# =====================================================================

def build_readability_summary(
    *,
    book_title: str,
    pg_id: str,
    flesch: float | None,
    flesch_kincaid: float | None,
    cefr: str | None,
    word_count: int | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    """Single-book readability. NEVER empty — if flesch is None it's
    a tool-broken state (use error_friendly view instead).
    """
    if flesch is None and flesch_kincaid is None:
        raise ValueError(
            "build_readability_summary: both flesch and FK are None — "
            "use build_error_friendly() instead, this is a broken result."
        )
    return RenderableView(
        view_type=ViewType.READABILITY_SUMMARY,
        payload={
            "book_title": book_title,
            "pg_id": pg_id,
            "flesch": flesch,
            "flesch_kincaid": flesch_kincaid,
            "cefr": cefr,
            "word_count": word_count,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# ETYMOLOGY_BUNDLE — closes B-R14-2 (word bundle inconsistency)
# =====================================================================

def build_etymology_bundle(
    *,
    word: str,
    translation_ru: str | None = None,
    ipa: str | None = None,
    pos: str | None = None,
    definition_en: str | None = None,
    etymology: dict | None = None,
    snippets: list[dict] | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    """Word lookup bundle. Always carries all known slots; missing slots
    are explicit (None) — template_executor renders them as
    «Этимология не извлеклась» rather than silently skipping.

    Closes B-R14-2: word bundle was inconsistent in R14 because each
    tool emitted a partial view and renderer guessed completeness. With
    typed slots, the renderer KNOWS what was attempted and what's
    actually missing.
    """
    payload = {
        "word": word,
        "translation_ru": translation_ru,
        "ipa": ipa,
        "pos": pos,
        "definition_en": definition_en,
        "etymology": etymology,
        "snippets": list(snippets or []),
        # Explicit slot-availability map — renderer reads this without
        # needing to inspect each None field.
        "slots_available": {
            "translation": translation_ru is not None,
            "ipa": ipa is not None,
            "pos": pos is not None,
            "definition": definition_en is not None,
            "etymology": etymology is not None,
            "snippets": bool(snippets),
        },
    }
    return RenderableView(
        view_type=ViewType.ETYMOLOGY_BUNDLE,
        payload=payload,
        headline=headline or word,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# RECOMMENDATION_LIST
# =====================================================================

def build_recommendation_list(
    *,
    items: list[dict],               # [{pg_id, title, author, reasons}]
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
    empty_reason: EmptyReason | None = None,
    empty_message_ru: str | None = None,
    empty_message_en: str | None = None,
) -> RenderableView:
    if not items:
        if not (empty_reason and empty_message_ru and empty_message_en):
            raise ValueError(
                "build_recommendation_list: items empty without empty_state"
            )
        return _legacy_empty(
            view_type=ViewType.RECOMMENDATION_LIST,
            reason=empty_reason,
            message_ru=empty_message_ru,
            message_en=empty_message_en,
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.RECOMMENDATION_LIST,
        payload={"items": items},
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# ATTRIBUTION_RESULT (author_attribution)
# =====================================================================

def build_attribution_result(
    *,
    candidates: list[dict],         # [{author, score, books_matched}]
    primary_metric: str,
    primary_metric_explanation: dict | None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not candidates:
        return _legacy_empty(
            view_type=ViewType.ATTRIBUTION_RESULT,
            reason=EmptyReason.NO_RECORDS_IN_CORPUS,
            message_ru="Стилометрический вектор не сошёлся ни с одним автором.",
            message_en="No author matched the stylometric vector.",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.ATTRIBUTION_RESULT,
        payload={
            "candidates": candidates,
            "primary_metric": primary_metric,
            "primary_metric_explanation": primary_metric_explanation,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# AUTHOR_METADATA
# =====================================================================

def build_author_metadata(
    *,
    author_canonical: str,
    birth_year: int | None,
    death_year: int | None,
    nationality: str | None,
    books_in_corpus: int,
    bio_source: str | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    return RenderableView(
        view_type=ViewType.AUTHOR_METADATA,
        payload={
            "author_canonical": author_canonical,
            "birth_year": birth_year,
            "death_year": death_year,
            "nationality": nationality,
            "books_in_corpus": books_in_corpus,
            "bio_source": bio_source,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# BOOK_LOOKUP
# =====================================================================

def build_book_lookup(
    *,
    book: dict,                       # {pg_id, title, author, pub_year, downloads}
    candidates: list[dict] | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    return RenderableView(
        view_type=ViewType.BOOK_LOOKUP,
        payload={
            "book": book,
            "candidates": list(candidates or []),
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# AUTHOR_LOOKUP
# =====================================================================

def build_author_lookup(
    *,
    author_canonical: str,
    books: list[dict],
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not books:
        return _legacy_empty(
            view_type=ViewType.AUTHOR_LOOKUP,
            reason=EmptyReason.NO_RECORDS_IN_CORPUS,
            message_ru=f"В корпусе нет книг автора {author_canonical}.",
            message_en=f"No books for {author_canonical} in corpus.",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.AUTHOR_LOOKUP,
        payload={
            "author_canonical": author_canonical,
            "books": books,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# EMOTION_PROFILE
# =====================================================================

def build_emotion_profile(
    *,
    book_title: str,
    pg_id: str,
    emotions: list[dict],            # [{emotion, share, count}]
    dominant: list[str] | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not emotions:
        return _legacy_empty(
            view_type=ViewType.EMOTION_PROFILE,
            reason=EmptyReason.NO_RECORDS_IN_CORPUS,
            message_ru="NRC лексикон не нашёл эмоциональных маркеров.",
            message_en="NRC lexicon found no emotion markers.",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.EMOTION_PROFILE,
        payload={
            "book_title": book_title,
            "pg_id": pg_id,
            "emotions": emotions,
            "dominant": list(dominant or []),
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# LEARNING_WORDS — closes B-R14-7 via DataValidity.BROKEN signaling
# =====================================================================

def build_learning_words(
    *,
    words: list[dict],               # [{lemma, translation, definition, example, level}]
    requested_level: str | None,
    requested_count: int | None = None,
    scope_label: str,                 # "Pride and Prejudice (PG1342)" / "The Adventures..."
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
    # B-R14-7 — caller marks broken when empty result is suspicious
    is_broken: bool = False,
    broken_reason: str | None = None,
) -> RenderableView:
    """Closes B-R14-7. When tool returns 0 words and golden_test_says_
    this_input_should_yield_words, caller sets is_broken=True. The view
    payload exposes this, and the parent ToolResult gets
    data_validity=BROKEN.
    """
    if not words:
        return _legacy_empty(
            view_type=ViewType.LEARNING_WORDS,
            reason=EmptyReason.TOOL_BROKEN if is_broken else EmptyReason.NO_SIGNAL_EXPECTED,
            message_ru=(
                broken_reason or
                (f"Не нашёл слов уровня {requested_level} для {scope_label}. "
                 f"Это похоже на сбой фильтра уровня — фича 'learning_words B2' "
                 f"не работает ожидаемо.")
                if is_broken else
                f"Слов уровня {requested_level} для {scope_label} нет в корпусе."
            ),
            message_en=(
                broken_reason or
                f"No level={requested_level} words for {scope_label} — broken filter."
                if is_broken else
                f"No level={requested_level} words for {scope_label} in corpus."
            ),
            provenance=provenance,
            filters_applied={"level": requested_level, "scope": scope_label},
            suggestion="Попробуй уровень 'basic' или другую книгу." if is_broken else None,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.LEARNING_WORDS,
        payload={
            "words": words,
            "requested_level": requested_level,
            "requested_count": requested_count,
            "scope_label": scope_label,
            "count_returned": len(words),
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# COLLOCATES + WORD_CONTEXTS + TIMELINE — common shapes
# =====================================================================

def build_collocates(
    *,
    word: str,
    collocates: list[dict],          # [{token, npmi, count}]
    window: int,
    scope_label: str,
    metric_label: str = "NPMI",      # rendered header for the score column
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not collocates:
        return _legacy_empty(
            view_type=ViewType.COLLOCATES,
            reason=EmptyReason.NO_SIGNAL_EXPECTED,
            message_ru=f"Слово «{word}» не имеет статистически значимых коллокатов в {scope_label}.",
            message_en=f"No significant collocates for «{word}» in {scope_label}.",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.COLLOCATES,
        payload={
            "word": word,
            "collocates": collocates,
            "window": window,
            "scope_label": scope_label,
            "metric_label": metric_label,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


def build_word_contexts(
    *,
    word: str,
    contexts: list[dict],            # [{snippet, pg_id, title, author}]
    scope_label: str,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not contexts:
        return _legacy_empty(
            view_type=ViewType.WORD_CONTEXTS,
            reason=EmptyReason.NO_SIGNAL_EXPECTED,
            message_ru=f"Слово «{word}» не встретилось в {scope_label}.",
            message_en=f"No occurrences of «{word}» in {scope_label}.",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.WORD_CONTEXTS,
        payload={
            "word": word,
            "contexts": contexts,
            "scope_label": scope_label,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


def build_timeline_chart(
    *,
    word: str,
    series: list[dict],              # [{bucket_start, bucket_end, freq_per_million, count}]
    basis: str,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    if not series:
        return _legacy_empty(
            view_type=ViewType.TIMELINE_CHART,
            reason=EmptyReason.NO_SIGNAL_EXPECTED,
            message_ru=f"Слово «{word}» не встречается в корпусе с временной разбивкой.",
            message_en=f"No timeline data for «{word}».",
            provenance=provenance,
            language=language,
        )
    return RenderableView(
        view_type=ViewType.TIMELINE_CHART,
        payload={
            "word": word,
            "series": series,
            "basis": basis,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# CORPUS_META_SNAPSHOT
# =====================================================================

def build_corpus_meta_snapshot(
    *,
    n_books: int,
    n_authors: int,
    n_tokens: int | None,
    spgc_baseline: str,
    chroma_chunks: int | None = None,
    user_uploads: int = 0,
    year_min: int | None = None,
    year_max: int | None = None,
    year_basis: str | None = None,
    headline: str | None = None,
    caveats: list[str] | None = None,
    provenance: Provenance | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    # W-17 (Phase 5 P2, 2026-05-23) — corpus period coverage. When the
    # tool supplies year_min + year_max (real pub_year + birth+30 proxy
    # union), the snapshot view advertises the temporal span; the
    # template renderer adds a «Период охвата» row so the user can
    # answer «какой период охватывает корпус» directly.
    return RenderableView(
        view_type=ViewType.CORPUS_META_SNAPSHOT,
        payload={
            "n_books": n_books,
            "n_authors": n_authors,
            "n_tokens": n_tokens,
            "spgc_baseline": spgc_baseline,
            "chroma_chunks": chroma_chunks,
            "user_uploads": user_uploads,
            "year_min": year_min,
            "year_max": year_max,
            "year_basis": year_basis,
        },
        headline=headline,
        caveats=list(caveats or []),
        provenance=provenance,
        language=language,
    )


# =====================================================================
# EXPORT_ARTIFACT
# =====================================================================

def build_export_artifact(
    *,
    format: Literal["anki_csv", "markdown", "json", "csv", "tsv"],
    content: str,
    filename_suggestion: str,
    item_count: int,
    headline: str | None = None,
    caveats: list[str] | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    return RenderableView(
        view_type=ViewType.EXPORT_ARTIFACT,
        payload={
            "format": format,
            "content": content,
            "filename_suggestion": filename_suggestion,
            "item_count": item_count,
        },
        headline=headline,
        caveats=list(caveats or []),
        language=language,
    )


# =====================================================================
# INTRODUCTION / OUT_OF_SCOPE / ERROR_FRIENDLY / CLARIFY / NOT_FOUND
# =====================================================================

def build_introduction(
    *,
    name: str,
    capabilities: list[str],
    examples: list[str],
    corpus_size_books: int,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    return RenderableView(
        view_type=ViewType.INTRODUCTION,
        payload={
            "name": name,
            "capabilities": capabilities,
            "examples": examples,
            "corpus_size_books": corpus_size_books,
        },
        language=language,
    )


def build_out_of_scope(
    *,
    reason_kind: Literal["copyright", "role_injection", "prompt_injection",
                          "verbatim_request", "system_command",
                          "translation_quality", "generic"],
    why_ru: str,
    what_ru: list[str] | None = None,        # what user CAN do
    which_alternatives: list[dict] | None = None,    # specific alternatives
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    """3-part copyright/OOS refusal: WHY refused + WHAT user can do +
    WHICH specific alternative. Matches Stan's canonical Q3 pattern."""
    return RenderableView(
        view_type=ViewType.OUT_OF_SCOPE,
        payload={
            "reason_kind": reason_kind,
            "why_ru": why_ru,
            "what_ru": list(what_ru or []),
            "which_alternatives": list(which_alternatives or []),
        },
        language=language,
    )


def build_error_friendly(
    *,
    kind: Literal["renderer", "tool_timeout", "tool_internal",
                   "network", "unknown"],
    message_ru: str,
    retry_hint_ru: str | None = None,
    partial_results: list[dict] | None = None,
    language: Literal["ru", "en"] = "ru",
) -> RenderableView:
    return RenderableView(
        view_type=ViewType.ERROR_FRIENDLY,
        payload={
            "kind": kind,
            "message_ru": message_ru,
            "retry_hint_ru": retry_hint_ru,
            "partial_results": list(partial_results or []),
        },
        language=language,
    )


def build_clarify(
    *,
    question_ru: str,
    alternatives: list[str] | None = None,
    why: str | None = None,
) -> RenderableView:
    return _legacy_clarify(
        question_ru=question_ru,
        alternatives=alternatives,
        why=why,
    )


def build_not_found(
    *,
    entity_type: Literal["author", "book", "word"],
    query: str,
    message_ru: str,
    candidates: list[dict] | None = None,
) -> RenderableView:
    return _legacy_not_found(
        entity_type=entity_type,
        query=query,
        message_ru=message_ru,
        candidates=candidates,
    )


# =====================================================================
# attach_view helper — used in tools to wire view into ToolResult
# =====================================================================

def attach_view(
    result,
    view: RenderableView,
    *,
    data_validity: DataValidity = DataValidity.OK,
):
    """Helper used at end of @tool body. Sets `result.view` and
    `result.data_validity`, validates view shape, returns result.

    The validation step is the structural anti-fabrication guard: a tool
    that tries to emit an empty view without empty_state will raise
    here, not silently pass through to a renderer that fabricates.

    Phase 6 — required-field-missing entries are NOT raised here. The
    renderer surfaces them as «<field>: недоступно» caveats in the final
    output. attach_view stays strict on structural violations only.
    """
    structural = [i for i in view.validate()
                  if not i.startswith("required_field_missing:")]
    if structural:
        raise ValueError(
            f"attach_view: invalid view from tool {result.tool}: "
            f"{'; '.join(structural)}"
        )
    result.view = view
    result.data_validity = data_validity
    return result


# Module-level marker
V5_VIEW_BUILDERS_VERSION = "0.1"


__all__ = [
    "make_provenance", "attach_view",
    "build_top_n_table", "build_comparison_panel",
    "build_readability_summary", "build_etymology_bundle",
    "build_recommendation_list", "build_attribution_result",
    "build_author_metadata", "build_book_lookup", "build_author_lookup",
    "build_emotion_profile", "build_learning_words",
    "build_collocates", "build_word_contexts", "build_timeline_chart",
    "build_corpus_meta_snapshot", "build_export_artifact",
    "build_introduction", "build_out_of_scope", "build_error_friendly",
    "build_clarify", "build_not_found",
]
