"""Plan Builder — turn (intent, entities) into a deterministic tool chain.

Contract: docs/v2/PLANNER.md §4.

Output: a `QueryPlan` with one or more `PlanStep`s. The router executes each
step, threading prior results into later args where `inject_result_as` is set.

Each plan template is a small function so it's easy to test in isolation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from scripts.v2.planner.entities import Entities


Cost = Literal["cheap", "medium", "heavy"]


@dataclass
class PlanStep:
    tool: str
    args: dict
    depends_on: list[int] = field(default_factory=list)
    inject_result_as: str | None = None  # key in next step's args to fill
    optional: bool = False


@dataclass
class QueryPlan:
    intent: str
    entities: Entities
    steps: list[PlanStep]
    fallback_steps: list[PlanStep] = field(default_factory=list)
    expected_cost: Cost = "medium"
    needs_clarify: bool = False
    clarify_question: str | None = None
    explain: str = ""
    out_of_scope_reason: str | None = None


# ===== helpers =====


def _need_author(e: Entities, what: str = "автор") -> QueryPlan:
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            f"Для этого нужен {what}. Уточни — например: "
            f"«у Wodehouse», «у Doyle», «у Достоевского»."
        ),
        explain="запросил у пользователя автора",
    )


def _need_book(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            "Уточни название книги или PG id. Пример: "
            "«Pride and Prejudice» / «PG1342» / «Преступление и наказание»."
        ),
        explain="запросил у пользователя книгу",
    )


def _need_word(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question="Уточни какое слово. Пример: «слово \"fog\"», «слово ajar».",
        explain="запросил у пользователя слово",
    )


def _need_country(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question="Какая страна? GB / US / RU / FR — что именно сравнивать?",
        explain="запросил country",
    )


def _scope_from(e: Entities) -> dict | str:
    """Build the legacy `scope` dict that v1 tools accept."""
    if e.book_id:
        return {"book": e.book_id}
    if e.author_regex:
        scope = {"author": e.author_regex}
        if e.country:
            scope["country"] = e.country
        if e.year_from:
            scope["year_from"] = e.year_from
        if e.year_to:
            scope["year_to"] = e.year_to
        return scope
    return "all_corpus"


def _auto_min_corpus_count(e: Entities) -> int:
    """Heuristic: when filtering by POS or asking for 'характерные', bump
    min_corpus_count to drop OOV proper nouns. Matches v1 prompt rule 7."""
    if e.pos_filter or e.country:
        return 500
    return 100


# ===== plan templates =====


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


def _plan_author_metadata(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="author_metadata", entities=e,
        steps=[PlanStep(tool="author_metadata",
                        args={"author_regex": e.author_regex})],
        expected_cost="cheap",
        explain=f"вызову author_metadata({e.author_regex})",
    )


def _plan_top_authors(e: Entities) -> QueryPlan:
    if e.country:
        return QueryPlan(
            intent="top_authors_books", entities=e,
            steps=[PlanStep(tool="top_authors_by_country",
                            args={"country": e.country,
                                  "metric": "books",
                                  "top": e.top_n or 20})],
            expected_cost="medium",
            explain=f"top_authors_by_country({e.country})",
        )
    return QueryPlan(
        intent="top_authors_books", entities=e,
        steps=[PlanStep(tool="top_authors_by",
                        args={"metric": "books", "top": e.top_n or 10})],
        expected_cost="medium",
        explain="top_authors_by",
    )


def _plan_author_vocab(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="author_vocab", entities=e,
        steps=[PlanStep(tool="affinity_by_author",
                        args={"author_regex": e.author_regex,
                              "top": e.top_n or 30,
                              "min_corpus_count": _auto_min_corpus_count(e),
                              "pos_filter": e.pos_filter})],
        expected_cost="medium",
        explain=f"affinity_by_author({e.author_regex})",
    )


def _plan_author_compare(e: Entities) -> QueryPlan:
    others = e.multi_author_regex
    if not e.author_regex or not others:
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question="Нужны два автора для сравнения. Пример: «сравни Wodehouse и Twain».",
            explain="запросил второго автора",
        )
    return QueryPlan(
        intent="author_compare", entities=e,
        steps=[PlanStep(tool="compare_authors",
                        args={"author1_regex": e.author_regex,
                              "author2_regex": others[0],
                              "top": e.top_n or 20,
                              "min_corpus_count": 500})],
        expected_cost="medium",
        explain=f"compare_authors({e.author_regex}, {others[0]})",
    )


def _plan_author_closest(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="author_closest", entities=e,
        steps=[PlanStep(tool="author_influences",
                        args={"author_regex": e.author_regex,
                              "top": e.top_n or 10})],
        expected_cost="medium",
        explain=f"author_influences({e.author_regex}) — closest neighbours by Burrows Delta",
    )


def _plan_author_attribution(e: Entities) -> QueryPlan:
    text = (e.raw_misc or {}).get("attribution_text")
    if not text:
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question="Вставь сам текст, который нужно атрибутировать (хотя бы 500 слов).",
            explain="запросил текст для author_attribution",
        )
    return QueryPlan(
        intent="author_attribution", entities=e,
        steps=[PlanStep(tool="author_attribution",
                        args={"text": text, "top": e.top_n or 5})],
        expected_cost="medium",
        explain="author_attribution",
    )


def _plan_author_influences(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="author_influences", entities=e,
        steps=[PlanStep(tool="author_influences",
                        args={"author_regex": e.author_regex,
                              "top": e.top_n or 10})],
        expected_cost="medium",
        explain=f"author_influences({e.author_regex})",
    )


def _plan_book_vocab(e: Entities) -> QueryPlan:
    if e.book_id:
        return QueryPlan(
            intent="book_vocab", entities=e,
            steps=[PlanStep(tool="affinity_by_book",
                            args={"pg_id": e.book_id,
                                  "top": e.top_n or 30,
                                  "pos_filter": e.pos_filter,
                                  "min_corpus_count": 200,
                                  "exclude_proper_nouns": True})],
            expected_cost="medium",
            explain=f"affinity_by_book({e.book_id})",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_vocab", entities=e,
            steps=[
                PlanStep(tool="find_book",
                         args={"title": e.book_title}),
                PlanStep(tool="affinity_by_book",
                         args={"top": e.top_n or 30,
                               "pos_filter": e.pos_filter,
                               "min_corpus_count": 200,
                               "exclude_proper_nouns": True},
                         depends_on=[0],
                         inject_result_as="pg_id"),
            ],
            expected_cost="medium",
            explain=f"find_book → affinity_by_book для «{e.book_title}»",
        )
    return _need_book(e)


def _plan_book_readability(e: Entities) -> QueryPlan:
    if e.book_id:
        return QueryPlan(
            intent="book_readability", entities=e,
            steps=[PlanStep(tool="book_readability",
                            args={"pg_id": e.book_id})],
            expected_cost="cheap",
            explain=f"book_readability({e.book_id})",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_readability", entities=e,
            steps=[
                PlanStep(tool="find_book",
                         args={"title": e.book_title}),
                PlanStep(tool="book_readability", args={},
                         depends_on=[0], inject_result_as="pg_id"),
            ],
            expected_cost="cheap",
            explain="find_book → book_readability",
        )
    return _need_book(e)


def _plan_book_archaic(e: Entities) -> QueryPlan:
    if e.book_id:
        return QueryPlan(
            intent="book_archaic", entities=e,
            steps=[PlanStep(tool="book_archaic_words",
                            args={"pg_id": e.book_id, "top": e.top_n or 30})],
            expected_cost="medium",
            explain=f"book_archaic_words({e.book_id})",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_archaic", entities=e,
            steps=[
                PlanStep(tool="find_book", args={"title": e.book_title}),
                PlanStep(tool="book_archaic_words",
                         args={"top": e.top_n or 30},
                         depends_on=[0], inject_result_as="pg_id"),
            ],
            expected_cost="medium",
            explain="find_book → book_archaic_words",
        )
    return _need_book(e)


def _plan_book_emotion(e: Entities) -> QueryPlan:
    if e.book_id:
        return QueryPlan(
            intent="book_emotion", entities=e,
            steps=[PlanStep(tool="book_emotion_profile",
                            args={"pg_id": e.book_id})],
            expected_cost="medium",
            explain=f"book_emotion_profile({e.book_id})",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_emotion", entities=e,
            steps=[
                PlanStep(tool="find_book", args={"title": e.book_title}),
                PlanStep(tool="book_emotion_profile", args={},
                         depends_on=[0], inject_result_as="pg_id"),
            ],
            expected_cost="medium",
            explain="find_book → book_emotion_profile",
        )
    return _need_book(e)


def _plan_book_recommendation(e: Entities) -> QueryPlan:
    """Q30: «произведения для читателя B2 без архаизмов».

    Use top_books_by_downloads as a popularity proxy, then renderer says
    'check book_readability for each' — we can't filter by CEFR globally
    until BookProfile pipeline (Sprint 4) is online."""
    return QueryPlan(
        intent="book_recommendation", entities=e,
        steps=[PlanStep(tool="top_books_by_downloads",
                        args={"top": 20, "lang": "en"})],
        expected_cost="medium",
        explain="topular books → user filters by readability manually",
    )


def _plan_word_contexts(e: Entities) -> QueryPlan:
    if not e.word:
        return _need_word(e)
    if e.author_regex:
        return QueryPlan(
            intent="word_contexts", entities=e,
            steps=[PlanStep(tool="word_contexts",
                            args={"author_regex": e.author_regex,
                                  "word": e.word, "max_samples": 8})],
            expected_cost="cheap",
            explain=f"word_contexts({e.author_regex}, {e.word})",
        )
    return QueryPlan(
        intent="word_contexts", entities=e,
        steps=[PlanStep(tool="word_contexts_global",
                        args={"word": e.word, "k": 12})],
        expected_cost="medium",
        explain=f"word_contexts_global({e.word})",
    )


def _plan_word_collocates(e: Entities) -> QueryPlan:
    if not e.word:
        return _need_word(e)
    scope = _scope_from(e)
    return QueryPlan(
        intent="word_collocates", entities=e,
        steps=[PlanStep(tool="word_collocates",
                        args={"scope": scope, "word": e.word,
                              "window": 4, "top": e.top_n or 20})],
        expected_cost="medium",
        explain=f"word_collocates({scope}, {e.word})",
    )


def _plan_word_timeline(e: Entities) -> QueryPlan:
    if e.year_from and not e.year_to:
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=[PlanStep(tool="words_disappearing_after",
                            args={"year": e.year_from - 1, "top": e.top_n or 25})],
            expected_cost="medium",
            explain=f"words_disappearing_after({e.year_from - 1})",
        )
    if e.word:
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=[PlanStep(tool="word_freq_timeline",
                            args={"word": e.word, "bucket_years": 25})],
            expected_cost="medium",
            explain=f"word_freq_timeline({e.word})",
        )
    return QueryPlan(
        intent="word_timeline", entities=e,
        steps=[PlanStep(tool="words_disappearing_after",
                        args={"year": 1920, "top": e.top_n or 25})],
        expected_cost="medium",
        explain="words_disappearing_after default",
    )


def _plan_word_pos(e: Entities) -> QueryPlan:
    if not e.word and not e.book_id:
        # default sample word that v1 prompt uses
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question="Уточни — какое слово проверить на полисемию? И в какой книге/у какого автора?",
            explain="word_pos needs target word",
        )
    scope = _scope_from(e)
    return QueryPlan(
        intent="word_pos", entities=e,
        steps=[PlanStep(tool="word_pos_distribution",
                        args={"scope": scope, "word": e.word or "light"})],
        expected_cost="cheap",
        explain=f"word_pos_distribution({scope}, {e.word or 'light'})",
    )


def _plan_word_etymology(e: Entities) -> QueryPlan:
    if e.author_regex and e.etymology_family:
        scope = {"author": e.author_regex}
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[PlanStep(tool="find_words_by_etymology",
                            args={"scope": scope, "family": e.etymology_family,
                                  "top": e.top_n or 30})],
            expected_cost="heavy",
            explain=f"find_words_by_etymology({scope}, family={e.etymology_family})",
        )
    if e.word:
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[PlanStep(tool="word_etymology", args={"word": e.word})],
            expected_cost="cheap",
            explain=f"word_etymology({e.word})",
        )
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question="Этимологию какого слова — или нужно «германские/латинские слова у автора X»?",
        explain="etymology needs word or (author, family)",
    )


def _plan_word_emotion(e: Entities) -> QueryPlan:
    scope = _scope_from(e)
    emotion = e.emotion or "fear"
    return QueryPlan(
        intent="word_emotion", entities=e,
        steps=[PlanStep(tool="emotion_collocates",
                        args={"scope": scope, "emotion": emotion,
                              "window": 4, "top": e.top_n or 25})],
        expected_cost="medium",
        explain=f"emotion_collocates({scope}, {emotion})",
    )


def _plan_learning(e: Entities) -> QueryPlan:
    scope = _scope_from(e)
    if scope == "all_corpus":
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Для изучаемой лексики уточни: для какого автора или книги? "
                "Пример: «B1 vocab из Pride and Prejudice», «слова для Wodehouse»."
            ),
            explain="learning_words needs scope",
        )
    return QueryPlan(
        intent="learning", entities=e,
        steps=[PlanStep(tool="learning_words",
                        args={"scope": scope, "level": e.level or "intermediate",
                              "top": e.top_n or 30, "lemmatize": True})],
        expected_cost="medium",
        explain=f"learning_words({scope}, level={e.level or 'intermediate'})",
    )


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
    yf, yt = e.year_from, e.year_to
    if not yf and not yt:
        yf, yt = 1837, 1901  # default to Victorian
    return QueryPlan(
        intent="period_vocab", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": ".*",
                              "n": 1, "top": e.top_n or 30,
                              "pos_filter": e.pos_filter,
                              "year_from": yf, "year_to": yt})],
        expected_cost="heavy",
        explain=f"top_ngrams_by_author over {yf}-{yt}",
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
    if e.word:
        return _plan_word_collocates(e)
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question="Уточни топик. Пример: «слова рядом с fog», «collocates слова rain».",
        explain="topic_words needs anchor word",
    )


def _plan_word_dialogue(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="word_dialogue", entities=e, steps=[],
        out_of_scope_reason=(
            "Корпус не размечен на диалоги vs нарратив. Это требует "
            "отдельной аннотации, которой пока нет."
        ),
        explain="word_dialogue → out_of_scope для v2-alpha",
    )


def _plan_word_movement(e: Entities) -> QueryPlan:
    yf, yt = e.year_from, e.year_to
    if not yf and not yt:
        yf, yt = 1800, 1899
    return QueryPlan(
        intent="word_movement", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": ".*",
                              "n": 1, "top": e.top_n or 30,
                              "pos_filter": ["VERB"],
                              "year_from": yf, "year_to": yt})],
        expected_cost="heavy",
        explain=f"top_ngrams_by_author over {yf}-{yt}, POS=VERB",
    )


def _plan_lexical_wealth(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="lexical_wealth", entities=e,
        steps=[PlanStep(tool="top_authors_by",
                        args={"metric": "tokens", "top": e.top_n or 20})],
        expected_cost="heavy",
        explain="top_authors_by(metric=tokens) — proxy для богатства словаря",
    )


def _plan_vocab_passport(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="vocab_passport", entities=e,
        steps=[PlanStep(tool="author_profile",
                        args={"author_regex": e.author_regex})],
        expected_cost="heavy",
        explain=f"author_profile({e.author_regex}) — composite паспорт",
    )


def _plan_translation_quality(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="translation_quality", entities=e, steps=[],
        out_of_scope_reason=(
            "Параллельный корпус EN↔RU пока не подключён (Sprint 9.8). "
            "Могу показать фирменные слова автора, биграммы, обороты — "
            "но не сравнивать с переводами."
        ),
        explain="translation_quality → честный отказ",
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


# ===== dispatch table =====


PLAN_BUILDERS = {
    "introduction":         _plan_introduction,
    "corpus_meta":          _plan_corpus_meta,
    "author_metadata":      _plan_author_metadata,
    "author_vocab":         _plan_author_vocab,
    "author_compare":       _plan_author_compare,
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
    "top_authors_books":    _plan_top_authors,
    "country_compare":      _plan_country_compare,
    "country_vocab":        _plan_country_vocab,
    "period_vocab":         _plan_period_vocab,
    "genre_compare":        _plan_genre_compare,
    "topic_words":          _plan_topic_words,
    "translation_quality":  _plan_translation_quality,
    "vocab_passport":       _plan_vocab_passport,
    "word_dialogue":        _plan_word_dialogue,
    "word_movement":        _plan_word_movement,
    "out_of_scope":         _plan_out_of_scope,
}


def build(intent: str, entities: Entities) -> QueryPlan:
    fn = PLAN_BUILDERS.get(intent)
    if fn is None:
        # clarify or unknown intent
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
    return fn(entities)
