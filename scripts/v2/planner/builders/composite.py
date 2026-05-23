"""Composite / country / period / genre / topic plan builders.

`country_compare`, `composite_compare`, `country_vocab`, `period_vocab`,
`genre_compare`, `topic_words`, `topic_book_search`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import (
    PlanStep,
    QueryPlan,
)
from scripts.v2.planner.builders.word import _plan_word_collocates


def _plan_country_compare(e: Entities) -> QueryPlan:
    """Q12/Q23: «BrE vs AmE». Use compare via top_authors_by_country + affinity
    fragments. For v2-alpha we kick off with top_authors_by_country(GB)
    plus a follow-up suggestion in the explain."""
    return QueryPlan(
        intent="country_compare", entities=e,
        steps=[
            PlanStep(tool="top_authors_by_country",
                     args={"country": "GB", "metric": "books", "top": 10}),
            PlanStep(tool="top_authors_by_country",
                     args={"country": "US", "metric": "books", "top": 10}),
        ],
        expected_cost="medium",
        explain="top_authors_by_country GB + US — потом пользователь может выбрать affinity per author",
    )


def _plan_composite_compare(e: Entities) -> QueryPlan:
    """Sprint 11.4 — Q40-style extreme cross-section.

    Q40: «Возьми все английские произведения 1850-1920, раздели на британских
    и американских, ... покажи 200 слов B2-C1 которые отличают британскую
    прозу от американской». Full per-corpus affinity over the period would
    scan ~20k books and blow the chat budget, so we approximate with:

      0. top_authors_by_country(GB, metric=tokens, top=10) — cached via
         Sprint 11.2 author_tokens.json, ~50ms.
      1. top_authors_by_country(US, metric=tokens, top=10) — same.
      2. affinity_by_author for leader of GB top — cached CSV → ~1s.
      3. affinity_by_author for leader of US top — cached CSV → ~1s.

    The LLM/renderer then surfaces both leaders' signature words side-by-side
    with the country top-author lists, giving Stan a real lexical contrast
    instead of just "here are top authors per country." Full B2-C1 lemma
    differential across the whole period is a Sprint 12 corpus-side
    pre-computation (build_country_affinity.py is a future enhancement).
    """
    yf = e.year_from or 1850
    yt = e.year_to or 1920
    return QueryPlan(
        intent="composite_compare", entities=e,
        steps=[
            PlanStep(tool="top_authors_by_country",
                     args={"country": "GB", "metric": "tokens", "top": 10}),
            PlanStep(tool="top_authors_by_country",
                     args={"country": "US", "metric": "tokens", "top": 10}),
            PlanStep(tool="affinity_by_author",
                     args={"top": 30, "min_corpus_count": 500,
                           "pos_filter": e.pos_filter},
                     depends_on=[0], inject_result_as="author_regex",
                     optional=True),
            PlanStep(tool="affinity_by_author",
                     args={"top": 30, "min_corpus_count": 500,
                           "pos_filter": e.pos_filter},
                     depends_on=[1], inject_result_as="author_regex",
                     optional=True),
        ],
        expected_cost="medium",
        explain=(
            f"top_authors_by_country GB + US ({yf}-{yt}) + "
            f"affinity для leader каждой страны (composite Q40-style)"
        ),
    )


def _plan_country_vocab(e: Entities) -> QueryPlan:
    """Q6: «британские слова Кристи». Author vocab + country filter."""
    if not e.author_regex:
        return QueryPlan(
            intent="country_vocab", entities=e,
            steps=[PlanStep(tool="top_authors_by_country",
                            args={"country": e.country or "GB",
                                  "top": e.top_n or 20})],
            expected_cost="medium",
            explain=f"top_authors_by_country({e.country or 'GB'})",
        )
    return QueryPlan(
        intent="country_vocab", entities=e,
        steps=[PlanStep(tool="affinity_by_author",
                        args={"author_regex": e.author_regex,
                              "top": e.top_n or 30,
                              "min_corpus_count": 500,
                              "pos_filter": e.pos_filter})],
        expected_cost="medium",
        explain=f"affinity_by_author({e.author_regex}) — country filter on follow-up",
    )


def _plan_period_vocab(e: Entities) -> QueryPlan:
    text_lower = (e.raw_misc.get("raw_text") or "").lower()
    # Q38-style: «женских персонажей викторианской литературы» — gender of
    # speaking characters isn't annotated in our corpus. Refuse politely
    # rather than burn 120s on a top_ngrams scan that can't answer the real
    # question anyway.
    if any(k in text_lower for k in ("женск", "мужск", "female character",
                                     "male character", "gender")):
        return QueryPlan(
            intent="period_vocab", entities=e, steps=[],
            out_of_scope_reason=(
                "Гендер персонажей не размечен в корпусе SPGC — нет тегирования "
                "диалогов и speaker'ов. Могу показать общую лексику периода "
                "(`top_ngrams_by_author` с year_from/year_to) или фирменные слова "
                "конкретных авториц (Austen, Eliot, Gaskell, Bronte)."
            ),
            explain="period_vocab + gender → no annotation",
        )
    yf, yt = e.year_from, e.year_to
    if not yf and not yt:
        yf, yt = 1837, 1901
    return QueryPlan(
        intent="period_vocab", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": e.author_regex or ".*",
                              "n": 1, "top": min(e.top_n or 25, 30),
                              "pos_filter": e.pos_filter,
                              "year_from": yf, "year_to": yt,
                              "country": e.country})],
        expected_cost="heavy",
        explain=(f"top_ngrams_by_author over {yf}-{yt}"
                 f"{f', country={e.country}' if e.country else ''}, top≤30"),
    )


def _plan_genre_compare(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="genre_compare", entities=e, steps=[],
        needs_clarify=False,
        out_of_scope_reason=(
            "Жанровая разметка корпуса пока не размечена. "
            "Могу предложить ближайшее: сравни конкретных авторов "
            "(compare_authors), или фильтр по периоду + country."
        ),
        explain="genre_compare → soft refusal с предложением альтернативы",
    )


def _plan_topic_words(e: Entities) -> QueryPlan:
    """Q33-style topic queries: «слова в описаниях тумана/дождя/моря».

    Two-step heuristic:
      1. If user quoted a word — straight to word_collocates with their scope.
      2. Otherwise extract an English anchor from Russian topic stem
         (тумана→fog, дождя→rain). Default scope to 19th century when none
         given — these queries are usually about classical literature, not
         the entire 75k-book corpus, and a default keeps wall time under 90s
         (word_collocates already caps at max_books=8000).
    """
    if e.word:
        return _plan_word_collocates(e)
    text_lower = (e.raw_misc.get("raw_text") or "").lower()
    for candidate, en in (("туман", "fog"), ("дожд", "rain"),
                          ("сыр", "damp"), ("мор", "sea"),
                          ("fog", "fog"), ("rain", "rain"),
                          ("sea", "sea")):
        if candidate in text_lower:
            e.word = en
            # If user didn't specify a period, default to the 19th century —
            # otherwise scope_from would return "all_corpus" and the tool
            # would clarify-out unhelpfully.
            if not (e.author_regex or e.book_id or e.year_from or e.year_to):
                e.year_from = 1800
                e.year_to = 1900
            return _plan_word_collocates(e)
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=("Уточни ключевое слово в кавычках, например "
                          "«collocates слова \"fog\"», «слова рядом с \"rain\"»."),
        explain="topic_words needs anchor word",
    )


_TOPIC_FILLER_RE = None


def _strip_topic_filler(raw: str) -> str:
    """Strip «найди / посоветуй / book about» prefixes so the topic we
    hand to semantic search is just the topical phrase."""
    import re
    global _TOPIC_FILLER_RE
    if _TOPIC_FILLER_RE is None:
        _TOPIC_FILLER_RE = re.compile(
            r"^\s*(?:найди|поищи|посоветуй|подскажи|recommend|find|"
            r"что\s+почитать)\s+"
            # «me a / для меня / мне» — pile-up of soft fillers; allow
            # each independently so «find me a book» strips cleanly.
            r"(?:мне\s+)?(?:me\s+)?(?:a\s+)?(?:an\s+)?"
            r"(?:книг\w*|роман\w*|произведен\w*|book|novel)\s+"
            r"(?:про|о|об|на\s+тему|about|on)\s+",
            re.IGNORECASE,
        )
    cleaned = _TOPIC_FILLER_RE.sub("", raw).strip(" ?!.,;:«»\"")
    return cleaned


def _plan_topic_book_search(e: Entities) -> QueryPlan:
    """«Найди книгу про викторианский Лондон» — semantic search over
    chunks, dedupe by pg_id, return book-shaped rows.

    The full raw query goes in as the topic — hybrid_search's semantic
    side is robust to filler («найди книгу про …»), and the BGE rerank
    (if available) further suppresses non-topical results.

    Sprint 20+ B4: when lang_hint is set («английская классика про X»),
    surface a render note. The corpus is ~95% English but contains
    multilingual chunks (Finnish, Hungarian, Italian) that can leak
    into topical search; renderer is told to disclose if non-matching
    language results appear."""
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    topic = _strip_topic_filler(raw) or raw
    notes: list[str] = []
    if e.lang_hint and e.lang_hint != "en":
        # Non-English requested → highlight that our corpus is mostly EN
        notes.append(
            f"Пользователь явно просил язык '{e.lang_hint}', но корпус "
            f"в основном английский. Если в результатах смесь языков — "
            f"DISCLOSE: «корпус Project Gutenberg в основном английский, "
            f"для целевого языка результаты могут быть ограничены»."
        )
    elif e.lang_hint == "en":
        # English explicit → guard against leaking non-EN chunks
        notes.append(
            "Пользователь явно просил английскую литературу. Если в "
            "результатах появятся книги на других языках (Finnish, "
            "Hungarian, Italian — встречаются в Project Gutenberg), "
            "EXCLUDE их из ответа или DISCLOSE отдельно."
        )
    return QueryPlan(
        intent="topic_book_search", entities=e,
        steps=[PlanStep(tool="find_book_by_topic",
                        # Sprint 18 — BGE cross-encoder rerank by default
                        # for topical book search. RRF gives the candidate
                        # pool, cross-encoder reorders for true topical
                        # relevance. ~1-2s extra latency, big quality lift.
                        args={"topic": topic, "top": 8,
                              "rerank_with": "bge_reranker"})],
        expected_cost="medium",
        explain=f"topic_book_search → find_book_by_topic(topic={topic!r}, "
                "rerank=bge_reranker)",
        render_notes=notes,
    )
