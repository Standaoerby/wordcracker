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
    # Sprint 14 — inline contextual help: instead of «нужен автор» / «уточни»
    # show concrete reformulations the user can copy. Include the captured
    # raw query as scaffold so they can edit it minimally.
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    hint = ""
    if raw:
        # Show what they likely meant: «у Doyle / Wodehouse / Достоевского»
        hint = (
            f"\n\nТы написал: «{raw[:120]}». Добавь автора:\n"
            f"• «… у Doyle»\n"
            f"• «… у Wodehouse»\n"
            f"• «… у Достоевского»"
        )
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            f"Для этого нужен {what}.{hint}"
        ),
        explain="запросил у пользователя автора",
    )


def _need_book(e: Entities) -> QueryPlan:
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    hint = ""
    if raw:
        hint = (
            f"\n\nТы написал: «{raw[:120]}». Добавь книгу:\n"
            f"• «… в \"Pride and Prejudice\"»\n"
            f"• «… в \"Dracula\"»\n"
            f"• «… в \"Преступление и наказание\"»"
        )
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            f"Уточни название книги или PG id (например, PG1342).{hint}"
        ),
        explain="запросил у пользователя книгу",
    )


# Targeted analog suggestions per copyright author — better UX than a
# generic «вот несколько имён». Keys are substrings of KNOWN_BOOKS
# canonical names; first match wins.
_COPYRIGHT_ANALOGS = {
    "lord of the rings": ("Tolkien", "Уильям Моррис «The Well at the World's End» (PG169) — тот же архаичный fantasy-стиль, прямо влиял на Толкина"),
    "hobbit":            ("Tolkien", "Уильям Моррис «The House of the Wolfings» (PG2885) — древнегерманские/норвежские корни, как у Толкина"),
    "1984":              ("Orwell", "Джозеф Конрад «Heart of Darkness» (PG219) — тёмная дистопическая тональность, B2+"),
    "nineteen eighty":   ("Orwell", "Джозеф Конрад «Heart of Darkness» (PG219) — тёмная дистопическая тональность"),
    "old man and the sea": ("Hemingway", "Mark Twain «Adventures of Huckleberry Finn» (PG76) — ближайший американский лаконичный стиль из public-domain"),
}


def _copyright_refusal_if_book_under_copyright(e: Entities) -> QueryPlan | None:
    """If the user named a known but unavailable book (Tolkien / Hemingway /
    Orwell / etc. — recorded in KNOWN_BOOKS with empty PG id), return an
    `out_of_scope` plan with a structured explanation:
    1. WHAT we have for this book (metadata only — title, downloads,
       author bio, year — via Gutendex if the PG entry exists)
    2. WHY we don't have the full text (copyright window)
    3. WHICH public-domain analog we'd recommend instead

    Stan's 2026-05-18 feedback was that the old refusal felt curt — just
    «отсутствует в корпусе». Users want to know what they CAN ask about
    even when full-text analysis is off the table.

    Returns None when this isn't the situation."""
    if e.book_title and not e.book_id:
        from scripts.v2.planner.entities import KNOWN_BOOKS
        key = e.book_title.lower().replace("’", "'").replace("‘", "'")
        # «Old Man and the Sea» without leading «the» misses
        # «the old man and the sea» in KNOWN_BOOKS. Try both variants —
        # users routinely drop the leading article when typing.
        candidates = [key]
        if not key.startswith("the "):
            candidates.append("the " + key)
        elif key.startswith("the "):
            candidates.append(key[4:])
        match_key = next((c for c in candidates if c in KNOWN_BOOKS), None)
        if match_key:
            _pg, canonical = KNOWN_BOOKS[match_key]
            key = match_key  # so the analog lookup below uses the canonical form
            if not _pg:
                analog_hint = ""
                for sub, (_author, hint) in _COPYRIGHT_ANALOGS.items():
                    if sub in key:
                        analog_hint = f"\n\n**Ближайший аналог в public-domain:** {hint}."
                        break
                if not analog_hint:
                    analog_hint = (
                        "\n\n**Ближайший аналог в public-domain:** для "
                        "Tolkien-стиля — Уильям Моррис «The Well at the "
                        "World's End», для Hemingway — Mark Twain, для "
                        "Orwell — Джозеф Конрад «Heart of Darkness»."
                    )
                return QueryPlan(
                    intent="out_of_scope", entities=e, steps=[],
                    out_of_scope_reason=(
                        f"«{canonical}» — книга всё ещё под защитой "
                        f"copyright, **полнотекстовый анализ невозможен**: "
                        f"в Project Gutenberg доступны только public-domain "
                        f"тексты (US до ~1929, UK до ~1973).\n\n"
                        f"**Что доступно по этой книге:** только мета-"
                        f"информация (название, автор, year, downloads из "
                        f"Gutendex API, если книга вообще зарегистрирована). "
                        f"Стилометрию / частоты / affinity / контексты "
                        f"посчитать не на чем — токенизированного текста нет."
                        f"{analog_hint}"
                    ),
                    explain=f"book under copyright: {canonical}",
                )
    return None


def _with_copyright_check(builder):
    """Decorator: short-circuit any book-touching plan builder with the
    copyright OOS refusal when the user named an unavailable title. The
    explicit `refusal = _copyright_refusal_if_book_under_copyright(e); if
    refusal: return refusal` was repeated 8 times across builders; this
    keeps the single check in one place and the builder bodies focused on
    the happy path.
    """
    def wrapped(e: Entities) -> QueryPlan:
        refusal = _copyright_refusal_if_book_under_copyright(e)
        if refusal:
            return refusal
        return builder(e)
    wrapped.__name__ = builder.__name__
    wrapped.__doc__ = builder.__doc__
    return wrapped


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
    """Build the legacy `scope` dict that v1 tools accept.

    Most v1 tools reject `"all_corpus"` as a scope — they want `{'author': ...}`
    or `{'book': ...}`. So when the entity has a period/country filter but no
    explicit author, we widen the scope to `{'author': '.*', ...filters}` which
    v1's `_select_books` resolves to "all books matching these filters".
    """
    if e.book_id:
        return {"book": e.book_id}
    has_filters = bool(e.country or e.year_from or e.year_to)
    if e.author_regex or has_filters:
        scope = {"author": e.author_regex or ".*"}
        if e.country:
            scope["country"] = e.country
        if e.year_from:
            scope["year_from"] = e.year_from
        if e.year_to:
            scope["year_to"] = e.year_to
        return scope
    return "all_corpus"


def _scope_dict_or_clarify(e: Entities, *, intent: str, hint: str) -> "QueryPlan | dict":
    """Helper for tools that strictly require a dict scope. Returns either a
    valid scope dict or a clarify QueryPlan if no scope was extractable."""
    scope = _scope_from(e)
    if scope == "all_corpus":
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=hint,
            explain=f"{intent} requires explicit scope (author/book/period)",
        )
    return scope


# Authors whose English translations leak transliterated character /
# place names that look like real lexemes to spaCy POS (Gavril, Lisaveta,
# Korsakoff, kibitka, mossoo, beaupre — all appeared in the affinity for
# Pushkin at min_corpus_count=100). For these we bump the floor so the
# `corpus_count` filter aggressively drops transliterations even when
# spaCy mistags them as NOUN/ADJ instead of PROPN.
_HIGH_TRANSLIT_AUTHORS = frozenset({
    "^Pushkin,", "^Tolstoy,", "^Dostoyevsky,", "^Chekhov,",
    "^Turgenev,", "^Gogol,", "^Lermontov,", "^Bulgakov,",
})


def _auto_min_corpus_count(e: Entities) -> int:
    """Heuristic: bump min_corpus_count to drop OOV proper nouns.

    Floor was 100 — too soft, let through Pushkin character names
    (Gavril/Korsakoff/etc) at corpus_count 200-800. Raised default to 500
    (matches POS / country case) and an aggressive 1500 for Russian
    authors where transliterated names are systemically noisier.
    """
    if e.author_regex in _HIGH_TRANSLIT_AUTHORS:
        return 1500
    if e.pos_filter or e.country:
        return 500
    return 500


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


def _plan_book_lookup(e: Entities) -> QueryPlan:
    """Q2 (Stan's 2026-05-18 demon round): «найди книгу X» = pure resolution
    query. Run `find_book` directly. If extractor already pinned a PG id
    via KNOWN_BOOKS substring scan, just return that — no tool call
    needed."""
    if e.book_id:
        # Synthesize a find_book-shape response from KNOWN_BOOKS so the
        # renderer has structured data to talk about.
        return QueryPlan(
            intent="book_lookup", entities=e, steps=[
                PlanStep(tool="find_book",
                         args={"title": e.book_title or e.book_id})],
            expected_cost="cheap",
            explain=f"find_book({e.book_title or e.book_id}) — known book {e.book_id}",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_lookup", entities=e, steps=[
                PlanStep(tool="find_book", args={"title": e.book_title})],
            expected_cost="cheap",
            explain=f"find_book({e.book_title})",
        )
    # User said «найди книгу» but didn't name one — extract the rest of
    # the sentence after the trigger as the title query.
    text = (e.raw_misc or {}).get("raw_text", "")
    import re
    m = re.search(r"\b(?:найди|поищи)\s+книг\w*\s+(.+)$", text, re.IGNORECASE)
    title = (m.group(1).strip(" \"«»") if m else "")
    if not title:
        return _need_book(e)
    return QueryPlan(
        intent="book_lookup", entities=e, steps=[
            PlanStep(tool="find_book", args={"title": title})],
        expected_cost="cheap",
        explain=f"find_book({title}) — extracted from trigger",
    )


def _plan_top_authors(e: Entities) -> QueryPlan:
    # Honor `top_metric` when user said «по скачиваниям» / «по токенам».
    # Default falls back to «books». Stan's 2026-05-18 demon caught this:
    # «топ-5 британских авторов по скачиваниям» used to silently sort by
    # books even when the response table included downloads — confusing.
    metric = e.top_metric or "books"
    # top_authors_by_country only supports books/downloads; coerce tokens
    # to downloads which is the closest «popularity» proxy when filtered
    # by country.
    if e.country:
        country_metric = "downloads" if metric == "tokens" else metric
        return QueryPlan(
            intent="top_authors_books", entities=e,
            steps=[PlanStep(tool="top_authors_by_country",
                            args={"country": e.country,
                                  "metric": country_metric,
                                  "top": e.top_n or 20})],
            expected_cost="medium",
            explain=f"top_authors_by_country({e.country}, metric={country_metric})",
        )
    return QueryPlan(
        intent="top_authors_books", entities=e,
        steps=[PlanStep(tool="top_authors_by",
                        args={"metric": metric, "top": e.top_n or 10})],
        expected_cost="medium",
        explain=f"top_authors_by(metric={metric})",
    )


def _plan_author_top_words(e: Entities) -> QueryPlan:
    """«самое частотное слово автора» / «топ слов X» — raw unigram counts,
    not affinity. Routes to top_ngrams_by_author(n=1) so the user gets the
    actual zipf head (mostly stopwords filtered) for a quick stylistic
    fingerprint that doesn't require comparison to the rest of the corpus.

    Stan round 2 Q18: «топ-15 биграмм у Конан Дойла» — same intent but
    n=2. Detect bigram/trigram triggers in raw text and bump `n`.
    """
    if not e.author_regex:
        return _need_author(e)
    text_lower = (e.raw_misc.get("raw_text") or "").lower()
    import re
    if re.search(r"\bтриграмм|trigram", text_lower):
        n = 3
    elif re.search(r"\bбиграмм|bigram", text_lower):
        n = 2
    else:
        n = 1
    return QueryPlan(
        intent="author_top_words", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": e.author_regex,
                              "n": n, "top": e.top_n or 20,
                              "pos_filter": e.pos_filter})],
        expected_cost="medium",
        explain=f"top_ngrams_by_author({e.author_regex}, n={n}) — raw frequency",
    )


@_with_copyright_check
def _plan_author_vocab(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    # Sprint 11.3: when several authors are named (Q27 «морские авторы —
    # Мелвилл, Конрад, Стивенсон»), run affinity_by_author for each in
    # parallel — the renderer can present the joined signature lists or
    # compute set intersections downstream.
    steps = [PlanStep(tool="affinity_by_author",
                      args={"author_regex": e.author_regex,
                            "top": e.top_n or 30,
                            "min_corpus_count": _auto_min_corpus_count(e),
                            "pos_filter": e.pos_filter})]
    for extra in e.multi_author_regex[:3]:  # cap at 4 total to bound time
        steps.append(PlanStep(
            tool="affinity_by_author",
            args={"author_regex": extra,
                  "top": e.top_n or 30,
                  "min_corpus_count": _auto_min_corpus_count(e),
                  "pos_filter": e.pos_filter},
            optional=True))
    explain = f"affinity_by_author({e.author_regex})"
    if e.multi_author_regex:
        explain += f" + {len(e.multi_author_regex[:3])} more"
    return QueryPlan(
        intent="author_vocab", entities=e, steps=steps,
        expected_cost="medium",
        explain=explain,
    )


@_with_copyright_check
def _plan_book_compare(e: Entities) -> QueryPlan:
    """Q24-style: «слова в Treasure Island и Moby Dick, но редко в David
    Copperfield». Strategy: find_book each title (cached), then run
    affinity_by_book on each — the renderer surfaces words common to the
    positive set and rare in the negative.

    For v2-alpha we fire affinity_by_book on the *first* resolved book
    only and let the renderer say «here's signature for X — compare with
    Y/Z by re-asking». Full set-intersection is Sprint 9.x deferred.
    """
    # Need at least the primary book.
    if not e.book_id and not e.book_title:
        return _need_book(e)
    if e.book_id:
        return QueryPlan(
            intent="book_compare", entities=e,
            steps=[PlanStep(tool="affinity_by_book",
                            args={"pg_id": e.book_id,
                                  "top": e.top_n or 30,
                                  "min_corpus_count": 500,
                                  "exclude_proper_nouns": True})],
            expected_cost="medium",
            explain=(f"affinity_by_book({e.book_id}) — primary book; "
                     f"renderer suggests follow-up for secondary titles"),
        )
    return QueryPlan(
        intent="book_compare", entities=e,
        steps=[
            PlanStep(tool="find_book", args={"title": e.book_title}),
            PlanStep(tool="affinity_by_book",
                     args={"top": e.top_n or 30,
                           "min_corpus_count": 500,
                           "exclude_proper_nouns": True},
                     depends_on=[0], inject_result_as="pg_id"),
        ],
        expected_cost="medium",
        explain="find_book → affinity_by_book (primary); renderer asks for next",
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
    # Probe both authors first via cheap author_metadata. compare_authors
    # internally rebuilds affinity CSV if missing, which fails silently when
    # the second author has zero books in SPGC (Hemingway etc → corpus-side
    # gap). The probes are optional and the router gracefully continues to
    # compare_authors regardless — but they let the renderer warn the user
    # ahead of time and suggest closest available authors instead.
    #
    # min_corpus_count=2000 (was 500): character names like Pickwick / Weller
    # / Heep / Nickleby / Squeers / Trotwood occur 1000-1800 times in PG via
    # cross-references in commentaries and adaptations, so 500 wasn't strict
    # enough to filter them out. 2000 keeps actual stylistic markers
    # (cheerily / drawing-room / villainous / waistcoat / etc) while
    # cutting the character-name floor.
    return QueryPlan(
        intent="author_compare", entities=e,
        steps=[
            PlanStep(tool="author_metadata",
                     args={"author_regex": e.author_regex},
                     optional=True),
            PlanStep(tool="author_metadata",
                     args={"author_regex": others[0]},
                     optional=True),
            PlanStep(tool="compare_authors",
                     args={"author1_regex": e.author_regex,
                           "author2_regex": others[0],
                           "top": e.top_n or 20,
                           "min_corpus_count": 2000}),
        ],
        expected_cost="medium",
        explain=(f"probe({e.author_regex}) + probe({others[0]}) + "
                 f"compare_authors(min_corpus_count=2000 anti-PROPN)"),
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


@_with_copyright_check
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


@_with_copyright_check
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


@_with_copyright_check
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


@_with_copyright_check
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
        # Sprint 17 (Round 7 Q8): «примеры ajar у Остин/Диккенса/Дойла»
        # used to dispatch only to Austen. multi_author_regex was already
        # captured by the extractor but the plan ignored it. Now we emit a
        # word_contexts step per author (cap 4 total) and let the renderer
        # merge by author.
        steps = [PlanStep(
            tool="word_contexts",
            args={"author_regex": e.author_regex,
                  "word": e.word, "max_samples": 8},
        )]
        for extra in e.multi_author_regex[:3]:
            steps.append(PlanStep(
                tool="word_contexts",
                args={"author_regex": extra,
                      "word": e.word, "max_samples": 5},
                optional=True,
            ))
        explain = f"word_contexts({e.author_regex}, {e.word})"
        if e.multi_author_regex:
            explain += f" + {len(e.multi_author_regex[:3])} more authors"
        return QueryPlan(
            intent="word_contexts", entities=e, steps=steps,
            expected_cost="cheap",
            explain=explain,
        )
    # No author scope → hybrid_search if FTS5 is available, else legacy
    # word_contexts_global. hybrid pulls 30 from each retriever, RRF-merges
    # to top 12, and surfaces both exact mentions ('ajar' literally) and
    # semantically related passages ('half-open door', 'door slightly open').
    return QueryPlan(
        intent="word_contexts", entities=e,
        steps=[PlanStep(tool="hybrid_search",
                        args={"query": e.word, "k": 12, "per_retriever": 30})],
        expected_cost="medium",
        explain=f"hybrid_search({e.word}) — FTS5 + ChromaDB RRF merge",
    )


def _plan_word_collocates(e: Entities) -> QueryPlan:
    if not e.word:
        return _need_word(e)
    scope_or_plan = _scope_dict_or_clarify(
        e, intent="word_collocates",
        hint=("Уточни — у какого автора или книги ищем соседей слова? "
              "Или укажи период (например «викторианский»)."),
    )
    if isinstance(scope_or_plan, QueryPlan):
        return scope_or_plan
    return QueryPlan(
        intent="word_collocates", entities=e,
        steps=[PlanStep(tool="word_collocates",
                        args={"scope": scope_or_plan, "word": e.word,
                              "window": 4, "top": e.top_n or 20})],
        expected_cost="medium",
        explain=f"word_collocates({scope_or_plan}, {e.word})",
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


@_with_copyright_check
def _plan_word_etymology(e: Entities) -> QueryPlan:
    if e.author_regex and e.etymology_family:
        scope = {"author": e.author_regex}
        # Heavy tool — each candidate word triggers a Wiktionary HTTP call.
        # Cap top at 20 and bump min_corpus_count to 1000 so the candidate
        # pool stays small enough to finish under the 90s chat timeout.
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[PlanStep(tool="find_words_by_etymology",
                            args={"scope": scope, "family": e.etymology_family,
                                  "top": min(e.top_n or 15, 20),
                                  "min_corpus_count": 1000})],
            expected_cost="heavy",
            explain=f"find_words_by_etymology({scope}, family={e.etymology_family}, top≤20)",
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
    scope_or_plan = _scope_dict_or_clarify(
        e, intent="word_emotion",
        hint=("Уточни scope: у какого автора/книги/в какой эпохе искать "
              "эмоциональный контекст? Пример: «слова страха у По» или "
              "«мрачные слова у викторианцев»."),
    )
    if isinstance(scope_or_plan, QueryPlan):
        return scope_or_plan
    emotion = e.emotion or "fear"
    return QueryPlan(
        intent="word_emotion", entities=e,
        steps=[PlanStep(tool="emotion_collocates",
                        args={"scope": scope_or_plan, "emotion": emotion,
                              "window": 4, "top": e.top_n or 25})],
        expected_cost="medium",
        explain=f"emotion_collocates({scope_or_plan}, {emotion})",
    )


@_with_copyright_check
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
    # Cap top — anything over ~30 triggers per-word enrich loops that don't
    # finish under the 90s chat timeout. The renderer should offer the user
    # an "ещё 30" follow-up once the first batch lands.
    requested = e.top_n or 30
    eff_top = min(requested, 30)
    # When user explicitly asked for more than the cap, smuggle the original
    # request in via `_capped_from` so the wrapper can emit a ToolWarning the
    # LLM sees and mentions in the answer. Without this, Q21-style «300 слов»
    # silently returns 30 without acknowledging the user's number.
    args = {"scope": scope, "level": e.level or "intermediate",
            "top": eff_top, "lemmatize": True}
    if requested > eff_top:
        args["_capped_from"] = requested
    return QueryPlan(
        intent="learning", entities=e,
        steps=[PlanStep(tool="learning_words", args=args)],
        expected_cost="medium",
        explain=(f"learning_words({scope}, level={e.level or 'intermediate'}, "
                 f"top={eff_top}{f' [capped from {requested}]' if requested > 30 else ''})"),
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
    # Top-ngrams with author_regex='.*' over a 100-year window scans
    # ~20k books per token (5GB+ of token files). Even with top≤30 cap
    # and POS filter it consistently blows past the 120s chat budget.
    # Without an author or smaller scope we can't satisfy this query
    # in chat — be honest about that. Suggest a narrower scope.
    if not e.author_regex and not e.country and not e.book_id:
        return QueryPlan(
            intent="word_movement", entities=e, steps=[],
            out_of_scope_reason=(
                "Запрос «глаголы движения в XIX веке» требует сканирования "
                "20k+ книг — это превышает бюджет чата (90-120с). Сузь "
                "scope: укажи автора («у Диккенса»), страну («британские»), "
                "или конкретную книгу. Можно также спросить про глаголы "
                "у конкретного автора через `affinity_by_author(pos_filter=['VERB'])`."
            ),
            explain="word_movement без scope → too expensive for chat",
        )
    yf, yt = e.year_from, e.year_to
    if not yf and not yt:
        yf, yt = 1800, 1899
    return QueryPlan(
        intent="word_movement", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": e.author_regex or ".*",
                              "n": 1, "top": min(e.top_n or 25, 30),
                              "pos_filter": ["VERB"],
                              "year_from": yf, "year_to": yt,
                              "country": e.country})],
        expected_cost="heavy",
        explain=f"top_ngrams_by_author over {yf}-{yt}, POS=VERB, top≤30",
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


# ===== Sprint 17 — book_readability_compare =====


def _plan_book_readability_compare(e: Entities) -> QueryPlan:
    """«Что сложнее читать, X или Y» — runs book_readability on each
    resolved book; renderer compares Flesch / CEFR side by side.

    Requires at least 2 books. With just 1, falls back to single-book
    readability (better than refusing). When primary book has no PG id
    but does have a title, prepend a find_book step to resolve."""
    # Gather all books — primary + secondaries
    book_ids: list[str] = []
    book_titles_unresolved: list[str] = []
    if e.book_id:
        book_ids.append(e.book_id)
    elif e.book_title:
        book_titles_unresolved.append(e.book_title)
    for pg, title in zip(e.multi_book_ids, e.multi_book_titles):
        if pg:
            book_ids.append(pg)
        elif title:
            book_titles_unresolved.append(title)

    total = len(book_ids) + len(book_titles_unresolved)
    if total == 0:
        return _need_book(e)
    if total == 1:
        # Single-book → fall through to plain book_readability
        return _plan_book_readability(e)

    steps: list[PlanStep] = []
    # Cap at 3 books to bound wall-clock (book_readability is fast,
    # but renderer can only meaningfully compare 2-3 in one answer).
    for pg in book_ids[:3]:
        steps.append(PlanStep(tool="book_readability", args={"pg_id": pg}))
    # Resolve unresolved titles via find_book → readability chain
    for title in book_titles_unresolved[: 3 - len(steps)]:
        idx = len(steps)
        steps.append(PlanStep(tool="find_book", args={"title": title}))
        steps.append(PlanStep(
            tool="book_readability", args={},
            depends_on=[idx], inject_result_as="pg_id",
        ))
    return QueryPlan(
        intent="book_readability_compare", entities=e,
        steps=steps,
        expected_cost="medium",
        explain=f"book_readability × {total} books — Flesch/CEFR side-by-side",
    )


# ===== Sprint 16 Phase G — book_pub_year =====


def _plan_book_pub_year(e: Entities) -> QueryPlan:
    """«Когда была опубликована Война и мир» — surface pub_year from
    Open Library enrichment via find_book (Sprint 9.7).

    Render hint tells the LLM to look for `pub_year` in matches[0] —
    not authoryearofbirth (a common renderer confusion: «1828» for the
    author year vs «1869» for the book)."""
    if e.book_id:
        return QueryPlan(
            intent="book_pub_year", entities=e,
            steps=[PlanStep(tool="find_book",
                            args={"title": e.book_title or e.book_id})],
            expected_cost="cheap",
            explain=f"book_pub_year → find_book({e.book_title or e.book_id})",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_pub_year", entities=e,
            steps=[PlanStep(tool="find_book",
                            args={"title": e.book_title})],
            expected_cost="cheap",
            explain=f"book_pub_year → find_book({e.book_title!r})",
        )
    return _need_book(e)


# ===== Sprint 16 Phase F — topic_book_search =====


def _plan_topic_book_search(e: Entities) -> QueryPlan:
    """«Найди книгу про викторианский Лондон» — semantic search over
    chunks, dedupe by pg_id, return book-shaped rows.

    The full raw query goes in as the topic — hybrid_search's semantic
    side is robust to filler («найди книгу про …»), and the BGE rerank
    (if available) further suppresses non-topical results."""
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    topic = _strip_topic_filler(raw) or raw
    return QueryPlan(
        intent="topic_book_search", entities=e,
        steps=[PlanStep(tool="find_book_by_topic",
                        args={"topic": topic, "top": 8})],
        expected_cost="medium",
        explain=f"topic_book_search → find_book_by_topic(topic={topic!r})",
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


# ===== Sprint 16 Phase E — meta-query plan builders =====


def _plan_author_lookup(e: Entities) -> QueryPlan:
    """«Какие книги у X» / «list books by X» — reuses author_metadata which
    already returns `sample_titles` (up to 10) + `books_matched` count.
    Renderer formats the list; the LLM prompt's strict-facts rule keeps
    it bounded to actual data."""
    if not e.author_regex:
        return _need_author(e)
    return QueryPlan(
        intent="author_lookup", entities=e,
        steps=[PlanStep(tool="author_metadata",
                        args={"author_regex": e.author_regex})],
        expected_cost="cheap",
        explain=f"author_lookup → author_metadata({e.author_regex}) "
                "for sample_titles + books_matched",
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


def _plan_book_extremum(e: Entities) -> QueryPlan:
    """«Самая популярная книга» → top_books_by_downloads(top=1).
    Other superlatives (longest, simplest, oldest) don't have a single
    universal tool — route to clarify with a targeted hint until v3.1.

    Stan's Phase E brief: focus on routing the most-common queries; the
    long tail of «самая редкая» / «самая древняя» is deferred."""
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    raw_lc = raw.lower()
    if any(w in raw_lc for w in ("популярн", "скачиваем", "читаем",
                                  "popular", "downloaded", "most read")):
        return QueryPlan(
            intent="book_extremum", entities=e,
            steps=[PlanStep(tool="top_books_by_downloads",
                            args={"top": 1})],
            expected_cost="medium",
            explain="book_extremum → top_books_by_downloads(top=1)",
        )
    # For length/complexity/rarity extremums we don't have a one-shot tool.
    # Route to clarify with a useful menu of options. The Phase G long-tail
    # work can land specific tools later.
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            "Single-book superlatives кроме «самая популярная» пока требуют "
            "точного критерия. Попробуй:\n"
            "• «топ-10 самых популярных книг» → top_books_by_downloads\n"
            "• «самая длинная книга у Doyle» → сравнение книг автора (book_compare)\n"
            "• «топ-5 книг по readability» → пока вручную через book_readability\n"
            "\nНадо вычислить экстремум по другому полю? Скажи конкретнее."
        ),
        explain="book_extremum → clarify (no single-book extremum tool yet)",
    )


# ===== dispatch table =====


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
