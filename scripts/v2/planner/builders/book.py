"""Book-level plan builders.

`book_lookup`, `book_compare`, `book_vocab`, `book_readability`,
`book_archaic`, `book_emotion`, `book_recommendation`, `book_similar`,
`book_readability_compare`, `book_pub_year`, `book_extremum`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import (
    PlanStep,
    QueryPlan,
    _need_book,
    _smart_clarify_recipe,
    _with_copyright_check,
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
