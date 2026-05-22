"""render_v5 — v5 renderer entry point (Phase 4).

Bridges the deterministic template executor (Phase 3 step A) and the
verified ProseBinder (Phase 3 step B) into a drop-in replacement for
the legacy `_llm_render`.

Architecture ([[architecture_refactor_v5_plan]] §P1-P2 + §P4):

  results: list[ToolResult]
        ↓
  select_primary_view(results)        — pick the most informative view
        ↓
  template_executor.render_view(view) — skeleton (deterministic)
        ↓
  prose_binder.bind_prose(skeleton)   — intro + next-steps (verified)
        ↓
  final = [intro] + skeleton + [next_steps]

Contract — same shape as `_llm_render`:

    render_v5(question, plan, results, model, ollama_host, history)
        → (answer_text: str, meta: dict)

`meta` reports which view was used, whether Phase B ran, audit results,
and elapsed time per stage — usable as `RequestTrace` payload.

Phase 4: gated behind `WC_V5_RENDERER=on` env. Phase 6: becomes default
after acceptance turnir.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from scripts.v2 import prose_binder as pb
from scripts.v2 import template_executor as te
from scripts.v2._types import ToolResult
from scripts.v2.view_types import (
    DataValidity, RenderableView, ViewType,
)

log = logging.getLogger("wordcracker.v2.render_v5")

V5_RENDERER_ENABLED = os.environ.get("WC_V5_RENDERER", "off") == "on"


# =====================================================================
# Primary view selection
# =====================================================================

# Priority order for view types when multiple ToolResults carry views.
# Composite / final-shape views win over support views (BOOK_LOOKUP from
# a chained `find_book` is not the user-facing answer — the downstream
# `book_readability` is).
_VIEW_PRIORITY: dict[ViewType, int] = {
    # Final-shape user-facing views — high priority
    ViewType.READABILITY_SUMMARY:   100,
    ViewType.COMPARISON_PANEL:      95,
    ViewType.ETYMOLOGY_BUNDLE:      90,
    ViewType.EMOTION_PROFILE:       85,
    ViewType.AUTHOR_PROFILE:        85,
    ViewType.VOCAB_PASSPORT:        85,
    ViewType.ATTRIBUTION_RESULT:    80,
    ViewType.LEARNING_WORDS:        80,
    ViewType.TIMELINE_CHART:        75,
    ViewType.COLLOCATES:            70,
    ViewType.WORD_CONTEXTS:         70,
    ViewType.RECOMMENDATION_LIST:   65,
    ViewType.TOP_N_TABLE:           60,
    ViewType.AUTHOR_LOOKUP:         55,
    ViewType.AUTHOR_METADATA:       55,
    ViewType.CORPUS_META_SNAPSHOT:  50,
    ViewType.EXPORT_ARTIFACT:       50,
    # Support / lookup views — low priority (downstream usually has higher)
    ViewType.BOOK_LOOKUP:           30,
    # Negative paths
    ViewType.NOT_FOUND:             10,
    ViewType.CLARIFY:               5,
    ViewType.OUT_OF_SCOPE:          5,
    ViewType.ERROR_FRIENDLY:        5,
    ViewType.INTRODUCTION:          5,
    ViewType.BUNDLE:                40,
}


# E16 — intent → preferred view-type bonus. Static priority alone makes
# every full-bundle view (ETYMOLOGY_BUNDLE prio 90) beat partial-content
# views (WORD_CONTEXTS prio 70), even when the user explicitly asked for
# the latter. Bonus tilts the contest for matching intents without
# disturbing the unrelated default ordering.
_INTENT_VIEW_BONUS: dict[str, dict[ViewType, int]] = {
    "word_contexts": {
        ViewType.WORD_CONTEXTS: 40,   # 70 + 40 = 110 beats ETYMOLOGY_BUNDLE (90)
    },
    "word_collocates": {
        ViewType.COLLOCATES:    40,
    },
    "word_emotion": {
        ViewType.COLLOCATES:    30,   # emotion_collocates → COLLOCATES view
    },
    "word_etymology": {
        ViewType.ETYMOLOGY_BUNDLE: 10,  # already 90, gentle bump
    },
    "word_timeline": {
        ViewType.TIMELINE_CHART: 30,
    },
}


def _intent_alignment_bonus(intent: str | None, vt: ViewType) -> int:
    if not intent:
        return 0
    bonuses = _INTENT_VIEW_BONUS.get(intent)
    if not bonuses:
        return 0
    return bonuses.get(vt, 0)


def select_primary_view(
    results: list[ToolResult],
    *,
    intent: str | None = None,
) -> tuple[ToolResult | None, RenderableView | None]:
    """Pick the most informative view from a list of ToolResults.

    Heuristics:
      1. Skip ToolResults without `view`.
      2. Skip views whose data_validity is BROKEN (caller renders an
         ERROR_FRIENDLY fallback for those).
      3. Prefer views with non-empty payload.
      4. Within equal-payload-ness, use _VIEW_PRIORITY table.
      5. Tie-break: later-in-plan ToolResult (terminal step in DAG).

    `intent` (optional): when provided, applies an intent-aligned bonus
    so the view that semantically matches the user's intent beats
    incidental support views.  E16 («примеры использования слова X»
    intent=word_contexts but enrich_word's ETYMOLOGY_BUNDLE outranked
    hybrid_search's WORD_CONTEXTS by static priority alone, hiding the
    examples Stan actually asked for.)
    """
    if not results:
        return None, None

    candidates: list[tuple[int, int, int, ToolResult, RenderableView]] = []
    for idx, r in enumerate(results):
        if r is None or r.view is None:
            continue
        view: RenderableView = r.view
        # Skip BROKEN tools — render_v5 handles them via error_friendly path
        if r.data_validity == DataValidity.BROKEN:
            continue
        priority = _VIEW_PRIORITY.get(view.view_type, 40)
        # Empty-view penalty — but ONLY for "junk empty" (no empty_state
        # explanation). An empty view WITH empty_state is the canonical
        # answer for B-R14-3-class cases (e.g. compare_authors returned
        # nothing → COMPARISON_PANEL.empty_state explains why). That
        # explanation IS the user-facing answer; it must beat earlier
        # probe views (AUTHOR_METADATA from chained author_metadata
        # steps in author_compare plan).
        is_explained_empty = view.is_empty() and view.empty_state is not None
        is_junk_empty = view.is_empty() and view.empty_state is None
        empty_penalty = -200 if is_junk_empty else 0
        # E16 — intent-aligned bonus. When user asks «примеры
        # использования», we want WORD_CONTEXTS to beat ETYMOLOGY_BUNDLE
        # (which only carries translation/POS/def — no examples).
        intent_bonus = _intent_alignment_bonus(intent, view.view_type)
        # Higher idx = later in plan = closer to the user-facing answer
        candidates.append((
            priority + empty_penalty + intent_bonus, idx, priority, r, view,
        ))

    if not candidates:
        return None, None

    # Sort by (priority desc, idx desc) — later in plan wins on ties
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, _, best_r, best_v = candidates[0]
    return best_r, best_v


# =====================================================================
# Error / fallback paths
# =====================================================================

def _build_error_friendly_view(
    *,
    kind: str,
    message_ru: str,
    partial_results: list[ToolResult] | None = None,
) -> RenderableView:
    from scripts.v2 import view_builders as vb
    partials = []
    for r in (partial_results or []):
        if not r:
            continue
        summary = ""
        if r.view is not None:
            summary = (r.view.headline or
                        f"view: {r.view.view_type.value}")
        elif r.data:
            summary = str(r.data)[:120]
        partials.append({"tool": r.tool, "summary": summary or "(no view)"})
    return vb.build_error_friendly(
        kind=kind,
        message_ru=message_ru,
        retry_hint_ru="Попробуй уточнить запрос или повторить через минуту.",
        partial_results=partials,
        language="ru",
    )


def _find_broken_tool(results: list[ToolResult]) -> ToolResult | None:
    for r in results:
        if r is not None and r.data_validity == DataValidity.BROKEN:
            return r
    return None


def _find_clarify_or_oos(results: list[ToolResult]) -> RenderableView | None:
    """If any ToolResult emits CLARIFY/OUT_OF_SCOPE/NOT_FOUND views,
    prefer those — they're terminal user-facing states."""
    for r in results:
        if r is None or r.view is None:
            continue
        if r.view.view_type in (ViewType.CLARIFY, ViewType.OUT_OF_SCOPE,
                                  ViewType.NOT_FOUND):
            return r.view
    return None


# =====================================================================
# Main entry — render_v5
# =====================================================================

def render_v5(
    question: str,
    plan,                              # QueryPlan or PlanSpec — opaque
    results: list[ToolResult],
    *,
    model: str | None = None,
    ollama_host: str | None = None,
    history: list | None = None,
    llm_call: pb.LLMCallable | None = None,
    enable_prose: bool = True,
    prose_timeout_s: float = pb.DEFAULT_LLM_TIMEOUT_S,
) -> tuple[str, dict]:
    """v5 renderer — drop-in replacement for `_llm_render`.

    Returns (answer_text, meta) where:

      answer_text — final markdown to show user
      meta        — {
        "view_type":        str,
        "skeleton_chars":   int,
        "prose_used":       bool,
        "prose_audit_failed": bool,
        "verification_failures": list[str],
        "phase_a_ms":       int,
        "phase_b_ms":       int,
        "fallback_reason":  str | None,
      }
    """
    t_total_start = time.perf_counter()
    meta: dict[str, Any] = {
        "view_type": None,
        "skeleton_chars": 0,
        "prose_used": False,
        "prose_audit_failed": False,
        "verification_failures": [],
        "phase_a_ms": 0,
        "phase_b_ms": 0,
        "fallback_reason": None,
    }

    # ---- Step 0 — fail-fast routes ----

    # If any result claims a broken tool, prefer ERROR_FRIENDLY view.
    broken = _find_broken_tool(results)
    if broken is not None:
        view = _build_error_friendly_view(
            kind="tool_internal",
            message_ru=(
                f"Tool `{broken.tool}` сообщил, что данные семантически "
                f"некорректны (data_validity=BROKEN). Это известный сбой "
                f"фильтра — мы видим его в golden-тестах и не показываем "
                f"вводящий в заблуждение ответ."
            ),
            partial_results=results,
        )
        meta["view_type"] = view.view_type.value
        meta["fallback_reason"] = "tool_broken"
        skeleton = te.render_view(view)
        meta["skeleton_chars"] = len(skeleton)
        return skeleton, meta

    # Clarify / OOS / NOT_FOUND views are terminal — render them alone.
    terminal = _find_clarify_or_oos(results)
    if terminal is not None:
        skeleton = te.render_view(terminal)
        meta["view_type"] = terminal.view_type.value
        meta["skeleton_chars"] = len(skeleton)
        meta["fallback_reason"] = f"terminal_view:{terminal.view_type.value}"
        return skeleton, meta

    # ---- Step 1 — pick the primary view ----
    # E16 — pass intent so word_contexts queries don't lose to
    # ETYMOLOGY_BUNDLE from a parallel enrich_word step.
    plan_intent = getattr(plan, "intent", None)
    primary_r, primary_v = select_primary_view(results, intent=plan_intent)
    if primary_v is None:
        # No views available — return a friendly empty message; the
        # legacy renderer would have called the LLM, but we're in v5
        # path so we surface a clean fallback.
        msg = "Все инструменты вернулись без типизированного view — "\
              "возможно, ни один шаг плана не дошёл до полного результата."
        view = _build_error_friendly_view(
            kind="unknown", message_ru=msg, partial_results=results,
        )
        skeleton = te.render_view(view)
        meta["view_type"] = view.view_type.value
        meta["skeleton_chars"] = len(skeleton)
        meta["fallback_reason"] = "no_views_in_results"
        return skeleton, meta

    # ---- Step 2 — Phase A deterministic render ----
    t_a = time.perf_counter()
    try:
        skeleton = te.render_view(primary_v)
    except Exception as e:
        log.warning("render_view raised on %s: %s", primary_v.view_type, e)
        # Phase A failed — fall back to error_friendly
        view = _build_error_friendly_view(
            kind="renderer",
            message_ru=f"Сбой в детерминированном шаблоне ({primary_v.view_type.value}): {e}",
            partial_results=results,
        )
        skeleton = te.render_view(view)
        meta["view_type"] = view.view_type.value
        meta["fallback_reason"] = "phase_a_exception"
        meta["skeleton_chars"] = len(skeleton)
        return skeleton, meta
    meta["phase_a_ms"] = int((time.perf_counter() - t_a) * 1000)
    meta["view_type"] = primary_v.view_type.value
    meta["skeleton_chars"] = len(skeleton)

    # ---- Step 3 — Phase B prose binding (optional) ----
    if not enable_prose or not pb.V5_PROSE_BINDER_ENABLED:
        # Phase B disabled — return skeleton alone
        return skeleton, meta

    t_b = time.perf_counter()
    prose = pb.bind_prose(
        view=primary_v,
        skeleton=skeleton,
        question=question,
        history=history,
        llm_call=llm_call,
        llm_timeout_s=prose_timeout_s,
        enable_llm=True,
    )
    meta["phase_b_ms"] = int((time.perf_counter() - t_b) * 1000)
    meta["prose_used"] = prose.used_llm
    meta["prose_audit_failed"] = not prose.verification_passed
    meta["verification_failures"] = list(prose.verification_failures)

    if not prose.verification_passed or (not prose.intro and not prose.next_steps):
        # Phase B dropped — return skeleton alone
        return skeleton, meta

    prose_md = prose.to_markdown()
    final = skeleton.rstrip()
    if prose.intro:
        final = prose.intro.strip() + "\n\n" + final
    if prose.next_steps:
        final = final + "\n\n" + "\n".join(
            ["**Что ещё можно спросить:**"]
            + [f"- {s}" for s in prose.next_steps]
        )

    return final, meta


# Module marker
V5_RENDER_V5_VERSION = "0.1"


__all__ = [
    "render_v5", "select_primary_view",
    "V5_RENDERER_ENABLED", "V5_RENDER_V5_VERSION",
]
