"""Corpus / meta-level plan builders.

`introduction`, `corpus_meta`, `corpus_extremum`, `out_of_scope`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import PlanStep, QueryPlan


def _plan_introduction(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="introduction", entities=e, steps=[],
        expected_cost="cheap",
        explain="ответил без вызова tools — это representational/self-intro",
    )


def _plan_corpus_meta(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="corpus_meta", entities=e,
        steps=[PlanStep(tool="corpus_overview", args={})],
        expected_cost="cheap",
        explain="вызову corpus_overview",
    )


def _plan_corpus_extremum(e: Entities) -> QueryPlan:
    """«Самый плодовитый/популярный автор» — singleton case of top_authors_by.

    Picks the metric from raw query text since classifier rules trigger on
    «плодовитый» (books), «популярный» (downloads), «читаемый» (downloads).
    """
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    raw_lc = raw.lower()
    if any(w in raw_lc for w in ("плодовит", "написал", "prolific", "books")):
        metric = "books"
    elif any(w in raw_lc for w in ("популярн", "читаем", "скачиваем",
                                    "popular", "downloaded", "read")):
        metric = "downloads"
    else:
        metric = "books"   # safe default
    return QueryPlan(
        intent="corpus_extremum", entities=e,
        steps=[PlanStep(tool="top_authors_by",
                        args={"metric": metric, "top": 1})],
        expected_cost="medium",
        explain=f"corpus_extremum → top_authors_by(metric={metric}, top=1)",
    )


def _plan_out_of_scope(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="out_of_scope", entities=e, steps=[],
        out_of_scope_reason=(
            "Я аналитик корпуса Project Gutenberg, не генератор. "
            "Не пишу художку и не отвечаю на запросы вне корпуса. "
            "Могу показать фирменные слова, биграммы, обороты автора."
        ),
        explain="out_of_scope refusal",
    )
