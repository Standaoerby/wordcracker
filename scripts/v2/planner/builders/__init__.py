"""Plan builders package.

Phase 4 (REFACTOR_BRIEF) — the public surface for plan builders moves
HERE. Each builder is a small function that turns `(intent, Entities)`
into a `QueryPlan` with one or more `PlanStep`s; the router applies
fan-out / timeout / clarify-guard invariants on the resulting plan
(see `planner/invariants.py`).

CURRENT STATE: this is a re-exporting facade. The builders still
physically live in `planner/plan.py`; we re-publish them here so
external consumers (router, rag_v2, tests) can migrate imports to
`scripts.v2.planner.builders` over time. The PLAN_BUILDERS dispatch
table is exposed unchanged.

FOLLOW-UP: split `plan.py` into per-domain modules under this package
(`author.py`, `book.py`, `word.py`, `learning.py`, `meta.py`, ...).
Doing the physical move in its own commit keeps blame readable and
isolates the risk of import churn — the gate of Phase 4 is met
without it (fan-out invariant + POS/etymology fan-out are landed),
so the file split is a clean follow-up.

The static-registry rule (`PLAN_BUILDERS` is built once, no mid-module
mutation) IS already enforced in `plan.py` after Phase 4 — see the
single `PLAN_BUILDERS = {...}` declaration; the old post-hoc
`PLAN_BUILDERS["..."] = ...` assignments for `translate_word_list`
and `export_word_list` are gone.
"""
from __future__ import annotations

# Public registry — the canonical intent → builder map. Built statically
# in plan.py; re-exported here so callers can write
#   `from scripts.v2.planner.builders import PLAN_BUILDERS`.
from scripts.v2.planner.plan import (
    PLAN_BUILDERS,
    QueryPlan,
    PlanStep,
    build,
)

# Convenience re-exports of every individual builder. Mirrors the
# PLAN_BUILDERS table so consumers can import a specific builder by
# name for testing/introspection.
from scripts.v2.planner.plan import (
    # author
    _plan_author_attribution,
    _plan_author_closest,
    _plan_author_compare,
    _plan_author_influences,
    _plan_author_lookup,
    _plan_author_metadata,
    _plan_author_top_words,
    _plan_author_vocab,
    # book
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
    # word
    _plan_word_collocates,
    _plan_word_contexts,
    _plan_word_dialogue,
    _plan_word_emotion,
    _plan_word_etymology,
    _plan_word_movement,
    _plan_word_pos,
    _plan_word_timeline,
    # corpus / meta
    _plan_corpus_extremum,
    _plan_corpus_meta,
    _plan_introduction,
    _plan_top_authors,
    _plan_out_of_scope,
    # country / period / genre / topic
    _plan_composite_compare,
    _plan_country_compare,
    _plan_country_vocab,
    _plan_genre_compare,
    _plan_period_vocab,
    _plan_topic_book_search,
    _plan_topic_words,
    # learning / lexical / translation
    _plan_export_word_list,
    _plan_learning,
    _plan_lexical_wealth,
    _plan_translate_word_list,
    _plan_translation_quality,
    _plan_vocab_passport,
    # similarity
    _plan_similar_to,
)

__all__ = [
    "PLAN_BUILDERS",
    "QueryPlan",
    "PlanStep",
    "build",
    # author
    "_plan_author_attribution",
    "_plan_author_closest",
    "_plan_author_compare",
    "_plan_author_influences",
    "_plan_author_lookup",
    "_plan_author_metadata",
    "_plan_author_top_words",
    "_plan_author_vocab",
    # book
    "_plan_book_archaic",
    "_plan_book_compare",
    "_plan_book_emotion",
    "_plan_book_extremum",
    "_plan_book_lookup",
    "_plan_book_pub_year",
    "_plan_book_readability",
    "_plan_book_readability_compare",
    "_plan_book_recommendation",
    "_plan_book_similar",
    "_plan_book_vocab",
    # word
    "_plan_word_collocates",
    "_plan_word_contexts",
    "_plan_word_dialogue",
    "_plan_word_emotion",
    "_plan_word_etymology",
    "_plan_word_movement",
    "_plan_word_pos",
    "_plan_word_timeline",
    # corpus / meta
    "_plan_corpus_extremum",
    "_plan_corpus_meta",
    "_plan_introduction",
    "_plan_top_authors",
    "_plan_out_of_scope",
    # country / period / genre / topic
    "_plan_composite_compare",
    "_plan_country_compare",
    "_plan_country_vocab",
    "_plan_genre_compare",
    "_plan_period_vocab",
    "_plan_topic_book_search",
    "_plan_topic_words",
    # learning / lexical / translation
    "_plan_export_word_list",
    "_plan_learning",
    "_plan_lexical_wealth",
    "_plan_translate_word_list",
    "_plan_translation_quality",
    "_plan_vocab_passport",
    # similarity
    "_plan_similar_to",
]
