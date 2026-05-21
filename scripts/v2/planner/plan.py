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
    # Sprint 20+ — plan-level render notes (separate from per-tool
    # data._render_note). Used when a plan must surface a contract /
    # limitation to the renderer that no individual tool would emit,
    # e.g. «exclude_archaic flag set but no archaic-density column yet».
    render_notes: list[str] = field(default_factory=list)
    # B-R17-1 stage3.2 v3 (2026-05-21 evening v3) — when rules-path
    # explicitly asks for clarification (e.g. ambiguous surname →
    # «which Wells?»), v4 LLM planner fallback in rag_v2 MUST NOT
    # override. Without this flag, rag_v2 line 984 sees
    # needs_clarify=True as «rules-path gave up → let LLM plan»
    # and constructs a generic top_books_by_downloads plan,
    # bypassing the deliberate disambiguation request.
    #
    # Set this to True in plan builders that emit intentional
    # clarify (not «give up» clarify):
    #   * _ambiguous_author_clarify  — multiple Wells/Tolstoy/etc.
    #   * (future) book ambiguity, etc.
    authoritative_clarify: bool = False


# ===== helpers =====


def _need_author(e: Entities, what: str = "автор") -> QueryPlan:
    # Sprint 14 — inline contextual help: instead of «нужен автор» / «уточни»
    # show concrete reformulations the user can copy. Include the captured
    # raw query as scaffold so they can edit it minimally.
    raw = (e.raw_misc or {}).get("raw_text", "") or ""

    # Sprint 17 Round 8 P0: when the user explicitly named an author that
    # we couldn't resolve (e.g. «теперь у Марло», «давай Уэбстера»), say
    # so honestly instead of a generic «нужен автор». This is the surface
    # of the silent-fallback prevention in history.merge_with_history —
    # if we got here with unresolved_author_named set, the user does
    # have an author in mind, we just don't recognize them.
    unresolved = (e.raw_misc or {}).get("unresolved_author_named")
    if unresolved:
        clarify = (
            f"Я не узнаю автора «{unresolved}». "
            f"Возможно опечатка или этот автор пока не в моих алиасах.\n\n"
            f"Попробуй:\n"
            f"• уточнить написание (например «у Marlowe» / «у Кристофера Марло»)\n"
            f"• передать как regex: «у ^Marlowe,» или подобный\n"
            f"• назвать другого автора из списка — я знаю Doyle, Wodehouse, "
            f"Достоевского, Толкина и ~90 других классиков"
        )
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=clarify,
            explain=f"unresolved author named: {unresolved!r}",
        )

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


def _ambiguous_author_clarify(e: Entities) -> QueryPlan | None:
    """B-R17-1 stage3.2 (Stan UX correction): if the user typed a
    bare surname that matches multiple canonical authors in the
    corpus, ask which one they meant instead of silently aggregating.

    Returns a `needs_clarify=True` QueryPlan with the candidate list
    when ambiguous, or None when the surname is unambiguous (≤1
    canonical / already specific alias / metadata unavailable).

    Used by `_plan_author_metadata`, `_plan_author_lookup`,
    `_plan_author_vocab`, `_plan_author_compare` etc. — anywhere
    we'd otherwise dispatch with an ambiguous surname regex.
    """
    cands = e.author_clarify_candidates or []
    if len(cands) < 2:
        return None
    # Build a human-readable list — surname extracted from first
    # candidate's canonical name.
    first_name = cands[0].get("name", "")
    surname = first_name.split(",", 1)[0].strip() or "автор"
    bullets = []
    for c in cands:
        name = c.get("name", "")
        dl = c.get("downloads", 0)
        books = c.get("books", 0)
        bullets.append(f"• {name} ({books} книг, {dl:,} загрузок)")
    bullet_block = "\n".join(bullets)
    clarify_text = (
        f"Под фамилией «{surname}» в корпусе несколько авторов. "
        f"Кого ты имеешь в виду?\n\n{bullet_block}\n\n"
        f"Уточни полное имя — например, «{first_name}» — "
        f"и я повторю запрос."
    )
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=clarify_text,
        explain=f"ambiguous surname «{surname}» — "
                f"{len(cands)} canonical candidates",
        # Stop v4 LLM-planner fallback in rag_v2 from overriding this
        # with a generic top_books plan. This IS the correct answer.
        authoritative_clarify=True,
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
                        f"copyright, поэтому в каноническом корпусе "
                        f"Project Gutenberg доступны только public-domain "
                        f"тексты (US до ~1929, UK до ~1973).\n\n"
                        f"**Что доступно по этой книге:**\n"
                        f"• **Мета-информация** (название, автор, год, "
                        f"downloads из Gutendex API) — если книга "
                        f"зарегистрирована;\n"
                        f"• **Полный анализ из загруженной локально копии** "
                        f"— если вы загрузили свою версию через `/admin/`, "
                        f"работаем с U-id (fair use для исследовательских "
                        f"целей). Стилометрия / частоты / affinity / "
                        f"контексты в этом случае считаются по вашему "
                        f"экземпляру, не по канону SPGC.\n\n"
                        f"Без локальной загрузки стилометрия не считается — "
                        f"токенизированного текста нет."
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


# Sprint 22+ Round 12 Q17 — per-author copyright OOS. Hemingway has
# metadata in SPGC (resolve_author_name finds him) but no tokenized
# books in /workspace/spgc (estate enforces copyright). When user
# asks «vocab passport Hemingway» the resolver succeeds but tools
# return empty → renderer hallucinated «not found», critic caught
# the contradiction. Now we surface a graceful 3-part OOS instead,
# matching the book-level B102 pattern.
#
# Inclusions: authors whose major works are still under US copyright
# (typically died after 1929 for US, 1953 for UK life+70). Some have
# scattered PG public-domain juvenilia but the corpus story is
# «mostly absent». For these authors, return the OOS BEFORE running
# heavy author-level tools and getting empty results.
AUTHORS_UNDER_COPYRIGHT: frozenset[str] = frozenset({
    # English/American — died 1950+, mostly under US copyright
    "hemingway",
    "steinbeck",
    "faulkner",
    "salinger",
    "harper lee", "lee harper",
    "fitzgerald",       # F. Scott Fitzgerald
    "orwell",
    "tolkien",
    "lewis", "c.s. lewis",   # C.S. Lewis
    "huxley",           # Aldous Huxley
    "nabokov",
    # Russian — modern Soviet/post-Soviet, no PG public-domain
    "булгаков", "bulgakov",
    "пастернак", "pasternak",
    "solzhenitsyn", "солженицын",
    "набоков",
    # Add as observed
})


def _author_label_lc(e: Entities) -> str:
    """Lowercase author label for copyright matching. Pulls from
    e.author_label (canonical) or strips e.author_regex of regex
    metachars."""
    if e.author_label:
        return e.author_label.lower().strip()
    if e.author_regex:
        # «^Hemingway,» → «hemingway»
        import re as _re
        cleaned = _re.sub(r"[\^$,\\.]", " ", e.author_regex).strip().lower()
        # Take first token (surname usually)
        return cleaned.split()[0] if cleaned.split() else ""
    return ""


def _copyright_refusal_if_author_under_copyright(e: Entities) -> QueryPlan | None:
    """Stan Round 12 Q17 — per-author copyright OOS.

    Returns a 3-part OOS QueryPlan (same shape as the book-level helper)
    when the named author is in AUTHORS_UNDER_COPYRIGHT. Without this,
    tools that lookup by author_regex returned empty (because copyright-
    locked authors have no SPGC tokens), and the renderer fabricated
    «not found» or empty content.

    Returns None when the author is either public-domain or
    unrecognized (clarify-class — book-level check still runs).
    """
    label = _author_label_lc(e)
    if not label:
        return None
    # Match by token; e.g. «hemingway» catches «Hemingway, Ernest»
    for locked in AUTHORS_UNDER_COPYRIGHT:
        if locked in label or label in locked:
            display = (e.author_label or label).strip()
            return QueryPlan(
                intent="out_of_scope", entities=e, steps=[],
                out_of_scope_reason=(
                    f"Стилометрический анализ {display} **недоступен в "
                    f"каноническом корпусе**: его работы всё ещё под "
                    f"защитой copyright (US ~1929 / UK life+70 — этот "
                    f"автор не успел стать public-domain).\n\n"
                    f"**Что доступно:**\n"
                    f"• **Мета-информация** (биография, годы, число книг "
                    f"в PG если есть юношеские public-domain работы);\n"
                    f"• **Полный анализ из загруженных копий** — если "
                    f"вы загрузили работы через `/admin/`, работаем "
                    f"с U-id (fair use для исследовательских целей).\n\n"
                    f"Без локальной загрузки vocab passport / affinity / "
                    f"compare_authors не считаются — токенизированного "
                    f"текста под этим автором в SPGC нет."
                ),
                explain=f"author under copyright: {label}",
            )
    return None


def _with_author_copyright_check(builder):
    """Decorator: short-circuit author-level plan builders with the
    per-author copyright OOS when applicable. Apply to vocab_passport,
    author_profile, author_vocab — anything that ASSUMES tokens exist
    under e.author_regex."""
    def wrapped(e: Entities) -> QueryPlan:
        refusal = _copyright_refusal_if_author_under_copyright(e)
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


@_with_author_copyright_check
def _plan_author_metadata(e: Entities) -> QueryPlan:
    if not e.author_regex:
        return _need_author(e)
    ambiguous = _ambiguous_author_clarify(e)
    if ambiguous is not None:
        return ambiguous
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
    needed.

    Sprint 19+: respect e.top_n — when set (typically by expansion
    follow-up like «покажи все книги серии»), pass through to find_book
    so the user gets the wider set."""
    top_n = e.top_n or 5
    if e.book_id:
        # Synthesize a find_book-shape response from KNOWN_BOOKS so the
        # renderer has structured data to talk about.
        return QueryPlan(
            intent="book_lookup", entities=e, steps=[
                PlanStep(tool="find_book",
                         args={"title": e.book_title or e.book_id,
                               "top": top_n})],
            expected_cost="cheap",
            explain=f"find_book({e.book_title or e.book_id}, top={top_n}) — known book {e.book_id}",
        )
    if e.book_title:
        return QueryPlan(
            intent="book_lookup", entities=e, steps=[
                PlanStep(tool="find_book",
                         args={"title": e.book_title, "top": top_n})],
            expected_cost="cheap",
            explain=f"find_book({e.book_title}, top={top_n})",
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
            PlanStep(tool="find_book", args={"title": title, "top": top_n})],
        expected_cost="cheap",
        explain=f"find_book({title}, top={top_n}) — extracted from trigger",
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


@_with_author_copyright_check
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
@_with_author_copyright_check
def _plan_author_vocab(e: Entities) -> QueryPlan:
    # Sprint 18 — book-scope override. «характерные прилагательные в
    # "Dorian Gray"» matched author_vocab intent (pattern «характерные
    # слов») but the user explicitly named a book, not an author. Fall
    # through to _plan_book_vocab instead of pestering for an author —
    # the book entity is enough to compute affinity_by_book signature.
    if not e.author_regex and (e.book_id or e.book_title):
        return _plan_book_vocab(e)
    if not e.author_regex:
        return _need_author(e)
    ambiguous = _ambiguous_author_clarify(e)
    if ambiguous is not None:
        return ambiguous
    # Sprint 20 — propn-strict modifier (from history follow-up «убери
    # имена собственные»). Crank min_corpus_count up so OOV proper
    # nouns and rare character names get dropped at the corpus-frequency
    # gate — over and above the v3.1.1 surname blocklist that already
    # runs in the affinity_by_author v2 wrapper.
    propn_strict = bool((e.raw_misc or {}).get("_propn_strict"))
    min_cc = max(_auto_min_corpus_count(e), 5000) if propn_strict \
        else _auto_min_corpus_count(e)
    # Sprint 11.3: when several authors are named (Q27 «морские авторы —
    # Мелвилл, Конрад, Стивенсон»), run affinity_by_author for each in
    # parallel — the renderer can present the joined signature lists or
    # compute set intersections downstream.
    steps = [PlanStep(tool="affinity_by_author",
                      args={"author_regex": e.author_regex,
                            "top": e.top_n or 30,
                            "min_corpus_count": min_cc,
                            "pos_filter": e.pos_filter})]
    for extra in e.multi_author_regex[:3]:  # cap at 4 total to bound time
        steps.append(PlanStep(
            tool="affinity_by_author",
            args={"author_regex": extra,
                  "top": e.top_n or 30,
                  "min_corpus_count": min_cc,
                  "pos_filter": e.pos_filter},
            optional=True))
    explain = f"affinity_by_author({e.author_regex})"
    if e.multi_author_regex:
        explain += f" + {len(e.multi_author_regex[:3])} more"
    if propn_strict:
        explain += f" [propn_strict: min_corpus_count={min_cc}]"
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


@_with_author_copyright_check
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
    # Sprint 19+ — quote-lookup path takes priority over book_lookup
    # redirect. «угадай автора отрывка "It was the best of times..."»
    # has BOTH attribution_text (real passage) AND book_title (because
    # the quoted-string regex picked it up too). We want lexical_search
    # on the passage, not find_book(title="It was the best of times")
    # which would return nothing useful.
    text = (e.raw_misc or {}).get("attribution_text")
    has_substantive_passage = bool(text and len(text.split()) >= 5)

    # Sprint 18 — bibliographic «who wrote X» / «кто автор Дракулы»
    # falls through to book_lookup. The original «кто автор» rule was
    # tagged author_attribution but stylometric attribution requires
    # the passage text, not a book title. When a book is explicitly
    # named WITHOUT a substantive quoted passage, the user wants the
    # book's author from metadata — that's book_lookup territory.
    if (e.book_id or e.book_title) and not has_substantive_passage:
        return _plan_book_lookup(e)
    if not text:
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Вставь сам текст для атрибуции в кавычках, например:\n"
                "  «угадай автора отрывка \"<паста сюда>\"»\n\n"
                "Для коротких цитат (5+ слов) я найду точное совпадение в "
                "корпусе через FTS5. Для длинных образцов (200+ слов) — "
                "стилометрический анализ Burrows Delta."
            ),
            explain="запросил текст для author_attribution",
        )

    # Sprint 19+ — dual-path. Short passage = quote lookup; long passage
    # = stylometric attribution. Threshold 200 words: Burrows Delta
    # becomes statistically meaningful around 200-300 tokens; anything
    # shorter is noise. Lookup (FTS5 exact match via lexical_search)
    # works on as little as 5 words and is the right operation for
    # «угадай автора этой цитаты».
    word_count = len(text.split())
    if word_count < 200:
        # Quote lookup — wrap the passage in FTS5 phrase quotes so we
        # match the exact run. lexical_search returns pg_id + snippet
        # + score; renderer surfaces the matched book + author.
        # Trim if super long (FTS5 phrases >300 chars get expensive).
        phrase = text[:300] if len(text) > 300 else text
        # Strip terminal punctuation that breaks FTS5 phrase mode
        phrase = phrase.strip(' .,!?;:"\'«»""\'')
        return QueryPlan(
            intent="author_attribution", entities=e,
            steps=[PlanStep(
                tool="lexical_search",
                args={"query": f'"{phrase}"', "k": 5},
            )],
            expected_cost="cheap",
            explain=(f"quote lookup via lexical_search "
                     f"(passage={word_count} words, FTS5 exact match)"),
        )

    return QueryPlan(
        intent="author_attribution", entities=e,
        steps=[PlanStep(tool="author_attribution",
                        args={"text": text, "top": e.top_n or 5})],
        expected_cost="medium",
        explain=f"Burrows Delta attribution ({word_count} words)",
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
    # Sprint 19+ — corpus-wide aggregation request misroutes here when
    # «Flesch» / «readability» keyword fires book_readability but the
    # user actually wants «распределение по корпусу». Redirect to the
    # smart clarify recipe instead of bouncing to _need_book.
    raw_lc = ((e.raw_misc or {}).get("raw_text") or "").lower()
    if not e.book_id and not e.book_title:
        distribution_markers = (
            "распределение", "среднее", "медиана", "median", "average",
            "distribution", "p10", "p90", "p95", "процентиль",
        )
        corpus_scope = ("корпус" in raw_lc or "corpus" in raw_lc or
                         "all books" in raw_lc)
        if any(m in raw_lc for m in distribution_markers) and corpus_scope:
            recipe = _smart_clarify_recipe(e)
            if recipe:
                return QueryPlan(
                    intent="clarify", entities=e, steps=[],
                    needs_clarify=True,
                    clarify_question=recipe,
                    explain=("corpus-wide aggregation request — no "
                             "single tool, recipe offered"),
                )

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
    until BookProfile pipeline (Sprint 4) is online.

    Sprint 20+ B4 + B9: honor lang_hint (defaults to 'en') and
    exclude_archaic. Since we don't have a per-book archaic-density
    column yet, exclude_archaic surfaces as a plan-level render note
    that asks the renderer to disclose the limitation honestly rather
    than silently returning Pliny / Roman Stoicism for «B2 без архаизмов»."""
    lang = e.lang_hint or "en"
    notes: list[str] = []
    if e.exclude_archaic:
        # Stamp a render-time note so the renderer warns the user honestly
        # that we don't filter by archaic content automatically (B9). When
        # BookProfile.archaic_density lands (Sprint 4), this becomes a real
        # filter on top_books output.
        notes.append(
            "Пользователь просил «без архаизмов», но per-book archaic_density "
            "filter ещё не онлайн. DISCLOSE честно: «отсортировал по "
            "популярности; архаичность тайтла проверь через book_readability — "
            "Pliny/Бэкон/Шекспир заведомо архаичны, исключи их вручную». "
            "НЕ замалчивай это ограничение."
        )
    return QueryPlan(
        intent="book_recommendation", entities=e,
        steps=[PlanStep(tool="top_books_by_downloads",
                        args={"top": 20, "lang": lang})],
        expected_cost="medium",
        explain=(f"popular books (lang={lang}) → user filters by readability"
                 + (" [exclude_archaic flagged]" if e.exclude_archaic else "")),
        render_notes=notes,
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
    # word_contexts_global. hybrid pulls per_retriever from each side,
    # RRF-merges to top 12, optionally reranks with BGE cross-encoder
    # before slicing the final k. Sprint 18: rerank ON by default for
    # the no-author path — bi-encoder ranking surfaces lots of marginal
    # mentions; cross-encoder eliminates them.
    #
    # Sprint 21 B101: also fan out enrich_word in parallel so the
    # renderer can surface translation + IPA + POS + definition +
    # etymology alongside the contexts. enrich_word is Wiktionary-
    # cached (~1.5s first call, <5ms cached); independent of contexts
    # so the router runs them in parallel. enrich is optional — single-
    # word queries without author scope are the «расскажи мне про
    # слово X» case (Stan B101: «отдавать перевод слова, примеры в
    # контексте и этимологию» вместе).
    # Sprint 22+ B4: pass lang_hint through so «английская классика»
    # / «русский корпус» actually filter (Round 12 Q5 regression — 8/10
    # results were Finnish/Hungarian/Italian without filter).
    hs_args = {"query": e.word, "k": 12,
               "per_retriever": 50,
               "rerank_with": "bge_reranker"}
    if e.lang_hint:
        hs_args["lang"] = e.lang_hint
    return QueryPlan(
        intent="word_contexts", entities=e,
        steps=[
            PlanStep(tool="hybrid_search", args=hs_args),
            PlanStep(tool="enrich_word",
                     args={"word": e.word, "target_lang": "ru"},
                     optional=True),
        ],
        expected_cost="medium",
        explain=(f"hybrid_search({e.word}, lang={e.lang_hint or '*'}) "
                 f"— FTS5+Chroma RRF + BGE rerank, "
                 f"+ enrich_word in parallel (translation+etymology)"),
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


_MULTI_WORD_TIMELINE_RE = None


def _detect_multi_word_timeline(raw: str, primary: str | None) -> list[str]:
    """Sprint 18 — Round 8 C5: «timeline telephone+automobile+aeroplane»
    or «timeline telephone, automobile, aeroplane» — capture all bare
    lowercase Latin tokens chained by «+» or «,». Cap at 5 to bound
    wall-clock. Returns deduped list ordered by appearance, with the
    primary entity word first if present."""
    import re
    global _MULTI_WORD_TIMELINE_RE
    if _MULTI_WORD_TIMELINE_RE is None:
        # Triggers: «timeline X+Y+Z», «X, Y и Z по эпохам», «частота X+Y»
        _MULTI_WORD_TIMELINE_RE = re.compile(
            r"\b([a-z]{3,30})(?:\s*[+,]\s*([a-z]{3,30}))+",
            re.IGNORECASE,
        )
    if not raw:
        return [primary] if primary else []
    # Find the «X+Y» or «X, Y» span anywhere in the query
    m = _MULTI_WORD_TIMELINE_RE.search(raw)
    if not m:
        return [primary] if primary else []
    # Walk the matched span and split on + / , — captures every token
    span = m.group(0)
    tokens = re.split(r"\s*[+,]\s*", span)
    out: list[str] = []
    seen: set[str] = set()
    if primary and primary not in seen:
        out.append(primary.lower())
        seen.add(primary.lower())
    for t in tokens:
        t = t.strip().lower()
        if not t or t in seen:
            continue
        if len(t) < 3 or len(t) > 30:
            continue
        if not t.isascii() or not t.isalpha():
            continue
        out.append(t)
        seen.add(t)
        if len(out) >= 5:
            break
    return out


def _plan_word_timeline(e: Entities) -> QueryPlan:
    if e.year_from and not e.year_to:
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=[PlanStep(tool="words_disappearing_after",
                            args={"year": e.year_from - 1, "top": e.top_n or 25})],
            expected_cost="medium",
            explain=f"words_disappearing_after({e.year_from - 1})",
        )
    # Sprint 18 — multi-word timeline (Round 8 C5). Emit N parallel
    # word_freq_timeline calls; renderer plots them side by side.
    raw = (e.raw_misc or {}).get("raw_text") or ""
    multi_words = _detect_multi_word_timeline(raw, e.word)
    if len(multi_words) > 1:
        steps = [PlanStep(
            tool="word_freq_timeline",
            args={"word": w, "bucket_years": 25},
            optional=(i > 0),  # primary required, secondaries best-effort
        ) for i, w in enumerate(multi_words[:5])]
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=steps,
            expected_cost="medium",
            explain=(f"word_freq_timeline × {len(multi_words[:5])} "
                     f"({', '.join(multi_words[:5])})"),
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
    # word_pos_distribution rejects "all_corpus" string (line 1577 in
    # rag_tools.py): "bad scope; use {'book':PGid} | {'author':regex}".
    # When user asks generic «polysemy для set» with no scope, widen to
    # global-author regex which v1 _select_books treats as "all English
    # books"; max_occurrences=200 caps runtime to first 200 matches.
    if scope == "all_corpus":
        scope = {"author": ".*"}
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
        # Sprint 21 B101: fan out word_contexts in parallel — when user
        # asks etymology of a word, they often want примеры + перевод
        # together. word_etymology returns enrich_word data (translation
        # + IPA + POS + definition + etymology); word_contexts adds the
        # corpus examples. Both independent → router runs in parallel.
        # word_contexts is optional — Wiktionary outage doesn't kill
        # the etymology answer.
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[
                PlanStep(tool="word_etymology", args={"word": e.word}),
                PlanStep(tool="hybrid_search",
                         args={"query": e.word, "k": 6,
                               "per_retriever": 30,
                               "rerank_with": "bge_reranker"},
                         optional=True),
            ],
            expected_cost="cheap",
            explain=(f"word_etymology({e.word}) + hybrid_search({e.word}, k=6)"
                     f" parallel — Stan B101 bundle"),
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
    # Sprint 20 — translate-followup disclosure. When user said «переведи
    # эти слова», history layer set _translate_to='ru' AND switched
    # intent to `learning`. learning_words returns a DIFFERENT list from
    # the prior `affinity_by_author` turn (CEFR band-pass, not affinity).
    # Stamp a _render_hint so the renderer tells the user the list has
    # changed — otherwise they assume the same 96 words were translated.
    if (e.raw_misc or {}).get("_translate_to") == "ru":
        args["_translate_followup_disclose"] = True
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


@_with_author_copyright_check
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


# ===== Sprint 18 — similar_to (ambiguous similarity router) =====


def _plan_similar_to(e: Entities) -> QueryPlan:
    """«в стиле X» / «типа X» — X may be a book OR an author. Resolve
    by which entity slot the extractor filled, then delegate to the
    appropriate concrete plan. Mostly hits when neither book_similar
    nor author_closest's specific rules matched first (priority 130
    keeps it below those)."""
    if e.book_id or e.book_title:
        return _plan_book_similar(e)
    if e.author_regex:
        # Reuse author_closest semantics — «похож по стилю» = closest
        # stylistic neighbours via author_influences/Burrows Delta.
        return _plan_author_closest(e)
    # Neither resolved → clarify with both-options hint
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question=(
            "«В стиле X» — X это книга или автор? Уточни:\n"
            "• «в стиле книги "
            "«Pride and Prejudice»» → похожие книги\n"
            "• «в стиле автора Doyle» → похожие авторы по Burrows Delta"
        ),
        explain="similar_to — entity not resolved",
    )


# ===== Sprint 17 — book_similar =====


def _plan_book_similar(e: Entities) -> QueryPlan:
    """«Книги похожие на X», «продолжение X», «similar to X», «после X».

    Strategy: use X's canonical title as the semantic topic for
    find_book_by_topic. hybrid_search (semantic + lexical) finds
    chunks thematically related to X, dedupe-by-pg_id surfaces top
    candidate books. Result excludes X itself by post-filter (renderer
    can also note «excluding the reference book»).

    Requires a reference book. Without it the intent is meaningless —
    fall back to clarify with a hint.

    Sprint 20+ B8: enrich the topic query — bare title gives noisy
    word-cooccurrence results («Crime and Punishment» surfaces unrelated
    books with «crime»/«punishment» mentions). Adding «similar to / in
    theme of» framing biases semantic search toward thematic neighbours
    rather than lexical hits. Renderer also receives a note that the
    reference book itself should be excluded from the list (post-filter
    by pg_id at render time)."""
    if not e.book_id and not e.book_title:
        return _need_book(e)
    # Prefer the canonical English title — embeddings index is English,
    # so «Преступление и наказание» as topic returns less than
    # «Crime and Punishment».
    title = e.book_title or (e.raw_misc or {}).get("raw_text", "") or "books"
    # Sprint 20+ B8: enriched topic. "books with similar themes to X"
    # primes the embeddings toward thematic relatives. Empirically
    # better than bare title for "после X" / "sequel" queries.
    topic = f"books with similar themes and style to {title}"
    notes = []
    if e.book_id:
        notes.append(
            f"Это запрос «похожие на {title}» (book_similar). "
            f"Исходная книга — `{e.book_id}` ({title}). "
            f"ВАЖНО: при перечислении результатов EXCLUDE саму книгу "
            f"`{e.book_id}` из списка — у пользователя она уже прочитана. "
            f"Если она первая в результатах — пропусти и упомяни этот факт."
        )
    else:
        notes.append(
            f"Это запрос «похожие на {title}» (book_similar). "
            f"PG/U id исходной книги не разрешился (book_title только); "
            f"при перечислении проверь, не попадает ли сам тайтл «{title}» "
            f"в результаты — если да, EXCLUDE его."
        )
    steps = [PlanStep(
        tool="find_book_by_topic",
        # Sprint 18 — BGE rerank default. For «похожие на X» queries the
        # bi-encoder pool surfaces noisy neighbours (book mentions, not
        # thematic relatives); cross-encoder reorder is the win.
        args={"topic": topic, "top": 10, "rerank_with": "bge_reranker"},
    )]
    return QueryPlan(
        intent="book_similar", entities=e,
        steps=steps,
        expected_cost="medium",
        explain=(f"book_similar → find_book_by_topic(topic={topic!r}, "
                 f"rerank=bge_reranker) — thematic neighbours of "
                 f"{title} (excluding reference)"),
        render_notes=notes,
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
    ambiguous = _ambiguous_author_clarify(e)
    if ambiguous is not None:
        return ambiguous
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
    # Sprint 17 — books similar to a reference book
    "book_similar":         _plan_book_similar,
    # Sprint 18 — ambiguous similarity router («в стиле X»)
    "similar_to":           _plan_similar_to,
    # Sprint 20 — translate-followup escape hatch (honest clarify)
    # filled after function defined below
    "out_of_scope":         _plan_out_of_scope,
}


def _plan_translate_word_list(e: Entities) -> QueryPlan:
    """Sprint 20 — translate-followup with prior-words handoff.

    Stan 2026-05-19 prod: «дай переводы этих слов из контекста
    оригинальных книг» after a word-list turn. The prior assistant
    message rendered a markdown table — `history.merge_with_history`
    extracts column 1 and stashes the words on `e.raw_misc._prior_words`
    (capped at 10 to fit chat timeout: 10 × 1.5s Wiktionary lookup ≈
    15s + render + critic ≈ comfortably under 90s).

    Plan: one enrich_word step per word, each with target_lang='ru' so
    the v1 enrich pipeline returns IPA + part-of-speech + definition +
    Russian translation. Renderer combines them into a translation
    table aligned with the user's original list.

    If extraction failed (no markdown table / opaque format), fall
    back to the honest clarify telling user to list words explicitly.
    """
    prior_words = (e.raw_misc or {}).get("_prior_words") or []
    total = (e.raw_misc or {}).get("_prior_words_total") or len(prior_words)

    if not prior_words:
        # Extraction failed — surface honest clarify.
        return QueryPlan(
            intent="translate_word_list", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Хочешь перевод слов из предыдущего ответа? Я не смог "
                "автоматически вытащить их из текста (формат не "
                "распознался).\n\n"
                "Скопируй 5-10 слов из списка и спроси: «переведи "
                "tuppence, stitching, embroidery, strychnine, vavasour» "
                "— я подготовлю переводы с IPA и определением.\n\n"
                "Совет: спроси админа включить `WC_LLM_PLANNER=on` — "
                "v4 LLM-планнер видит conversation и решает это "
                "автоматически."
            ),
            explain="translate_word_list — extraction failed, surfacing clarify",
        )

    # Build N enrich_word steps. Each is independent (no deps), so a
    # failure in one (Wiktionary 404) doesn't kill the rest — router
    # collects partials.
    steps = [
        PlanStep(
            tool="enrich_word",
            args={"word": w, "target_lang": "ru"},
            optional=True,  # one Wiktionary miss != kill the whole batch
        )
        for w in prior_words[:10]
    ]
    # Inform renderer that this is a translate-followup over a prior
    # word list, NOT a fresh tool search. Renderer should:
    #   - present results in original list order
    #   - show enrich data per word (translation + IPA + definition)
    #   - mention if N was capped (10 of 96)
    explain = (f"translate_word_list: enrich_word × {len(steps)} from "
               f"prior assistant message")
    if total > len(steps):
        explain += f" (capped at {len(steps)} of {total} for chat timeout)"
    return QueryPlan(
        intent="translate_word_list", entities=e, steps=steps,
        expected_cost="heavy",
        explain=explain,
    )


# Wire builder into the dispatch table now that the function exists.
PLAN_BUILDERS["translate_word_list"] = _plan_translate_word_list


def _plan_export_word_list(e: Entities) -> QueryPlan:
    """Sprint 20+ B3 — export-followup with prior-words handoff.

    Stan Round 11 test «выгрузи в anki» / «csv pls» after a word-list
    turn used to fall to clarify because no rule matched and there was
    no plan to format prior words. Now:

    1. Intent classifier catches the export verb + format token.
    2. history.merge_with_history (existing translate-followup branch)
       extracts up to 10 prior words and stashes them on
       raw_misc._prior_words.
    3. This plan builder builds a NO-TOOL plan with render_notes
       containing the prior words + format hint. The renderer outputs
       a code-block formatted per format.

    No new tools needed — the formatting is deterministic enough that
    we can let the renderer LLM do it directly. Format hints describe
    the exact line shape so the LLM doesn't guess.
    """
    prior_words = (e.raw_misc or {}).get("_prior_words") or []
    total = (e.raw_misc or {}).get("_prior_words_total") or len(prior_words)
    fmt = e.export_format or "csv"

    if not prior_words:
        # Either there's no prior word-list turn, or extraction failed.
        # Surface an honest clarify with a copy-paste recipe so the user
        # can list 5-10 words and re-ask.
        return QueryPlan(
            intent="export_word_list", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                f"Хочешь выгрузить список слов в формате {fmt!r}? "
                f"Я не нашёл в предыдущем ответе таблицы со словами "
                f"(либо это первое сообщение, либо формат прошлого ответа "
                f"не распознался).\n\n"
                f"Перечисли явно слова, и я выгружу их:\n"
                f"• «выгрузи в {fmt}: tuppence, embroidery, stitching, vavasour»\n\n"
                f"Поддерживаемые форматы: anki (TSV), csv, json, markdown, tsv."
            ),
            explain=f"export_word_list — no prior words, surfacing clarify (fmt={fmt})",
        )

    # Build a render-only plan. Steps = [] is intentional; the renderer
    # uses render_notes + prior word list to format the output. This
    # avoids a useless Ollama round-trip for what is essentially
    # string transformation.
    spec = _format_spec(fmt)
    capped = prior_words[:50]    # safety cap — at 100 words renderer
                                 # output gets long but token-cheap
    notes = [
        f"EXPORT REQUEST: пользователь хочет prior word-list в формате "
        f"`{fmt}`. Слов извлечено: {len(capped)} из {total} (capped at 50 "
        f"if total > 50). Список (сохрани порядок!):\n"
        f"{capped}\n\n"
        f"{spec}\n\n"
        f"ВАЖНО: НЕ вызывай новые tools (steps=[] намеренно). Просто "
        f"отформатируй слова в указанном виде, оберни в ```код-блок```. "
        f"Если у тебя в context есть переводы/IPA/POS из предыдущих "
        f"enrich_word вызовов — добавь их. Иначе ОДНА колонка `word` "
        f"+ предупреждение «для перевода спроси отдельно»."
    ]
    return QueryPlan(
        intent="export_word_list", entities=e, steps=[],
        expected_cost="cheap",
        explain=(f"export_word_list: fmt={fmt}, n={len(capped)}/{total} "
                 f"(render-only, no tools)"),
        render_notes=notes,
    )


def _format_spec(fmt: str) -> str:
    """Human-readable description of the expected output shape per
    format. Renderer LLM reads this and produces the code block.
    Kept inline so format additions are one-place edits."""
    table = {
        "anki": (
            "ANKI TSV format: одна строка на слово, поля разделены TAB. "
            "Минимум: `word\\ttranslation\\tdefinition`. Header row НЕ "
            "нужен (Anki Desktop читает без header). Если у тебя нет "
            "перевода — оставь поле пустым между двумя \\t."
        ),
        "csv": (
            "CSV format: header `word,translation,definition` затем "
            "строки. Escape запятые в значениях двойными кавычками. "
            "Если перевода нет, оставь поле пустым (`word1,,definition1`)."
        ),
        "tsv": (
            "TSV (tab-separated): header `word\\ttranslation\\tdefinition` "
            "затем строки. Используй ровно TAB между полями (не пробелы)."
        ),
        "json": (
            "JSON format: массив объектов "
            "`[{\"word\": ..., \"translation\": ..., \"definition\": ...}, ...]`. "
            "Pretty-print с indent=2 для читаемости."
        ),
        "markdown": (
            "Markdown pipe-table:\n"
            "`| word | translation | definition |`\n"
            "`|------|-------------|------------|`\n"
            "Per-row после header. Подходит для Obsidian / Notion paste."
        ),
    }
    return table.get(fmt, table["csv"])


PLAN_BUILDERS["export_word_list"] = _plan_export_word_list


def _regex_to_canonical(regex: str | None) -> str | None:
    """`^Doyle, Arthur` → `Doyle, Arthur`. `^Wodehouse,` → `Wodehouse`."""
    if not regex:
        return None
    s = regex.lstrip("^").rstrip(",").strip()
    return s


def _smart_clarify_recipe(e: Entities) -> str | None:
    """Sprint 19+ — when intent failed but entities are RICH (compound
    research query with country + period + emotion + comparator), give
    the user a concrete 3-step recipe instead of generic «not sure».

    Stan 2026-05-19 «кто из английских авторов XIX века самый темный
    по эмоциональной палитре, у кого больше всего fear?» → entities
    correctly extracted country=GB, year_from=1800, year_to=1899,
    emotion=fear, but no single intent covers «extremum по emotion
    среди authors in period». The user should see a recipe, not a
    bare clarify.
    """
    raw_lc = ((e.raw_misc or {}).get("raw_text") or "").lower()

    # Sprint 19+ — triangulation pattern: «X между A и B, кто ближе к C».
    # Multi-author comparison with a third reference. Recipe: pairwise
    # compare_authors for each combo + Burrows Delta inspection.
    if e.author_regex and e.multi_author_regex and (
        "ближе к" in raw_lc or "closer to" in raw_lc or
        "ближе" in raw_lc):
        authors = [e.author_label or e.author_regex, *e.multi_author_regex]
        return (
            f"Triangulation запрос (кто из {authors[0]}/{authors[1]} "
            f"ближе к третьему) — у меня нет single tool. Собери из 2 шагов:\n\n"
            f"1. **compare_authors** для каждой пары:\n"
            f"   «сравни {authors[0]} и {authors[-1]} по стилю»\n"
            f"   «сравни {authors[1] if len(authors) > 1 else '?'} и "
            f"{authors[-1]} по стилю»\n"
            f"2. Сравни Burrows Delta distances — меньшее = ближе.\n"
            f"\nИли используй **author_closest** напрямую: «на кого "
            f"стилистически похож {authors[-1]}» — Burrows Delta ranks "
            f"top-N candidates."
        )

    # Sprint 19+ — corpus-wide distribution. «распределение Flesch по
    # корпусу», «среднее/медиана/p95 X в корпусе». No single tool
    # aggregates per-book metrics; recipe: top-N + sample.
    distribution_markers = (
        "распределение", "среднее", "медиана", "median", "average",
        "distribution", "p10", "p90", "p95", "процентиль",
    )
    has_distribution = any(m in raw_lc for m in distribution_markers)
    has_corpus_scope = ("корпус" in raw_lc or "corpus" in raw_lc or
                         "all books" in raw_lc)
    if has_distribution and has_corpus_scope:
        return (
            "Corpus-wide агрегации (распределение/среднее/p10/p90) "
            "пока нет как single tool — каждая metric (Flesch, FK, TTR, "
            "vocab_size) считается per-book. Recipe:\n\n"
            "1. **Sample**: «топ-20 книг по downloads» → берёшь представительную "
            "выборку (популярные книги = большая часть real reads)\n"
            "2. Для каждой — «уровень сложности <book>» (book_readability)\n"
            "3. Усредняй вручную из ответов\n\n"
            "В Sprint 20 backlog — corpus_stats_aggregate({metric})."
        )

    # Sprint 19+ — etymology-ratio across books. Stan 2026-05-19
    # «germanic vs latinate ratio в Beowulf и Paradise Lost». Multi-book
    # etymology comparison with no single tool. Each book gets its own
    # find_words_by_etymology({book: PGid}, family=X) — then ratio is
    # computed manually from candidate-count returns.
    ratio_markers = ("ratio", "соотношение", "процент", "доля", " vs ",
                      " vs.", "против ", "compare ", "сравни")
    has_ratio = any(m in raw_lc for m in ratio_markers)
    books_in_query: list[tuple[str, str]] = []
    if e.book_id and e.book_title:
        books_in_query.append((e.book_id, e.book_title))
    for bid, btitle in zip(e.multi_book_ids or [], e.multi_book_titles or []):
        books_in_query.append((bid, btitle))
    if e.etymology_family and len(books_in_query) >= 2 and has_ratio:
        fam = e.etymology_family
        # Pick the contrast family. germanic↔romance/latin, latin↔germanic,
        # norse↔romance, etc. If user said "vs latinate" or "vs latin"
        # explicitly, force that pair.
        contrast = "romance"
        if "latin" in raw_lc and fam in ("germanic", "norse"):
            contrast = "latin"
        elif fam in ("romance", "latin") and ("german" in raw_lc or "norse" in raw_lc):
            contrast = "germanic"
        steps = []
        for bid, btitle in books_in_query[:3]:
            steps.append(
                f"• `find_words_by_etymology` scope=book:{bid} ({btitle}) "
                f"family={fam} → high-affinity {fam} words\n"
                f"• `find_words_by_etymology` scope=book:{bid} ({btitle}) "
                f"family={contrast} → high-affinity {contrast} words"
            )
        bullets = "\n".join(steps)
        return (
            f"Etymology-ratio запрос ({fam} vs {contrast} across "
            f"{len(books_in_query)} книг) — у меня нет single tool. "
            f"Recipe per книгу:\n\n"
            f"{bullets}\n\n"
            f"Сравни len(matches) / candidate_pool по каждой книге — "
            f"ratio будет приблизительный (affinity-based, не coverage). "
            f"Для точной ratio: token-level POS tagger + per-token Wiktionary "
            f"lookup на весь текст — в backlog Sprint 20."
        )

    rich_fields = sum([
        bool(e.country),
        bool(e.year_from or e.year_to),
        bool(e.emotion),
        bool(e.author_regex),
        bool(e.book_id or e.book_title),
        bool(e.word),
        bool(e.etymology_family),
    ])
    if rich_fields < 2:
        return None

    parts: list[str] = ["Запрос сложный — у меня нет одного готового tool, "
                         "но это можно собрать из 2-3 шагов:"]
    if e.country and (e.year_from or e.year_to) and e.emotion:
        # «extremum по эмоции среди country-period»
        period = (f"{e.year_from or '?'}-{e.year_to or '?'}"
                   if (e.year_from or e.year_to) else "")
        parts.append(
            f"\n1. **Top авторы из {e.country}{' ' + period if period else ''}**: "
            f"спроси «топ-10 авторов из {e.country.lower()} по числу книг»\n"
            f"2. Для каждого верхнего — спроси «эмоциональный профиль <author>» "
            f"или «топ-3 книги {e.country.lower()} автора по {e.emotion} ratio»\n"
            f"3. Сравни {e.emotion}% по результатам"
        )
    elif e.country and e.emotion:
        parts.append(
            f"\n1. **Топ авторов из {e.country}**: «топ-10 авторов "
            f"{e.country.lower()} по числу книг»\n"
            f"2. **Эмоциональный профиль каждого**: «эмоциональный "
            f"профиль <author>» — посмотри {e.emotion}%\n"
        )
    elif e.country and (e.year_from or e.year_to):
        period = f"{e.year_from or '?'}-{e.year_to or '?'}"
        parts.append(
            f"\n1. **Топ авторов {e.country}**: «топ-10 авторов "
            f"{e.country.lower()} {period}»\n"
            f"2. Для каждого — нужный анализ (vocab / emotion / readability)"
        )
    else:
        # Generic compound — list what we found
        found = []
        if e.country: found.append(f"country={e.country}")
        if e.year_from or e.year_to:
            found.append(f"period={e.year_from or '?'}-{e.year_to or '?'}")
        if e.emotion: found.append(f"emotion={e.emotion}")
        if e.author_regex: found.append(f"author={e.author_regex}")
        if e.word: found.append(f"word={e.word!r}")
        parts.append(
            "\nЯ извлёк: " + ", ".join(found) + ". Уточни глагол "
            "(найди / сравни / топ / профиль / архаизмы) и я разверну в "
            "конкретный tool chain."
        )
    return "\n".join(parts)


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
    return fn(entities)
