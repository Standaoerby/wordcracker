"""Shared types and helpers for plan builders.

Phase 4 (REFACTOR_BRIEF / REMEDIATION_BRIEF T4) — extracted from
`scripts/v2/planner/plan.py` so each domain-builder module can import a
single small surface (`PlanStep`, `QueryPlan`, the clarify helpers, the
scope helpers, the copyright machinery, the smart-clarify-recipe) and
stay focused on intent → plan mapping.

Public for backward-compat: `plan.py` re-exports everything from this
module so external imports like `from scripts.v2.planner.plan import
PlanStep, QueryPlan, _need_author, ...` keep working.

R6: builders MUST NOT re-implement fan-out themselves. The
`_fan_out_authors_steps` shim below is kept only for legacy tests; it
delegates to `apply_fan_out_invariant`. New builders set the `fan_out`
marker on a single PlanStep and let the router clone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from scripts.v2.planner.entities import Entities


Cost = Literal["cheap", "medium", "heavy"]


@dataclass
class PlanStep:
    tool: str
    args: dict
    depends_on: list[int] = field(default_factory=list)
    inject_result_as: str | None = None  # key in next step's args to fill
    optional: bool = False
    # Phase 4 — fan-out invariant marker. Single value declares HOW the
    # router clones this step per `entities.multi_author_regex` entry.
    #   "scope_author"   — args has `scope: {"author": ...}`; clone swaps
    #                      scope's author while preserving country/year.
    #   "author_regex"   — args has `author_regex: ...` directly; clone
    #                      swaps that arg.
    # None (default)     — no fan-out. Cloned steps always get this set
    # back to None so the invariant is idempotent.
    fan_out: str | None = None


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


# ===== «need <entity>» clarify helpers =====


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


def _ngram_n_from_text(text: str) -> int:
    """Detect bigram/trigram intent from raw query text → n ∈ {1,2,3}.

    R-29 S1 — shared by `_plan_author_top_words` and `_plan_book_top_words`
    so the n-bump rule lives in ONE place (the book route mirrors the
    author route's «топ-15 биграмм у X» → n=2 behaviour). Defaults to 1.
    """
    import re
    t = (text or "").lower()
    if re.search(r"\bтриграмм|trigram", t):
        return 3
    if re.search(r"\bбиграмм|bigram", t):
        return 2
    return 1


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


def _fan_out_authors_steps(
    e: Entities,
    *,
    tool: str,
    base_args: dict,
    scope_field: str = "scope",
    author_field: str | None = None,
    cap: int = 3,
    primary_optional: bool = False,
) -> list[PlanStep]:
    """DEPRECATED (Phase 4). Use the `fan_out` marker on a single
    PlanStep and let the router's `apply_fan_out_invariant` clone it
    per `entities.multi_author_regex`.

    Kept as a thin compatibility shim for tests that exercised the
    helper directly. Builders MUST NOT call this — see R6 («Никаких
    новых _plan_* с копипастой fan-out»). The shim now delegates to
    the invariant so behavior stays identical even if a tool still
    relies on it.
    """
    if not e.author_regex:
        return []

    fan_out = "author_regex" if author_field is not None else "scope_author"
    primary_args = dict(base_args)
    if author_field is not None:
        primary_args[author_field] = e.author_regex
    else:
        primary_args[scope_field] = {"author": e.author_regex}
        if e.country:
            primary_args[scope_field]["country"] = e.country
        if e.year_from:
            primary_args[scope_field]["year_from"] = e.year_from
        if e.year_to:
            primary_args[scope_field]["year_to"] = e.year_to

    primary = PlanStep(
        tool=tool, args=primary_args,
        optional=primary_optional,
        fan_out=fan_out,
    )
    # Defer to the invariant so the shim and the real path produce the
    # same step list. Imported lazily to avoid a circular import at
    # module load time (invariants imports PlanStep from this module).
    from scripts.v2.planner.invariants import apply_fan_out_invariant
    stub_plan = QueryPlan(intent="_shim", entities=e, steps=[primary])
    fanned = apply_fan_out_invariant(stub_plan, cap=cap)
    return fanned.steps


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


# ===== smart-clarify recipe (used by builders + by the orchestrator) =====


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
