"""Plan builders package.

Phase 4 / T4 (REMEDIATION_BRIEF) — each builder is a small function
that turns `(intent, Entities)` into a `QueryPlan` with one or more
`PlanStep`s. The router applies fan-out / timeout / clarify-guard
invariants on the resulting plan (see `planner/invariants.py`).

Per-domain layout:

  _common.py    — `PlanStep`, `QueryPlan`, scope/clarify/copyright
                  helpers, smart-clarify recipe. NO `_plan_*` here.
  author.py     — author_metadata / vocab / compare / closest /
                  attribution / influences / lookup / top_authors.
  book.py       — book_lookup / vocab / readability / archaic /
                  emotion / recommendation / similar / pub_year /
                  extremum / readability_compare / book_compare.
  word.py       — word_contexts / collocates / timeline / pos /
                  etymology / emotion / dialogue / movement.
  corpus.py     — introduction / corpus_meta / corpus_extremum /
                  out_of_scope.
  composite.py  — country_compare / composite_compare / country_vocab /
                  period_vocab / genre_compare / topic_words /
                  topic_book_search.
  learning.py   — learning / lexical_wealth / vocab_passport /
                  translation_quality / translate_word_list /
                  export_word_list.
  similarity.py — similar_to.

`scripts/v2/planner/plan.py` owns the static `PLAN_BUILDERS` registry
and the `build()` entry point; it re-exports everything below for
backward-compat (tests import `_plan_*` and helpers directly from
`scripts.v2.planner.plan`).
"""
from __future__ import annotations

# Types + shared helpers
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

# Per-domain builders
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

__all__ = [
    # types / helpers
    "AUTHORS_UNDER_COPYRIGHT",
    "Cost",
    "PlanStep",
    "QueryPlan",
    "_HIGH_TRANSLIT_AUTHORS",
    "_ambiguous_author_clarify",
    "_auto_min_corpus_count",
    "_author_label_lc",
    "_copyright_refusal_if_author_under_copyright",
    "_copyright_refusal_if_book_under_copyright",
    "_fan_out_authors_steps",
    "_need_author",
    "_need_book",
    "_need_country",
    "_need_word",
    "_scope_dict_or_clarify",
    "_scope_from",
    "_smart_clarify_recipe",
    "_with_author_copyright_check",
    "_with_copyright_check",
    # author
    "_plan_author_attribution",
    "_plan_author_closest",
    "_plan_author_compare",
    "_plan_author_influences",
    "_plan_author_lookup",
    "_plan_author_metadata",
    "_plan_author_top_words",
    "_plan_author_vocab",
    "_plan_top_authors",
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
    "_plan_out_of_scope",
    # composite (country / period / genre / topic)
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
    "_plan_learning_books",
    "_plan_lexical_wealth",
    "_plan_translate_word_list",
    "_plan_translation_quality",
    "_plan_vocab_passport",
    # similarity
    "_plan_similar_to",
]
