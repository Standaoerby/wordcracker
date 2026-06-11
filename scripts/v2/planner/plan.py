"""Plan Builder — turn (intent, entities) into a deterministic tool chain.

Contract: docs/v2/PLANNER.md §4.

Output: a `QueryPlan` with one or more `PlanStep`s. The router executes each
step, threading prior results into later args where `inject_result_as` is set.

Each plan template is a small function so it's easy to test in isolation.

Phase 4 (REFACTOR_BRIEF) — fan-out / timeout / clarify-guard are now
PLAN-LEVEL invariants applied by the router (see `invariants.py`).
Builders MUST NOT re-implement fan-out per-builder (R6). To opt a step
into the multi-author fan-out, set `PlanStep.fan_out` to one of:
  - "scope_author"   when args is `{"scope": {"author": ...}, ...}`
  - "author_regex"   when args is `{"author_regex": ..., ...}`
The invariant clones the step per `entities.multi_author_regex[:cap]`
and clears the marker (idempotent).

Phase 4 / T4 (REMEDIATION_BRIEF) — `plan.py` is now an orchestrator:
types + helpers + builders live under `scripts/v2/planner/builders/`
(one module per domain). This file re-exports them for backward
compat (tests and external code import many symbols from this path)
and owns: (1) the static `PLAN_BUILDERS` registry, (2) the `build()`
entry point that delegates to a builder and applies plan-level
invariants. No `_plan_*` body lives here.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities

# --- types + shared helpers (defined in builders/_common.py) ---
from scripts.v2.planner.builders._common import (
    AUTHORS_UNDER_COPYRIGHT,
    Cost,
    PlanStep,
    QueryPlan,
    _ambiguous_author_clarify,
    _auto_min_corpus_count,
    _author_label_lc,
    _copyright_refusal_if_author_under_copyright,
    _copyright_refusal_if_book_under_copyright,
    _fan_out_authors_steps,
    _HIGH_TRANSLIT_AUTHORS,
    _need_author,
    _need_book,
    _need_country,
    _need_word,
    _scope_dict_or_clarify,
    _scope_from,
    _smart_clarify_recipe,
    _with_author_copyright_check,
    _with_copyright_check,
)

# --- per-domain plan builders ---
from scripts.v2.planner.builders.author import (
    _plan_author_attribution,
    _plan_author_closest,
    _plan_author_compare,
    _plan_author_influences,
    _plan_author_lookup,
    _plan_author_metadata,
    _plan_author_top_words,
    _plan_author_vocab,
    _plan_top_authors,
)
from scripts.v2.planner.builders.book import (
    _plan_book_archaic,
    _plan_book_compare,
    _plan_book_emotion,
    _plan_book_extremum,
    _plan_book_lookup,
    _plan_book_pub_year,
    _plan_book_readability,
    _plan_book_readability_compare,
    _plan_book_recommendation,
    _plan_book_similar,
    _plan_book_vocab,
)
from scripts.v2.planner.builders.composite import (
    _plan_composite_compare,
    _plan_country_compare,
    _plan_country_vocab,
    _plan_genre_compare,
    _plan_period_vocab,
    _plan_topic_book_search,
    _plan_topic_words,
)
from scripts.v2.planner.builders.corpus import (
    _plan_corpus_extremum,
    _plan_corpus_meta,
    _plan_introduction,
    _plan_out_of_scope,
)
from scripts.v2.planner.builders.learning import (
    _plan_export_word_list,
    _plan_learning,
    _plan_learning_books,
    _plan_lexical_wealth,
    _plan_translate_word_list,
    _plan_translation_quality,
    _plan_vocab_passport,
)
from scripts.v2.planner.builders.similarity import _plan_similar_to
from scripts.v2.planner.builders.word import (
    _plan_word_collocates,
    _plan_word_contexts,
    _plan_word_dialogue,
    _plan_word_emotion,
    _plan_word_etymology,
    _plan_word_movement,
    _plan_word_pos,
    _plan_word_timeline,
)


# ===== dispatch table =====
#
# T4 (REMEDIATION_BRIEF Part 4) — `PLAN_BUILDERS` is a STATIC single-
# declaration registry. Adding a new intent means appending to this
# dict literal (and importing the builder above), NOT mutating the
# dict elsewhere. The old `PLAN_BUILDERS[...] = ...` post-hoc
# assignments (translate_word_list, export_word_list) are gone.

PLAN_BUILDERS = {
    "introduction":         _plan_introduction,
    "corpus_meta":          _plan_corpus_meta,
    "author_metadata":      _plan_author_metadata,
    "author_vocab":         _plan_author_vocab,
    "author_top_words":     _plan_author_top_words,
    "author_compare":       _plan_author_compare,
    "book_compare":         _plan_book_compare,
    "author_attribution":   _plan_author_attribution,
    "author_influences":    _plan_author_influences,
    "author_closest":       _plan_author_closest,
    "lexical_wealth":       _plan_lexical_wealth,
    "book_vocab":           _plan_book_vocab,
    "book_readability":     _plan_book_readability,
    "book_archaic":         _plan_book_archaic,
    "book_emotion":         _plan_book_emotion,
    "book_recommendation":  _plan_book_recommendation,
    "word_contexts":        _plan_word_contexts,
    "word_collocates":      _plan_word_collocates,
    "word_timeline":        _plan_word_timeline,
    "word_pos":             _plan_word_pos,
    "word_etymology":       _plan_word_etymology,
    "word_emotion":         _plan_word_emotion,
    "learning":             _plan_learning,
    # R-27 WP1 (B106) — книги для изучающих язык
    "learning_books":       _plan_learning_books,
    "top_authors_books":    _plan_top_authors,
    "book_lookup":          _plan_book_lookup,
    "country_compare":      _plan_country_compare,
    "country_vocab":        _plan_country_vocab,
    "composite_compare":    _plan_composite_compare,
    "period_vocab":         _plan_period_vocab,
    "genre_compare":        _plan_genre_compare,
    "topic_words":          _plan_topic_words,
    "translation_quality":  _plan_translation_quality,
    "vocab_passport":       _plan_vocab_passport,
    "word_dialogue":        _plan_word_dialogue,
    "word_movement":        _plan_word_movement,
    # Sprint 16 Phase E — meta-query intents
    "author_lookup":        _plan_author_lookup,
    "corpus_extremum":      _plan_corpus_extremum,
    "book_extremum":        _plan_book_extremum,
    # Sprint 16 Phase F — semantic find_book by topic
    "topic_book_search":    _plan_topic_book_search,
    # Sprint 16 Phase G — pub_year + RU genitive titles
    "book_pub_year":        _plan_book_pub_year,
    # Sprint 17 — readability comparison
    "book_readability_compare": _plan_book_readability_compare,
    # Sprint 17 — books similar to a reference book
    "book_similar":         _plan_book_similar,
    # Sprint 18 — ambiguous similarity router («в стиле X»)
    "similar_to":           _plan_similar_to,
    # Sprint 20 — translate-followup with prior-words handoff
    "translate_word_list":  _plan_translate_word_list,
    # Sprint 20+ B3 — export-followup
    "export_word_list":     _plan_export_word_list,
    "out_of_scope":         _plan_out_of_scope,
}


def _regex_to_canonical(regex: str | None) -> str | None:
    """`^Doyle, Arthur` → `Doyle, Arthur`. `^Wodehouse,` → `Wodehouse`."""
    if not regex:
        return None
    s = regex.lstrip("^").rstrip(",").strip()
    return s


def build(intent: str, entities: Entities) -> QueryPlan:
    fn = PLAN_BUILDERS.get(intent)
    if fn is None:
        # Sprint 19+ — smart recipe when entities are rich
        recipe = _smart_clarify_recipe(entities)
        if recipe:
            return QueryPlan(
                intent="clarify", entities=entities, steps=[],
                needs_clarify=True,
                clarify_question=recipe,
                explain="rich entities, compound research query — recipe offered",
            )
        # clarify or unknown intent — generic example menu
        return QueryPlan(
            intent="clarify", entities=entities, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Не уверен, что ты имеешь в виду. Спроси конкретнее — например: "
                "«фирменные слова Wodehouse», «уровень сложности Pride and Prejudice», "
                "«германские слова Толкина»."
            ),
            explain="не определил intent с достаточной уверенностью",
        )
    plan = fn(entities)
    # Phase 4 — apply plan-level invariants at the canonical entry
    # point. Builders emit single-step plans with `fan_out` markers;
    # the invariant expands them per `multi_author_regex`. Router
    # re-applies invariants for defense in depth (idempotent) so direct
    # `router.execute(QueryPlan(...))` callers also get the expansion.
    from scripts.v2.planner.invariants import apply_invariants
    return apply_invariants(plan)
